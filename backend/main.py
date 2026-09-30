"""FastAPI backend: a single /ws/transcribe WebSocket. Binary frames in are
16kHz mono int16 PCM; JSON frames out are transcript/translation/status
events:

    {"type": "partial"|"final"|"idle",
     "text": str, "detected_lang": str, "translated_text": str,
     "model_tier": "small"|"base"|"tiny",
     "cpu_status": "green"|"yellow"|"red"|"off",
     "segment_closed_at": float, "queue_length": int, "workers": int, "timestamp": float}

("workers" is PARALLEL_WORKERS, config.py -- static for the process's
lifetime, sent on every event since there's no dedicated handshake message
for it yet.)

("idle" events omit text/detected_lang/translated_text/segment_closed_at --
they only fire once on the transition into silence, so the frontend can
turn its CPU-strain light off without guessing from a timeout.)

Receiving audio and processing it run as two concurrent tasks (see
transcription/segment_queue.py) so a slow segment never blocks reading
more audio off the socket, and so backlog can be measured/preempted.

Control messages in (JSON text frames):
    {"type": "set_dst_lang", "lang": "<iso-639-1>"}
    {"type": "set_src_lang", "lang": "auto"|"<iso-639-1>"}
    {"type": "set_engine", "engine": "faster-whisper"|"parakeet"}  ("modal" is
        entered via start_modal_setup below, not set_engine directly)
    {"type": "set_tier", "tier": "auto"|"small"|"base"|"tiny"}  (faster-whisper only)
    {"type": "set_acceptable_latency", "seconds": float}
    {"type": "start_modal_setup", "token_id": str, "token_secret": str}  (both
        optional -- see modal_engine.py's deploy_and_warm_up; blank/omitted
        falls back to whatever ambient Modal auth the container has)
    {"type": "stop_modal"}
    {"type": "set_paused", "paused": bool}  (sent when the frontend's pause
        button toggles; see VadSegmenter.force_close -- without this the
        backend has no way to know audio stopped mid-speech)

Modal status events out (see transcription/modal_engine.py):
    {"type": "modal_setup_status",
     "status": "deploying"|"warming up"|"ready"|"alive"|"stopping"|"terminated",
     "error": str,  # only present on a "terminated" that followed a setup failure
     "timestamp": float}
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket

from backend.config import (
    CPU_THREADS_PER_WORKER,
    DEFAULT_ACCEPTABLE_LATENCY_S,
    DEFAULT_DST_LANG,
    DEFAULT_ENGINE,
    DEFAULT_TIER_MODE,
    MODEL_TIERS,
    PARALLEL_WORKERS,
)
from backend import log_viewer
from backend.logging_config import log
from backend.transcription.engine import AdaptiveEngine, get_model
from backend.transcription.modal_engine import ModalEngine
from backend.transcription.parakeet_engine import ParakeetEngine
from backend.transcription.result_sequencer import ResultSequencer
from backend.transcription.segment_queue import SegmentQueue
from backend.transcription.vad_segmenter import VadSegmenter
from backend.translation.translator import translate
import backend.translation.translator as translator_module

MODAL_HEARTBEAT_INTERVAL_S = 30


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Eagerly load the default-path models (faster-whisper's starting tier
    # + the NLLB translator, needed regardless of engine) at startup rather
    # than lazily on first use. Without this, /health returns 200 as soon
    # as uvicorn boots even though nothing has actually been downloaded or
    # loaded yet -- misleading for a launcher script polling /health as a
    # "ready" signal, and it hides multi-minute first-run model downloads
    # behind what looks like an already-running app. Parakeet/Modal are
    # deliberately left lazy: they're opt-in, not the default path.
    log.info(
        "Adaptive parallelism: %d concurrent worker(s), %d CPU thread(s) each.",
        PARALLEL_WORKERS, CPU_THREADS_PER_WORKER,
    )
    log.info("Warming up default models (faster-whisper small, NLLB-200 translator)...")
    await asyncio.to_thread(get_model, MODEL_TIERS[0])
    await asyncio.to_thread(translator_module._load)
    log.info("Models ready -- backend is fully warmed up.")
    yield


app = FastAPI(title="StreamScribe_adaptive backend", lifespan=lifespan)
app.include_router(log_viewer.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


class ConnectionState:
    """Shared, mutable state for one connection -- control messages update
    it; both the receiver and processor tasks read from it."""

    def __init__(self) -> None:
        self.dst_lang = DEFAULT_DST_LANG
        self.src_lang: str | None = None  # None = auto-detect
        self.acceptable_latency = DEFAULT_ACCEPTABLE_LATENCY_S
        self.engine_name = DEFAULT_ENGINE


async def _send_modal_status(websocket: WebSocket, send_lock: asyncio.Lock, status: str, error: str | None = None) -> None:
    message = {
        "type": "modal_setup_status",
        "status": status,
        "timestamp": time.time(),
    }
    if error:
        message["error"] = error
    async with send_lock:
        await websocket.send_text(json.dumps(message))


async def _modal_heartbeat(websocket: WebSocket, engines: dict, state: ConnectionState, send_lock: asyncio.Lock) -> None:
    """Runs for as long as this connection lives; only actually sends
    anything while Modal is the active, live engine -- exits quietly once
    it's been stopped or switched away from, rather than looping forever
    doing nothing."""
    modal_engine = engines["modal"]
    while state.engine_name == "modal" and modal_engine.is_active:
        await asyncio.sleep(MODAL_HEARTBEAT_INTERVAL_S)
        if state.engine_name == "modal" and modal_engine.is_active:
            await _send_modal_status(websocket, send_lock, "alive")


async def _handle_modal_setup(
    websocket: WebSocket, engines: dict, state: ConnectionState, send_lock: asyncio.Lock,
    token_id: str | None = None, token_secret: str | None = None,
) -> None:
    loop = asyncio.get_running_loop()

    def on_status(status: str) -> None:
        asyncio.run_coroutine_threadsafe(_send_modal_status(websocket, send_lock, status), loop)

    try:
        await asyncio.to_thread(engines["modal"].deploy_and_warm_up, on_status, token_id, token_secret)
        state.engine_name = "modal"
        asyncio.create_task(_modal_heartbeat(websocket, engines, state, send_lock))
    except Exception as exc:
        # Full traceback in the log, a concise message to the frontend --
        # silently reverting to "terminated" with no detail at all was the
        # actual bug report ("modal instance setup is failing"): the
        # failure was real but invisible, on both ends.
        log.exception("[Modal] setup failed")
        error_text = str(exc) or type(exc).__name__  # some exceptions str() to ""
        await _send_modal_status(websocket, send_lock, "terminated", error=error_text)


async def _handle_modal_stop(websocket: WebSocket, engines: dict, state: ConnectionState, send_lock: asyncio.Lock) -> None:
    # Switch away from Modal immediately, before teardown even starts, so
    # the processor loop can never hand a segment to a ModalEngine that's
    # mid-stop() -- local is the fallback the instant this begins, not
    # once teardown happens to finish.
    if state.engine_name == "modal":
        state.engine_name = DEFAULT_ENGINE
    await _send_modal_status(websocket, send_lock, "stopping")
    # to_thread, not a direct blocking call: stop() tears down the Modal
    # App/container context (__exit__), which can take a moment -- run
    # inline here, it would stall the receiver loop (and so stall reading
    # audio/control messages) for that whole time.
    await asyncio.to_thread(engines["modal"].stop)
    await _send_modal_status(websocket, send_lock, "terminated")


async def _send_idle(
    websocket: WebSocket, engines: dict, state: ConnectionState, queue: SegmentQueue,
    sequencer: ResultSequencer, send_lock: asyncio.Lock,
) -> None:
    # Through the sequencer (not sent directly), at the seq "one past
    # everything pushed so far" -- see ResultSequencer.submit_idle for why:
    # sent directly, this could race ahead of a still-in-flight segment's
    # result and get overwritten by it, leaving the status light showing
    # stale strain with nothing left to ever correct it once audio
    # actually stops.
    idle_message = {
        "type": "idle",
        "model_tier": engines[state.engine_name].model_tier,
        "cpu_status": "off",
        "queue_length": len(queue),
        "workers": PARALLEL_WORKERS,
    }
    await sequencer.submit_idle(queue.next_seq, idle_message, websocket, send_lock)


async def _receiver(
    websocket: WebSocket, segmenter: VadSegmenter, queue: SegmentQueue, state: ConnectionState,
    send_lock: asyncio.Lock, engines: dict, sequencer: ResultSequencer,
) -> None:
    was_speaking = False

    while True:
        message = await websocket.receive()

        # The raw receive() API delivers a disconnect as an ordinary
        # message (type "websocket.disconnect") rather than raising --
        # calling receive() again afterwards is what raises, and the
        # exact exception class for that has changed across
        # starlette versions (WebSocketDisconnect vs
        # WebSocketDisconnected), so checking the type explicitly here
        # is the version-independent way to exit cleanly.
        if message["type"] == "websocket.disconnect":
            break

        if message.get("bytes") is not None:
            for event in segmenter.push(message["bytes"]):
                queue.push(event)
                was_speaking = True
                log.info("Segment queued: seq=%d kind=%s queue_len=%d", queue.next_seq - 1, event.kind, len(queue))

            if was_speaking and not segmenter.in_speech:
                log.info("Speech -> silence, sending idle")
                await _send_idle(websocket, engines, state, queue, sequencer, send_lock)
                was_speaking = False

        elif message.get("text") is not None:
            control = json.loads(message["text"])
            control_type = control.get("type")
            log.info("Control message: %s", control)
            if control_type == "set_dst_lang":
                state.dst_lang = control.get("lang", state.dst_lang)
            elif control_type == "set_src_lang":
                lang = control.get("lang", "auto")
                state.src_lang = None if lang == "auto" else lang
            elif control_type == "set_acceptable_latency":
                state.acceptable_latency = float(control.get("seconds", state.acceptable_latency))
            elif control_type == "set_tier":
                engines["faster-whisper"].set_tier_mode(control.get("tier", "auto"))
            elif control_type == "set_engine":
                requested = control.get("engine", DEFAULT_ENGINE)
                if requested in ("faster-whisper", "parakeet"):
                    state.engine_name = requested
            elif control_type == "start_modal_setup":
                token_id = control.get("token_id") or None
                token_secret = control.get("token_secret") or None
                asyncio.create_task(_handle_modal_setup(websocket, engines, state, send_lock, token_id, token_secret))
            elif control_type == "stop_modal":
                asyncio.create_task(_handle_modal_stop(websocket, engines, state, send_lock))
            elif control_type == "set_paused":
                if control.get("paused", False):
                    # No more audio bytes are coming until resumed, so the
                    # normal "next frame arrives and notices speech ended"
                    # path (right above) can never fire on its own --
                    # without this, pausing mid-speech would leave
                    # in_speech stuck True and this connection would never
                    # emit another idle transition, stalling the frontend's
                    # queue/status light with nothing left to unstick them.
                    closed = segmenter.force_close()
                    if closed:
                        log.info("Paused mid-speech -- force-closing %d buffered segment(s)", len(closed))
                    for event in closed:
                        queue.push(event)
                        was_speaking = True
                    if was_speaking:
                        await _send_idle(websocket, engines, state, queue, sequencer, send_lock)
                        was_speaking = False


def _truncate(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


async def _processor(
    websocket: WebSocket, engines: dict, queue: SegmentQueue, state: ConnectionState,
    send_lock: asyncio.Lock, sequencer: ResultSequencer,
) -> None:
    while True:
        try:
            await _process_one_segment(websocket, engines, queue, state, send_lock, sequencer)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A single bad segment (a model error, a translation failure,
            # anything) must not permanently kill this worker -- before
            # this fix, any exception here ended the whole while loop
            # silently: the task just stops (nobody awaits or checks
            # these background tasks), leaving the connection permanently
            # short one worker for the rest of its life -- every worker
            # hitting this early on would mean nothing ever gets
            # transcribed again, with no error visible anywhere. The one
            # segment that failed simply isn't delivered: mark_submitted
            # was already called for it, so ResultSequencer's own
            # in-flight timeout ages it out on its own, same as any other
            # segment that's taking too long.
            log.exception("Processor worker hit an error on one segment -- continuing")
            # Guarantees this loop always yields back to the event loop on
            # every iteration, even if the failure happened before
            # _process_one_segment reached its first await (e.g. a bad
            # engines[...] lookup) -- without an await somewhere in this
            # branch, a failure that repeats with nothing ever awaiting
            # first would spin this task as a tight synchronous loop and
            # never yield, which starves every other task sharing this
            # event loop (found by this exact scenario while testing the
            # fix above, not theoretical). Also throttles a true failure
            # storm instead of retrying as fast as physically possible.
            await asyncio.sleep(0.2)


async def _process_one_segment(
    websocket: WebSocket, engines: dict, queue: SegmentQueue, state: ConnectionState,
    send_lock: asyncio.Lock, sequencer: ResultSequencer,
) -> None:
    active_engine = engines[state.engine_name]
    if isinstance(active_engine, AdaptiveEngine):
        # Give tier fallback a chance to drain the backlog on its own
        # before preempt_if_needed ever has to drop segments outright.
        active_engine.record_queue_length(len(queue))
    dropped_seqs = queue.preempt_if_needed(state.acceptable_latency)
    if dropped_seqs:
        log.info("Preempted %d stale queued segment(s): seqs=%s", len(dropped_seqs), dropped_seqs)
        # Those segments' audio is gone -- tell the sequencer now rather
        # than letting it wait out the in-flight timeout for something
        # that was never even submitted to a worker.
        await sequencer.mark_abandoned(dropped_seqs, websocket, send_lock)

    item = await queue.pop()
    # This segment's "time of submission" for the sequencer's
    # in-flight-too-long check below -- see result_sequencer.py.
    sequencer.mark_submitted(item.seq)
    # Re-resolved after the (possibly long) wait above, in case the
    # engine was switched while the queue was empty -- matches the
    # original pre-queue-length-tracking behavior.
    engine = engines[state.engine_name]

    start = time.monotonic()
    result = await asyncio.to_thread(engine.transcribe_segment, item.event.audio, 16000, state.src_lang)
    elapsed = time.monotonic() - start
    queue.record_processing_time(elapsed)
    log.info(
        "seq=%d kind=%s tier=%s cpu=%s elapsed=%.2fs queue_len=%d text=%r",
        item.seq, item.event.kind, result.model_tier, result.cpu_status, elapsed, len(queue),
        _truncate(result.text),
    )

    translated = await asyncio.to_thread(translate, result.text, result.detected_lang, state.dst_lang)

    # Built here (this segment's "time of completion"), but not sent
    # directly: with PARALLEL_WORKERS > 1 several segments finish out of
    # order (a fast worker on a short segment beats a slow worker still
    # stuck on an earlier, longer one), so actually dispatching it -- in
    # order, or skipping a segment that's taken too long -- is
    # ResultSequencer's job, not this loop's. See result_sequencer.py.
    message = {
        "type": item.event.kind,
        "text": result.text,
        "detected_lang": result.detected_lang,
        "translated_text": translated,
        "model_tier": result.model_tier,
        "cpu_status": result.cpu_status,
        "segment_closed_at": item.event.closed_at,
        "queue_length": len(queue),
        "workers": PARALLEL_WORKERS,
    }
    await sequencer.submit_result(item.seq, message, state.acceptable_latency, websocket, send_lock)


async def _sequencer_ticker(sequencer: ResultSequencer, state: ConnectionState, websocket: WebSocket, send_lock: asyncio.Lock) -> None:
    """Periodic nudge so a segment stuck in flight gets skipped promptly
    even if no *other* segment happens to complete right after it stalls
    -- submit_result() alone only re-checks staleness when something new
    arrives to flush. Mirrors the frontend's own _check_delayed polling
    (overlay_window.py, 250ms) in spirit."""
    while True:
        await asyncio.sleep(0.2)
        await sequencer.tick(state.acceptable_latency, websocket, send_lock)


@app.websocket("/ws/transcribe")
async def ws_transcribe(websocket: WebSocket) -> None:
    await websocket.accept()
    conn_id = uuid.uuid4().hex[:8]
    log.info("[%s] connection open", conn_id)

    engines = {
        "faster-whisper": AdaptiveEngine(tier_mode=DEFAULT_TIER_MODE),
        "parakeet": ParakeetEngine(),
        "modal": ModalEngine(),
    }
    segmenter = VadSegmenter()
    queue = SegmentQueue()
    state = ConnectionState()
    send_lock = asyncio.Lock()
    # Restores in-order delivery across PARALLEL_WORKERS concurrent
    # workers -- see result_sequencer.py.
    sequencer = ResultSequencer()

    receiver_task = asyncio.create_task(_receiver(websocket, segmenter, queue, state, send_lock, engines, sequencer))
    # PARALLEL_WORKERS concurrent consumers of the same queue, not one --
    # see config.py's PARALLEL_WORKERS docstring for why the count is
    # sized off the CPU actually available. Each awaits queue.pop()
    # independently, so a burst of segments gets processed several at a
    # time instead of strictly one after another.
    processor_tasks = [
        asyncio.create_task(_processor(websocket, engines, queue, state, send_lock, sequencer))
        for _ in range(PARALLEL_WORKERS)
    ]
    ticker_task = asyncio.create_task(_sequencer_ticker(sequencer, state, websocket, send_lock))

    try:
        await receiver_task
    except Exception:
        log.exception("[%s] receiver task crashed", conn_id)
    finally:
        for task in processor_tasks:
            task.cancel()
        ticker_task.cancel()
        # Belt-and-suspenders: if the connection drops without an explicit
        # stop_modal (e.g. the app crashes or loses network), don't leave
        # a billed Modal container running past this session.
        engines["modal"].stop()
        log.info("[%s] connection closed", conn_id)
