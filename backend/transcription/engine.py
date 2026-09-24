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
    MODEL_TIERS,
    RTF_GREEN_MAX,
    RTF_YELLOW_MAX,
    STRAIN_WINDOW,
)

_model_cache: dict[tuple[str, str], WhisperModel] = {}
_lock = threading.Lock()


def get_model(model_name: str, device: str = "cpu") -> WhisperModel:
    """Returns the cached model for (model_name, device), loading it on
    first use. Loading is the expensive part (disk/network + memory), not
    switching tiers once every tier has been used once in this process."""
    key = (model_name, device)
    with _lock:
        if key not in _model_cache:
            compute_type = "int8" if device == "cpu" else "float16"
            _model_cache[key] = WhisperModel(model_name, device=device, compute_type=compute_type)
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
    tier has already been used once in this process."""

    def __init__(self, device: str = "cpu") -> None:
        self.device = device
        self._tier_index = 0  # 0 == MODEL_TIERS[0] == "small"
        self._rtf_samples: list[float] = []

    @property
    def model_tier(self) -> str:
        return MODEL_TIERS[self._tier_index]

    def _record_rtf(self, rtf: float) -> str:
        """Folds `rtf` into the rolling window, steps the active tier up or
        down once the window is full and consistently past a threshold,
        and returns this segment's cpu_status."""
        self._rtf_samples.append(rtf)
        self._rtf_samples = self._rtf_samples[-STRAIN_WINDOW:]
        window_full = len(self._rtf_samples) >= STRAIN_WINDOW
        avg_rtf = sum(self._rtf_samples) / len(self._rtf_samples)

        if avg_rtf > RTF_YELLOW_MAX:
            if window_full and self._tier_index < len(MODEL_TIERS) - 1:
                self._tier_index += 1
                self._rtf_samples.clear()
            return "red"
        if avg_rtf > RTF_GREEN_MAX:
            return "yellow"
        if window_full and self._tier_index > 0:
            self._tier_index -= 1
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
