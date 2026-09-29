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
    seq: int  # monotonic enqueue order -- see result_sequencer.py


class SegmentQueue:
    def __init__(self) -> None:
        self._items: collections.deque[QueuedSegment] = collections.deque()
        self._not_empty = asyncio.Event()
        self._processing_times: list[float] = []
        self._next_seq = 0

    @property
    def next_seq(self) -> int:
        """The seq the next push() would assign -- i.e. "one past
        everything pushed so far". Used by ResultSequencer.submit_idle to
        place the idle transition correctly in sequence order."""
        return self._next_seq

    def push(self, event: SegmenterEvent) -> None:
        self._items.append(QueuedSegment(event=event, enqueued_at=time.time(), seq=self._next_seq))
        self._next_seq += 1
        self._not_empty.set()

    async def pop(self) -> QueuedSegment:
        while not self._items:
            self._not_empty.clear()
            await self._not_empty.wait()
        return self._items.popleft()

    def record_processing_time(self, elapsed: float) -> None:
        self._processing_times.append(elapsed)
        self._processing_times = self._processing_times[-PROCESSING_TIME_WINDOW:]

    def preempt_if_needed(self, acceptable_latency: float) -> list[int]:
        """Drops every queued segment except the newest if the predicted
        wait for that newest segment -- (segments ahead of it) * (recent
        average processing time) -- exceeds QUEUE_PREEMPTION_FACTOR times
        the acceptable-latency setting. Returns the seqs of whatever got
        dropped (empty if it didn't preempt), so the caller can tell
        ResultSequencer those segments will never complete -- otherwise it
        would wait out the in-flight timeout for segments that were never
        even submitted to a worker in the first place."""
        depth = len(self._items)
        if depth <= 1 or not self._processing_times:
            return []

        avg_processing_time = sum(self._processing_times) / len(self._processing_times)
        predicted_wait = (depth - 1) * avg_processing_time

        if predicted_wait > QUEUE_PREEMPTION_FACTOR * acceptable_latency:
            newest = self._items[-1]
            dropped_seqs = [item.seq for item in self._items if item is not newest]
            self._items.clear()
            self._items.append(newest)
            return dropped_seqs
        return []

    def __len__(self) -> int:
        return len(self._items)
