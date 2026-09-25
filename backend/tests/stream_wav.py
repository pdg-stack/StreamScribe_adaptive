"""Standalone test harness for backend/main.py's /ws/transcribe WebSocket.
Streams a 16kHz mono WAV file to the backend in real-time-simulated chunks
and prints back transcript/translation/status events. Not part of the
shipped app -- a dev tool for verifying Sprint 1 end-to-end, including the
small->base->tiny fallback cascade under --fast/--repeat stress.

Requires: pip install -r backend/tests/requirements-dev.txt
Requires the backend running (docker compose up) and reachable at
ws://127.0.0.1:8000.

Usage:
    python -m backend.tests.stream_wav path/to/sample.wav
    python -m backend.tests.stream_wav path/to/sample.wav --dst hi
    python -m backend.tests.stream_wav path/to/sample.wav --fast --repeat 4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import wave

import websockets

FRAME_MS = 30
SAMPLE_RATE = 16000
FRAME_BYTES = int(SAMPLE_RATE * FRAME_MS / 1000) * 2  # int16 mono


async def stream_once(path: str, dst_lang: str, fast: bool, tag: str) -> None:
    with wave.open(path, "rb") as wav_file:
        if wav_file.getframerate() != SAMPLE_RATE or wav_file.getnchannels() != 1:
            raise SystemExit(
                f"{path}: expected {SAMPLE_RATE}Hz mono WAV, got "
                f"{wav_file.getframerate()}Hz/{wav_file.getnchannels()}ch"
            )
        pcm = wav_file.readframes(wav_file.getnframes())

    uri = "ws://127.0.0.1:8000/ws/transcribe"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type": "set_dst_lang", "lang": dst_lang}))

        async def sender() -> None:
            for i in range(0, len(pcm), FRAME_BYTES):
                await ws.send(pcm[i:i + FRAME_BYTES])
                if not fast:
                    await asyncio.sleep(FRAME_MS / 1000)
            # Trailing silence so the final segment actually closes.
            silence = b"\x00" * FRAME_BYTES
            for _ in range(30):
                await ws.send(silence)
                if not fast:
                    await asyncio.sleep(FRAME_MS / 1000)

        async def receiver() -> None:
            async for message in ws:
                print(f"[{tag}] {json.loads(message)}")

        recv_task = asyncio.create_task(receiver())
        await sender()
        await asyncio.sleep(2)  # drain trailing responses
        recv_task.cancel()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav_path")
    parser.add_argument("--dst", default="en", help="destination language (ISO 639-1)")
    parser.add_argument("--fast", action="store_true", help="send as fast as possible (stress test)")
    parser.add_argument("--repeat", type=int, default=1, help="concurrent streams (stress test)")
    args = parser.parse_args()

    await asyncio.gather(*[
        stream_once(args.wav_path, args.dst, args.fast, tag=f"stream-{i}")
        for i in range(args.repeat)
    ])


if __name__ == "__main__":
    asyncio.run(main())
