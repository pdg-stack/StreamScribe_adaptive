"""Decouples receiving audio (fast: VAD segmentation only) from processing
it (slow: transcribe + translate), so a slow segment doesn't block reading
more audio off the socket, and so backlog can be measured and preempted.

One instance per WebSocket connection. Not thread-safe across OS threads --
intended for use within a single asyncio event loop, where the lack of an
`await` between mutating operations already prevents interleaving.

PLAIN-ENGLISH OVERVIEW (for anyone new to this file):
This is a to-do list ("queue") of chunks of speech waiting to be
transcribed. Two different parts of the program share it:
  - one part's only job is to keep listening to incoming audio and drop
    finished chunks ("segments") onto this list (push);
  - another part (main.py's _processor) picks chunks off the list one at a
    time and does the actual (slow) work of transcribing + translating
    them (pop).
Keeping these two jobs separate means "listening" never has to pause and
wait for "transcribing" to finish -- audio keeps getting captured smoothly
even while a slow segment is still being worked on. That's the entire
reason this class exists instead of just processing each segment the
instant it's ready.
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
        """Add one finished chunk of speech to the end of the list.
        Called by main.py's _receiver as soon as the VAD segmenter decides
        a chunk is ready (see vad_segmenter.py)."""
        self._items.append(QueuedSegment(event=event, enqueued_at=time.time(), seq=self._next_seq))
        self._next_seq += 1
        # Wakes up anything that's currently sitting in pop() below, waiting
        # for "there's now at least one item" -- see pop()'s comment.
        self._not_empty.set()

    async def pop(self) -> QueuedSegment:
        """Take the oldest chunk off the front of the list and return it,
        for a worker to actually transcribe. If the list is currently
        empty, this waits (without spinning/burning CPU) until push()
        above adds something, then tries again."""
        while not self._items:
            self._not_empty.clear()
            await self._not_empty.wait()
        return self._items.popleft()

    def record_processing_time(self, elapsed: float) -> None:
        """Remembers how long the last few segments took to transcribe, so
        preempt_if_needed() below can estimate how long a newly queued
        segment will have to wait its turn."""
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
