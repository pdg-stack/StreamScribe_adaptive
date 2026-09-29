"""WebSocket client: streams captured PCM to the backend and delivers
parsed transcript/translation/status JSON events back via callback.
Reconnects automatically if the backend isn't running yet or drops.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from collections.abc import Callable

import websockets

BACKEND_URI = "ws://127.0.0.1:8000/ws/transcribe"
RECONNECT_DELAY_S = 2


class WsClient:
    """Runs its own asyncio event loop on a background thread so the Qt
    main thread never blocks on network I/O. `send_audio()` and
    `set_dst_lang()` are safe to call from the Qt thread."""

    def __init__(
        self,
        on_event: Callable[[dict], None],
        on_connection_change: Callable[[bool], None] | None = None,
    ) -> None:
        self._on_event = on_event
        self._on_connection_change = on_connection_change
        self._audio_queue: queue.Queue[bytes] = queue.Queue()
        self._control_queue: queue.Queue[dict] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def send_audio(self, pcm_bytes: bytes) -> None:
        self._audio_queue.put(pcm_bytes)

    def set_dst_lang(self, lang: str) -> None:
        self._control_queue.put({"type": "set_dst_lang", "lang": lang})

    def set_src_lang(self, lang: str) -> None:
        self._control_queue.put({"type": "set_src_lang", "lang": lang})

    def set_engine(self, engine: str) -> None:
        self._control_queue.put({"type": "set_engine", "engine": engine})

    def set_tier(self, tier: str) -> None:
        self._control_queue.put({"type": "set_tier", "tier": tier})

    def set_acceptable_latency(self, seconds: float) -> None:
        self._control_queue.put({"type": "set_acceptable_latency", "seconds": seconds})

    def start_modal_setup(self, token_id: str = "", token_secret: str = "") -> None:
        self._control_queue.put({"type": "start_modal_setup", "token_id": token_id, "token_secret": token_secret})

    def stop_modal(self) -> None:
        self._control_queue.put({"type": "stop_modal"})

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._connect_loop())

    async def _connect_loop(self) -> None:
        while not self._stop.is_set():
            try:
                async with websockets.connect(BACKEND_URI) as ws:
                    if self._on_connection_change:
                        self._on_connection_change(True)
                    await asyncio.gather(self._sender(ws), self._receiver(ws))
            except (OSError, websockets.exceptions.WebSocketException):
                if self._on_connection_change:
                    self._on_connection_change(False)
                await asyncio.sleep(RECONNECT_DELAY_S)

    async def _sender(self, ws) -> None:
        while not self._stop.is_set():
            while not self._control_queue.empty():
                await ws.send(json.dumps(self._control_queue.get_nowait()))
            try:
                pcm = self._audio_queue.get_nowait()
                await ws.send(pcm)
            except queue.Empty:
                await asyncio.sleep(0.01)

    async def _receiver(self, ws) -> None:
        async for message in ws:
            self._on_event(json.loads(message))
