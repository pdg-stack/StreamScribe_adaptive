"""Parakeet TDT ASR engine, via sherpa-onnx's quantized ONNX export.
Local-only (never via Modal, per plan) -- a single 0.6B checkpoint, no
tiers. Covers Russian/Spanish/English/Italian/Portuguese; the frontend's
language dropdown limits selectable languages to what's actually
available per engine, so there's nothing to route around here.

NeMo/Parakeet doesn't expose which language it detected (a known,
currently-open gap in NVIDIA's own model -- see the plan's ASR research
section), so `detected_lang` on results just echoes back whatever was
passed in (usually None); the overlay's "Auto Detect (Russian)"-style
suffix only works when the engine is faster-whisper.
"""

from __future__ import annotations

import tarfile
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
import sherpa_onnx

from backend.config import PARAKEET_CACHE_DIR, PARAKEET_MODEL_URL, RTF_GREEN_MAX, RTF_YELLOW_MAX, STRAIN_WINDOW
from backend.transcription.engine import TranscriptionResult

_lock = threading.Lock()
_recognizer: sherpa_onnx.OfflineRecognizer | None = None


def _ensure_model(cache_dir: Path) -> Path:
    model_dir = cache_dir / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
    if (model_dir / "encoder.int8.onnx").exists():
        return model_dir

    cache_dir.mkdir(parents=True, exist_ok=True)
    archive_path = cache_dir / "parakeet.tar.bz2"
    urllib.request.urlretrieve(PARAKEET_MODEL_URL, archive_path)
    with tarfile.open(archive_path, "r:bz2") as tar:
        tar.extractall(cache_dir)
    archive_path.unlink()
    return model_dir


def _get_recognizer() -> sherpa_onnx.OfflineRecognizer:
    global _recognizer
    with _lock:
        if _recognizer is None:
            model_dir = _ensure_model(Path(PARAKEET_CACHE_DIR))
            _recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=str(model_dir / "encoder.int8.onnx"),
                decoder=str(model_dir / "decoder.int8.onnx"),
                joiner=str(model_dir / "joiner.int8.onnx"),
                tokens=str(model_dir / "tokens.txt"),
                model_type="nemo_transducer",
                feature_dim=128,
                decoding_method="greedy_search",
                num_threads=4,
            )
        return _recognizer


class ParakeetEngine:
    """Same transcribe_segment()/model_tier/cpu_status shape as
    AdaptiveEngine, so main.py can swap between them behind one interface
    -- but there's only one checkpoint, so tier_mode/set_tier_mode don't
    apply here."""

    def __init__(self) -> None:
        self._rtf_samples: list[float] = []
        # transcribe_segment() runs inside asyncio.to_thread; with
        # PARALLEL_WORKERS > 1 (see backend/main.py), several calls can be
        # in flight on different OS threads at once, all mutating
        # _rtf_samples -- the recognizer itself handles concurrent decode
        # fine (each call gets its own stream), but this bookkeeping needs
        # its own lock the same way AdaptiveEngine's does.
        self._state_lock = threading.Lock()

    @property
    def model_tier(self) -> str:
        return "parakeet"

    def _record_rtf(self, rtf: float) -> str:
        with self._state_lock:
            self._rtf_samples.append(rtf)
            self._rtf_samples = self._rtf_samples[-STRAIN_WINDOW:]
            avg_rtf = sum(self._rtf_samples) / len(self._rtf_samples)
        if avg_rtf > RTF_YELLOW_MAX:
            return "red"
        if avg_rtf > RTF_GREEN_MAX:
            return "yellow"
        return "green"

    def transcribe_segment(
        self, audio: np.ndarray, sample_rate: int = 16000, language: str | None = None
    ) -> TranscriptionResult:
        recognizer = _get_recognizer()
        duration = len(audio) / sample_rate

        start = time.monotonic()
        stream = recognizer.create_stream()
        stream.accept_waveform(sample_rate, audio)
        recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        elapsed = time.monotonic() - start

        rtf = elapsed / duration if duration > 0 else 0.0
        cpu_status = self._record_rtf(rtf)

        return TranscriptionResult(
            text=text,
            detected_lang=language,
            model_tier=self.model_tier,
            cpu_status=cpu_status,
        )
