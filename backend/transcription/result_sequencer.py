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

PLAIN-ENGLISH OVERVIEW (for anyone new to this file):
Think of it like a numbered ticket queue at a counter, e.g. "now serving
#4". Segments get a number when they're first captured (#0, #1, #2...).
Because more than one segment can be transcribed at once, #2 might finish
before #1 does. This class holds #2's answer back ("pending") until #1's
answer arrives too, so they still go out to the app's screen in the right
order (#1 then #2), never #2 before #1.
The one exception: if #1 is taking way too long (past the acceptable-delay
setting), we stop waiting for it, skip straight to serving #2, and just
throw #1's answer away whenever/if it eventually shows up -- better a
short gap than freezing everything behind one slow segment forever.
The periodic "ticker" (see main.py's _sequencer_ticker, called every 0.2s)
is what actually notices "#1 has been waiting too long" even when nothing
else is happening -- if that ticker ever stops running, the whole queue
can appear to freeze even though individual segments are still being
transcribed fine behind the scenes.
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import WebSocket

from backend.config import QUEUE_PREEMPTION_FACTOR
from backend.logging_config import log

# How long a single websocket.send_text() may take before we give up on it.
# _flush() below sends WHILE holding self._lock (see its docstring), so an
# indefinitely hanging send -- a stalled network connection that hasn't
# been noticed as dead yet -- would freeze every future call into this
# class forever, not just this one message. A timeout turns that into "one
# send fails, gets logged, the connection tears down normally" instead.
SEND_TIMEOUT_S = 5.0


async def _send(websocket: WebSocket, message: dict) -> None:
    await asyncio.wait_for(websocket.send_text(json.dumps(message)), timeout=SEND_TIMEOUT_S)


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
                    await _send(websocket, message)
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
        # In plain terms: "keep serving the next ticket number for as long
        # as we can." Each pass through this loop handles ONE outcome for
        # whatever self._next_seq currently is, then either moves on to the
        # next number (continue) or stops because that number genuinely
        # isn't ready yet (break) -- there's nothing more useful to do
        # until either a new result comes in or the ticker calls this again.
        while True:
            if self._next_seq in self._abandoned:
                self._abandoned.discard(self._next_seq)
                self._next_seq += 1
                continue

            if self._next_seq in self._pending:
                message = self._pending.pop(self._next_seq)
                message["timestamp"] = time.time()
                async with send_lock:
                    await _send(websocket, message)
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
