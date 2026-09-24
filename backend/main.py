"""FastAPI backend: a single /ws/transcribe WebSocket. Binary frames in are
16kHz mono int16 PCM; JSON frames out are transcript/translation/status
events:

    {"type": "partial"|"final"|"idle",
     "text": str, "detected_lang": str, "translated_text": str,
     "model_tier": "small"|"base"|"tiny",
     "cpu_status": "green"|"yellow"|"red"|"off",
     "timestamp": float}

("idle" events omit text/detected_lang/translated_text -- they only fire
once on the transition into silence, so the frontend can turn its
CPU-strain light off without guessing from a timeout.)

Control messages in (JSON text frames):
    {"type": "set_dst_lang", "lang": "<iso-639-1>"}
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import FastAPI, WebSocket

from backend.config import DEFAULT_DST_LANG
from backend.transcription.engine import AdaptiveEngine
from backend.transcription.vad_segmenter import VadSegmenter
from backend.translation.translator import translate

app = FastAPI(title="StreamScribe_fwhisper backend")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.websocket("/ws/transcribe")
async def ws_transcribe(websocket: WebSocket) -> None:
    await websocket.accept()
    engine = AdaptiveEngine()
    segmenter = VadSegmenter()
    dst_lang = DEFAULT_DST_LANG
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
                # transcribe/translate are CPU-bound and synchronous
                # (model downloads, faster-whisper, ctranslate2) -- run
                # them off the event loop so the server stays responsive
                # (health checks, other connections) while they run.
                result = await asyncio.to_thread(engine.transcribe_segment, event.audio)
                translated = await asyncio.to_thread(translate, result.text, result.detected_lang, dst_lang)
                await websocket.send_text(json.dumps({
                    "type": event.kind,
                    "text": result.text,
                    "detected_lang": result.detected_lang,
                    "translated_text": translated,
                    "model_tier": result.model_tier,
                    "cpu_status": result.cpu_status,
                    "timestamp": time.time(),
                }))
                was_speaking = True

            if was_speaking and not segmenter.in_speech:
                await websocket.send_text(json.dumps({
                    "type": "idle",
                    "model_tier": engine.model_tier,
                    "cpu_status": "off",
                    "timestamp": time.time(),
                }))
                was_speaking = False

        elif message.get("text") is not None:
            control = json.loads(message["text"])
            if control.get("type") == "set_dst_lang":
                dst_lang = control.get("lang", dst_lang)
