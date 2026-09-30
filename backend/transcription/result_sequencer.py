"""Restores in-order delivery over a WebSocket when multiple concurrent
processor workers (see main.py's PARALLEL_WORKERS) race to finish segments
out of the order they were spoken in -- a fast worker on a short segment
can easily finish before a slow worker still stuck on an earlier, longer
one. Without this, captions could arrive to the frontend out of sequence.

Segments are numbered as they're enqueued (SegmentQueue.push's `seq`).
Results are buffered here as they complete and only flushed to the socket
strictly in that order -- *unless* the segment currently being waited on
has been in flight (submitted to a worker, not yet finished) longer than
the acceptable-latency threshold, in which case it's skipped rather than
held up for, exactly like the existing delayed/preemption logic elsewhere
in this pipeline (frontend's DELAYED_FACTOR, this backend's
QUEUE_PREEMPTION_FACTOR) gives up waiting rather than blocking forever.
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import WebSocket

from backend.config import QUEUE_PREEMPTION_FACTOR
from backend.logging_config import log


class ResultSequencer:
    """One instance per WebSocket connection."""

    def __init__(self) -> None:
        self._next_seq = 0
        self._pending: dict[int, dict] = {}  # seq -> finished message, not yet its turn
        self._submitted_at: dict[int, float] = {}  # seq -> when a worker started on it
        self._abandoned: set[int] = set()  # seq known to never complete (preempted out of the queue)
        self._lock = asyncio.Lock()

    def mark_submitted(self, seq: int) -> None:
        """Call the instant a worker pops this segment and starts on it --
        this is the "time of submission" the in-flight timeout below is
        measured from."""
        self._submitted_at[seq] = time.monotonic()

    async def mark_abandoned(self, seqs: list[int], websocket: WebSocket, send_lock: asyncio.Lock) -> None:
        """Call with the seqs SegmentQueue.preempt_if_needed() just dropped
        -- those segments' audio is gone, so waiting on them would hang
        forever rather than eventually timing out. Known-abandoned, so
        this can flush past them immediately rather than waiting out the
        in-flight timeout for something that was never even submitted to a
        worker in the first place."""
        if not seqs:
            return
        async with self._lock:
            self._abandoned.update(seqs)
            await self._flush(websocket, send_lock)

    async def submit_result(
        self, seq: int, message: dict, acceptable_latency: float, websocket: WebSocket, send_lock: asyncio.Lock
    ) -> None:
        """Call once a worker finishes a segment (transcribe + translate
        done, message built) -- buffers it and flushes whatever's now
        eligible to send, in order."""
        async with self._lock:
            self._submitted_at.pop(seq, None)
            if seq < self._next_seq:
                # Already skipped as stale (or abandoned) while this was
                # still processing -- a late straggler behind a sequence
                # number that's already moved past it. Discard rather than
                # buffer forever: it would never get picked up (_flush only
                # ever advances next_seq), and displaying it now would be
                # exactly the out-of-order result this class exists to
                # prevent.
                log.info("Discarding late straggler result for seq=%d (next_seq=%d)", seq, self._next_seq)
                return
            self._pending[seq] = message
            await self._flush(websocket, send_lock, acceptable_latency)

    async def submit_idle(self, seq: int, message: dict, websocket: WebSocket, send_lock: asyncio.Lock) -> None:
        """Routes the "idle" transition (speech -> silence) through this
        same ordering mechanism, at the seq the queue's *next* push would
        get (i.e. "after everything pushed so far"). Sent directly instead
        (bypassing the sequencer entirely) was the original design, and it
        could race ahead of a still-in-flight segment's result: idle
        arrives and turns the status light off, then a slower segment's
        result -- say, cpu_status="red" from before audio stopped --
        arrives *after* it and turns the light back on, with nothing left
        to ever turn it off again since no more audio is coming. Unlike
        submit_result, a seq at-or-behind next_seq here means everything
        ahead of it is already resolved, so it sends immediately instead
        of being treated as a stale straggler to discard."""
        async with self._lock:
            if seq <= self._next_seq:
                message["timestamp"] = time.time()
                async with send_lock:
                    await websocket.send_text(json.dumps(message))
                return
            self._pending[seq] = message
            await self._flush(websocket, send_lock)

    async def tick(self, acceptable_latency: float, websocket: WebSocket, send_lock: asyncio.Lock) -> None:
        """Periodic nudge (see main.py's _sequencer_ticker) so a segment
        that's been in flight too long gets skipped promptly even if no
        *other* segment happens to complete right after it stalls --
        submit_result() alone only re-checks staleness when something new
        arrives to flush."""
        async with self._lock:
            await self._flush(websocket, send_lock, acceptable_latency)

    async def _flush(self, websocket: WebSocket, send_lock: asyncio.Lock, acceptable_latency: float | None = None) -> None:
        # Caller holds self._lock.
        while True:
            if self._next_seq in self._abandoned:
                self._abandoned.discard(self._next_seq)
                self._next_seq += 1
                continue

            if self._next_seq in self._pending:
                message = self._pending.pop(self._next_seq)
                message["timestamp"] = time.time()
                async with send_lock:
                    await websocket.send_text(json.dumps(message))
                self._next_seq += 1
                continue

            submitted_at = self._submitted_at.get(self._next_seq)
            if (
                submitted_at is not None
                and acceptable_latency is not None
                and time.monotonic() - submitted_at > QUEUE_PREEMPTION_FACTOR * acceptable_latency
            ):
                # Still being worked on somewhere, but it's taken too long
                # -- move on rather than holding up every segment after it.
                # Its result, if it ever arrives, lands in _pending under a
                # seq that's already behind next_seq and is simply never
                # picked up -- see submit_result, which never rewinds
                # next_seq backwards.
                log.info(
                    "Skipping seq=%d: in flight %.2fs > %.2fs threshold",
                    self._next_seq, time.monotonic() - submitted_at, QUEUE_PREEMPTION_FACTOR * acceptable_latency,
                )
                self._submitted_at.pop(self._next_seq, None)
                self._next_seq += 1
                continue

            break
