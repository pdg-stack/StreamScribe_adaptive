"""VAD-gated audio segmentation. faster-whisper transcribes a bounded
buffer, not a token stream, so "streaming" here means segment-and-
transcribe-as-you-go: buffer speech frames, close a segment on trailing
silence or a max-length cutoff, and emit periodic partial results on long
segments so the overlay never looks frozen mid-sentence.

PLAIN-ENGLISH OVERVIEW (for anyone new to this file):
"VAD" = Voice Activity Detection -- a small library (webrtcvad) that looks
at a tiny slice of audio (30 milliseconds) and answers one yes/no
question: "is someone talking right now, or is this silence?" That's the
ONLY thing it decides. This class calls VAD on every slice as audio comes
in, 30ms at a time, and uses the answers to decide when one "utterance"
(a sentence or phrase) has started and ended:
  - speech starts -> start collecting frames into a buffer
  - speech continues -> keep collecting, and every few seconds, send off
    what's collected so far as a "partial" (a rough, in-progress guess,
    so the caption on screen doesn't sit frozen mid-sentence)
  - ~half a second of silence after speech -> the utterance is done,
    send off everything collected as a "final" (the real, complete answer)
This is what turns a never-ending stream of raw audio bytes into discrete
chunks that the ASR model (faster-whisper etc.) can actually transcribe --
those models expect one finished clip at a time, not an infinite stream.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import webrtcvad

from backend.config import (
    FRAME_MS,
    MAX_SEGMENT_S,
    PARTIAL_INTERVAL_S,
    SAMPLE_RATE,
    SILENCE_TRIGGER_MS,
    VAD_AGGRESSIVENESS,
)

_FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_MS / 1000)
_FRAME_BYTES = _FRAME_SAMPLES * 2  # int16 PCM
_PARTIAL_WINDOW_FRAMES = int(PARTIAL_INTERVAL_S * 2 * 1000 / FRAME_MS)


@dataclass
class SegmenterEvent:
    kind: str  # "partial" | "final"
    audio: np.ndarray  # mono float32 PCM in [-1, 1]
    closed_at: float  # time.time() when this event was produced -- used by
    # the frontend's acceptable-latency logic and the backend's queue
    # preemption to measure how stale a pending segment has become.


@dataclass
class VadSegmenter:
    """Feed raw int16 PCM bytes via push(); it returns SegmenterEvents as
    segments close or as periodic partials fire. Not thread-safe -- one
    instance per WebSocket connection, called from a single receive loop."""

    _vad: webrtcvad.Vad = field(default_factory=lambda: webrtcvad.Vad(VAD_AGGRESSIVENESS))
    _byte_buffer: bytes = b""
    _segment_frames: list[bytes] = field(default_factory=list)
    _silence_ms: int = 0
    _segment_ms: int = 0
    _last_partial_ms: int = 0
    _in_speech: bool = False

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def push(self, pcm_bytes: bytes) -> list[SegmenterEvent]:
        self._byte_buffer += pcm_bytes
        events: list[SegmenterEvent] = []
        while len(self._byte_buffer) >= _FRAME_BYTES:
            frame = self._byte_buffer[:_FRAME_BYTES]
            self._byte_buffer = self._byte_buffer[_FRAME_BYTES:]
            events.extend(self._push_frame(frame))
        return events

    def _push_frame(self, frame: bytes) -> list[SegmenterEvent]:
        is_speech = self._vad.is_speech(frame, SAMPLE_RATE)

        if is_speech:
            self._in_speech = True
            self._silence_ms = 0
            self._segment_frames.append(frame)
            self._segment_ms += FRAME_MS
        elif self._in_speech:
            # Trailing silence inside/just after an utterance: keep
            # buffering briefly so words aren't clipped, while it counts
            # toward the silence-trigger threshold below.
            self._segment_frames.append(frame)
            self._segment_ms += FRAME_MS
            self._silence_ms += FRAME_MS

        if not self._in_speech:
            return []

        if self._silence_ms >= SILENCE_TRIGGER_MS or self._segment_ms >= MAX_SEGMENT_S * 1000:
            event = SegmenterEvent(kind="final", audio=self._to_audio(self._segment_frames), closed_at=time.time())
            self._reset()
            return [event]

        if self._segment_ms - self._last_partial_ms >= PARTIAL_INTERVAL_S * 1000:
            self._last_partial_ms = self._segment_ms
            # Bound the partial-pass window so cost doesn't grow with
            # segment length -- only the most recent ~2x the partial
            # interval is re-transcribed each time, not the whole segment.
            window = self._segment_frames[-_PARTIAL_WINDOW_FRAMES:]
            return [SegmenterEvent(kind="partial", audio=self._to_audio(window), closed_at=time.time())]

        return []

    def _reset(self) -> None:
        self._segment_frames = []
        self._silence_ms = 0
        self._segment_ms = 0
        self._last_partial_ms = 0
        self._in_speech = False

    def force_close(self) -> list[SegmenterEvent]:
        """Closes whatever's currently buffered as a final segment, as if
        trailing silence had just completed naturally, and resets to a
        clean idle state. For when the frontend pauses capture: no more
        audio bytes are coming, so the normal "the next frame arrives and
        detects the speech->silence transition" path (_push_frame, only
        reachable via push()) can never fire on its own -- without this,
        in_speech would stay stuck True and this connection would never
        emit another idle transition, leaving the frontend's queue/status
        light waiting on something that will never arrive."""
        if not self._in_speech or not self._segment_frames:
            self._reset()
            return []
        event = SegmenterEvent(kind="final", audio=self._to_audio(self._segment_frames), closed_at=time.time())
        self._reset()
        return [event]

    @staticmethod
    def _to_audio(frames: list[bytes]) -> np.ndarray:
        pcm = b"".join(frames)
        samples = np.frombuffer(pcm, dtype=np.int16)
        return samples.astype(np.float32) / 32768.0
