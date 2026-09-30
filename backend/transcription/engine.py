"""Streaming ASR engine. Adapts LocalScribe_whisper_modal's proven
WhisperModel cache pattern (one model instance per (model_name, device),
loaded once and reused) to in-memory audio segments instead of files, and
adds a three-tier small/base/tiny cascade that degrades and recovers
automatically based on measured CPU strain (processing time vs. audio
duration -- the real-time factor, RTF).
"""

from __future__ import annotations

import threading
import time
from typing import NamedTuple

import numpy as np
from faster_whisper import WhisperModel

from backend.config import (
    BEAM_SIZE,
    CPU_THREADS_PER_WORKER,
    MODEL_TIERS,
    PARALLEL_WORKERS,
    QUEUE_LENGTH_DEMOTE_DEPTH,
    QUEUE_LENGTH_PROMOTE_DEPTH,
    QUEUE_STRAIN_WINDOW,
    RTF_GREEN_MAX,
    RTF_YELLOW_MAX,
    STRAIN_WINDOW,
)

_model_cache: dict[tuple[str, str], WhisperModel] = {}
_lock = threading.Lock()


def get_model(model_name: str, device: str = "cpu") -> WhisperModel:
    """Returns the cached model for (model_name, device), loading it on
    first use. Loading is the expensive part (disk/network + memory), not
    switching tiers once every tier has been used once in this process.

    `num_workers`/`cpu_threads` (CPU only -- Modal's own remote model, built
    separately in modal_engine.py, isn't affected) are what actually make
    backend/main.py's concurrent processor workers a real speedup rather
    than several threads queueing behind one CTranslate2 replica: num_workers
    gives the model that many replicas to serve concurrent transcribe()
    calls in true parallel, and cpu_threads bounds each replica's own
    intra-op threading so total demand stays sized to what the machine
    actually has (see config.py's PARALLEL_WORKERS)."""
    key = (model_name, device)
    with _lock:
        if key not in _model_cache:
            compute_type = "int8" if device == "cpu" else "float16"
            num_workers = PARALLEL_WORKERS if device == "cpu" else 1
            cpu_threads = CPU_THREADS_PER_WORKER if device == "cpu" else 0
            _model_cache[key] = WhisperModel(
                model_name, device=device, compute_type=compute_type,
                cpu_threads=cpu_threads, num_workers=num_workers,
            )
        return _model_cache[key]


class TranscriptionResult(NamedTuple):
    text: str
    detected_lang: str
    model_tier: str
    cpu_status: str


class AdaptiveEngine:
    """One instance per WebSocket connection. Tracks a rolling window of
    recent real-time-factor (RTF) samples to decide the active model tier
    and the cpu_status shown in the overlay, and reuses the process-wide
    model cache so stepping between tiers doesn't reload from disk once a
    tier has already been used once in this process.

    `tier_mode` is either "auto" (the cascade below picks the tier) or an
    explicit tier name ("small"/"base"/"tiny") -- an explicit choice is
    honored as-is and never overridden by strain, though cpu_status is
    still computed and shown (useful diagnostic even when not driving a
    fallback)."""

    def __init__(self, device: str = "cpu", tier_mode: str = "auto") -> None:
        self.device = device
        self.tier_mode = tier_mode
        self._tier_index = 0  # 0 == MODEL_TIERS[0] == "small"
        self._rtf_samples: list[float] = []
        self._queue_len_samples: list[int] = []
        # transcribe_segment() runs inside asyncio.to_thread, and with
        # PARALLEL_WORKERS > 1 several of those calls are genuinely
        # in flight on different OS threads at once -- _record_rtf and
        # record_queue_length both mutate the same lists/_tier_index, so
        # without this they can race (e.g. two threads' append+slice
        # interleaving and dropping a sample, or both deciding to demote
        # off the same pre-clear window).
        self._state_lock = threading.Lock()

    @property
    def model_tier(self) -> str:
        if self.tier_mode != "auto":
            return self.tier_mode
        return MODEL_TIERS[self._tier_index]

    def set_tier_mode(self, tier_mode: str) -> None:
        with self._state_lock:
            self.tier_mode = tier_mode
            self._rtf_samples.clear()
            self._queue_len_samples.clear()

    def record_queue_length(self, queue_length: int) -> None:
        """A second, more urgent strain signal alongside _record_rtf: a
        burst of segments can back the queue up even when each one
        individually still looks fine to RTF. In "auto" tier_mode, try a
        lighter tier -- reversible, and may let the queue drain on its own
        -- before the queue's own preemption logic (segment_queue.py) ever
        has to drop segments outright. Call once per segment about to be
        processed, before popping it, so the depth reflects the backlog
        actually waiting."""
        with self._state_lock:
            if self.tier_mode != "auto":
                return
            self._queue_len_samples.append(queue_length)
            self._queue_len_samples = self._queue_len_samples[-QUEUE_STRAIN_WINDOW:]
            if len(self._queue_len_samples) < QUEUE_STRAIN_WINDOW:
                return
            avg_depth = sum(self._queue_len_samples) / len(self._queue_len_samples)

            if avg_depth > QUEUE_LENGTH_DEMOTE_DEPTH and self._tier_index < len(MODEL_TIERS) - 1:
                self._tier_index += 1
                self._queue_len_samples.clear()
                self._rtf_samples.clear()
            elif avg_depth <= QUEUE_LENGTH_PROMOTE_DEPTH and self._tier_index > 0:
                # Same jump-to-best-tier reasoning as _record_rtf's recovery
                # branch: demotion already re-triggers correctly if this
                # turns out to be premature.
                self._tier_index = 0
                self._queue_len_samples.clear()
                self._rtf_samples.clear()

    def _record_rtf(self, rtf: float) -> str:
        """Folds `rtf` into the rolling window, steps the active tier up or
        down once the window is full and consistently past a threshold
        (only in "auto" tier_mode), and returns this segment's cpu_status."""
        with self._state_lock:
            self._rtf_samples.append(rtf)
            self._rtf_samples = self._rtf_samples[-STRAIN_WINDOW:]
            window_full = len(self._rtf_samples) >= STRAIN_WINDOW
            avg_rtf = sum(self._rtf_samples) / len(self._rtf_samples)
            auto = self.tier_mode == "auto"

            if avg_rtf > RTF_YELLOW_MAX:
                if auto and window_full and self._tier_index < len(MODEL_TIERS) - 1:
                    self._tier_index += 1
                    self._rtf_samples.clear()
                return "red"
            if avg_rtf > RTF_GREEN_MAX:
                return "yellow"
            if auto and window_full and self._tier_index > 0:
                # Jump straight back to the best tier rather than creeping
                # up one step at a time: incrementally stepping tiny->base
                # ->small needs a full clean STRAIN_WINDOW streak at *each*
                # intermediate tier, and switching to a heavier tier right
                # after recovering tends to itself nudge RTF back up for a
                # segment or two -- easy to get stuck just below "small"
                # indefinitely. Always attempting "small" and letting the
                # existing demotion logic above cascade back down through
                # base/tiny if it's still too slow gets back to the best
                # tier whenever it's actually sustainable, with no new
                # failure mode (demotion already handles a bad attempt
                # correctly).
                self._tier_index = 0
                self._rtf_samples.clear()
            return "green"

    def transcribe_segment(
        self, audio: np.ndarray, sample_rate: int = 16000, language: str | None = None
    ) -> TranscriptionResult:
        """audio: mono float32 PCM in [-1, 1]. Runs the currently active
        tier's model, measures RTF, and updates the tier/status for the
        *next* segment -- this segment's own result reports the tier that
        actually produced it. `language=None` auto-detects (the default,
        source-language dropdown's "Auto Detect" option); passing an
        ISO 639-1 code forces that language instead of detecting it, and
        `detected_lang` on the result will just echo it back."""
        ran_tier = self.model_tier
        model = get_model(ran_tier, self.device)
        duration = len(audio) / sample_rate

        start = time.monotonic()
        segments, info = model.transcribe(audio, language=language, beam_size=BEAM_SIZE)
        text = " ".join(s.text.strip() for s in segments).strip()
        elapsed = time.monotonic() - start

        rtf = elapsed / duration if duration > 0 else 0.0
        cpu_status = self._record_rtf(rtf)

        return TranscriptionResult(
            text=text,
            detected_lang=info.language,
            model_tier=ran_tier,
            cpu_status=cpu_status,
        )
