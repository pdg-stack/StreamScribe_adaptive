"""Decouples receiving audio (fast: VAD segmentation only) from processing
it (slow: transcribe + translate), so a slow segment doesn't block reading
more audio off the socket, and so backlog can be measured and preempted.

One instance per WebSocket connection. Not thread-safe across OS threads --
intended for use within a single asyncio event loop, where the lack of an
`await` between mutating operations already prevents interleaving.
"""

from __future__ import annotations

import asyncio
import collections
import time
from dataclasses import dataclass

from backend.config import PROCESSING_TIME_WINDOW, QUEUE_PREEMPTION_FACTOR
from backend.transcription.vad_segmenter import SegmenterEvent


@dataclass
class QueuedSegment:
    event: SegmenterEvent
    enqueued_at: float


class SegmentQueue:
    def __init__(self) -> None:
        self._items: collections.deque[QueuedSegment] = collections.deque()
        self._not_empty = asyncio.Event()
        self._processing_times: list[float] = []

    def push(self, event: SegmenterEvent) -> None:
        self._items.append(QueuedSegment(event=event, enqueued_at=time.time()))
        self._not_empty.set()

    async def pop(self) -> QueuedSegment:
        while not self._items:
            self._not_empty.clear()
            await self._not_empty.wait()
        return self._items.popleft()

    def record_processing_time(self, elapsed: float) -> None:
        self._processing_times.append(elapsed)
        self._processing_times = self._processing_times[-PROCESSING_TIME_WINDOW:]

    def preempt_if_needed(self, acceptable_latency: float) -> bool:
        """Drops every queued segment except the newest if the predicted
        wait for that newest segment -- (segments ahead of it) * (recent
        average processing time) -- exceeds QUEUE_PREEMPTION_FACTOR times
        the acceptable-latency setting. Returns True if it preempted."""
        depth = len(self._items)
        if depth <= 1 or not self._processing_times:
            return False

        avg_processing_time = sum(self._processing_times) / len(self._processing_times)
        predicted_wait = (depth - 1) * avg_processing_time

        if predicted_wait > QUEUE_PREEMPTION_FACTOR * acceptable_latency:
            newest = self._items[-1]
            self._items.clear()
            self._items.append(newest)
            return True
        return False

    def __len__(self) -> int:
        return len(self._items)
