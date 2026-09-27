"""FastAPI backend: a single /ws/transcribe WebSocket. Binary frames in are
16kHz mono int16 PCM; JSON frames out are transcript/translation/status
events:

    {"type": "partial"|"final"|"idle",
     "text": str, "detected_lang": str, "translated_text": str,
     "model_tier": "small"|"base"|"tiny",
     "cpu_status": "green"|"yellow"|"red"|"off",
     "segment_closed_at": float, "queue_length": int, "timestamp": float}

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
    {"type": "start_modal_setup"}
    {"type": "stop_modal"}

Modal status events out (see transcription/modal_engine.py):
    {"type": "modal_setup_status",
     "status": "deploying"|"warming up"|"ready"|"alive"|"terminated",
     "timestamp": float}
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import FastAPI, WebSocket

from backend.config import DEFAULT_ACCEPTABLE_LATENCY_S, DEFAULT_DST_LANG, DEFAULT_ENGINE, DEFAULT_TIER_MODE
from backend.transcription.engine import AdaptiveEngine
from backend.transcription.modal_engine import ModalEngine
from backend.transcription.parakeet_engine import ParakeetEngine
from backend.transcription.segment_queue import SegmentQueue
from backend.transcription.vad_segmenter import VadSegmenter
from backend.translation.translator import translate

MODAL_HEARTBEAT_INTERVAL_S = 30

app = FastAPI(title="StreamScribe_adaptive backend")


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


async def _send_modal_status(websocket: WebSocket, send_lock: asyncio.Lock, status: str) -> None:
    async with send_lock:
        await websocket.send_text(json.dumps({
            "type": "modal_setup_status",
            "status": status,
            "timestamp": time.time(),
        }))


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


async def _handle_modal_setup(websocket: WebSocket, engines: dict, state: ConnectionState, send_lock: asyncio.Lock) -> None:
    loop = asyncio.get_running_loop()

    def on_status(status: str) -> None:
        asyncio.run_coroutine_threadsafe(_send_modal_status(websocket, send_lock, status), loop)

    try:
        await asyncio.to_thread(engines["modal"].deploy_and_warm_up, on_status)
        state.engine_name = "modal"
        asyncio.create_task(_modal_heartbeat(websocket, engines, state, send_lock))
    except Exception:
        await _send_modal_status(websocket, send_lock, "terminated")


async def _receiver(websocket: WebSocket, segmenter: VadSegmenter, queue: SegmentQueue, state: ConnectionState, send_lock: asyncio.Lock, engines: dict) -> None:
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

            if was_speaking and not segmenter.in_speech:
                async with send_lock:
                    await websocket.send_text(json.dumps({
                        "type": "idle",
                        "model_tier": engines[state.engine_name].model_tier,
                        "cpu_status": "off",
                        "queue_length": len(queue),
                        "timestamp": time.time(),
                    }))
                was_speaking = False

        elif message.get("text") is not None:
            control = json.loads(message["text"])
            control_type = control.get("type")
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
                asyncio.create_task(_handle_modal_setup(websocket, engines, state, send_lock))
            elif control_type == "stop_modal":
                engines["modal"].stop()
                if state.engine_name == "modal":
                    state.engine_name = DEFAULT_ENGINE
                await _send_modal_status(websocket, send_lock, "terminated")


async def _processor(websocket: WebSocket, engines: dict, queue: SegmentQueue, state: ConnectionState, send_lock: asyncio.Lock) -> None:
    while True:
        queue.preempt_if_needed(state.acceptable_latency)
        item = await queue.pop()
        engine = engines[state.engine_name]

        start = time.monotonic()
        result = await asyncio.to_thread(engine.transcribe_segment, item.event.audio, 16000, state.src_lang)
        elapsed = time.monotonic() - start
        queue.record_processing_time(elapsed)

        translated = await asyncio.to_thread(translate, result.text, result.detected_lang, state.dst_lang)

        async with send_lock:
            await websocket.send_text(json.dumps({
                "type": item.event.kind,
                "text": result.text,
                "detected_lang": result.detected_lang,
                "translated_text": translated,
                "model_tier": result.model_tier,
                "cpu_status": result.cpu_status,
                "segment_closed_at": item.event.closed_at,
                "queue_length": len(queue),
                "timestamp": time.time(),
            }))


@app.websocket("/ws/transcribe")
async def ws_transcribe(websocket: WebSocket) -> None:
    await websocket.accept()

    engines = {
        "faster-whisper": AdaptiveEngine(tier_mode=DEFAULT_TIER_MODE),
        "parakeet": ParakeetEngine(),
        "modal": ModalEngine(),
    }
    segmenter = VadSegmenter()
    queue = SegmentQueue()
    state = ConnectionState()
    send_lock = asyncio.Lock()

    receiver_task = asyncio.create_task(_receiver(websocket, segmenter, queue, state, send_lock, engines))
    processor_task = asyncio.create_task(_processor(websocket, engines, queue, state, send_lock))

    try:
        await receiver_task
    finally:
        processor_task.cancel()
        # Belt-and-suspenders: if the connection drops without an explicit
        # stop_modal (e.g. the app crashes or loses network), don't leave
        # a billed Modal container running past this session.
        engines["modal"].stop()
