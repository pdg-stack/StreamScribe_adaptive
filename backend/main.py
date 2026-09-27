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
    {"type": "set_tier", "tier": "auto"|"small"|"base"|"tiny"}
    {"type": "set_acceptable_latency", "seconds": float}
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import FastAPI, WebSocket

from backend.config import DEFAULT_ACCEPTABLE_LATENCY_S, DEFAULT_DST_LANG, DEFAULT_TIER_MODE
from backend.transcription.engine import AdaptiveEngine
from backend.transcription.segment_queue import SegmentQueue
from backend.transcription.vad_segmenter import VadSegmenter
from backend.translation.translator import translate

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


async def _receiver(websocket: WebSocket, segmenter: VadSegmenter, queue: SegmentQueue, state: ConnectionState, send_lock: asyncio.Lock, engine: AdaptiveEngine) -> None:
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
                        "model_tier": engine.model_tier,
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
                engine.set_tier_mode(control.get("tier", "auto"))


async def _processor(websocket: WebSocket, engine: AdaptiveEngine, queue: SegmentQueue, state: ConnectionState, send_lock: asyncio.Lock) -> None:
    while True:
        queue.preempt_if_needed(state.acceptable_latency)
        item = await queue.pop()

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

    engine = AdaptiveEngine(tier_mode=DEFAULT_TIER_MODE)
    segmenter = VadSegmenter()
    queue = SegmentQueue()
    state = ConnectionState()
    send_lock = asyncio.Lock()

    receiver_task = asyncio.create_task(_receiver(websocket, segmenter, queue, state, send_lock, engine))
    processor_task = asyncio.create_task(_processor(websocket, engine, queue, state, send_lock))

    try:
        await receiver_task
    finally:
        processor_task.cancel()
