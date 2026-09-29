"""Entry point: wires WASAPI loopback capture -> WebSocket client ->
overlay UI. Runs natively on Windows, outside Docker -- Docker can't reach
host audio devices or the desktop compositor, so this process must run
directly on the host (see README/plan). Run as `python -m frontend.main`
from the repo root.
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import QObject, QSharedMemory, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication

from .audio_capture import LoopbackCapture
from .audio_source_detector import active_source_process
from .overlay_window import OverlayWindow
from .settings import Settings
from .ws_client import WsClient

SOURCE_POLL_MS = 1000
SINGLETON_KEY = "StreamScribe_adaptive_singleton"


class _EventBridge(QObject):
    """WsClient's callbacks fire from a background asyncio thread, and
    LoopbackCapture's from PortAudio's own thread; Qt widgets may only be
    touched from the GUI thread. Routing through a signal marshals each
    call onto the GUI thread's event loop safely."""

    event_received = pyqtSignal(dict)
    audio_activity = pyqtSignal()


def main() -> None:
    # Only one overlay at a time: QSharedMemory's create() fails if a
    # segment under this key already exists, which happens exactly when
    # another instance of this process is already running (the OS cleans
    # the segment up when that process exits, even if it crashes).
    singleton_guard = QSharedMemory(SINGLETON_KEY)
    if not singleton_guard.create(1):
        print("StreamScribe_adaptive is already running -- close it first.")
        sys.exit(1)

    print("[App] StreamScribe_adaptive starting...", flush=True)

    app = QApplication(sys.argv)
    settings = Settings.load()
    bridge = _EventBridge()

    def on_connection_change(connected: bool) -> None:
        print(f"[Backend] {'connected' if connected else 'disconnected -- retrying...'}", flush=True)

    ws_client = WsClient(on_event=bridge.event_received.emit, on_connection_change=on_connection_change)

    def on_src_lang_change(code: str) -> None:
        ws_client.set_src_lang(code)

    def on_dest_lang_change(code: str) -> None:
        ws_client.set_dst_lang(code)

    def on_engine_change(engine: str) -> None:
        print(f"[Engine] switched to: {engine}", flush=True)
        ws_client.set_engine(engine)

    def on_tier_change(tier: str) -> None:
        print(f"[Engine] model size set to: {tier}", flush=True)
        ws_client.set_tier(tier)

    def on_modal_setup_requested(token_id: str, token_secret: str) -> None:
        print("[Modal] setup requested...", flush=True)
        ws_client.start_modal_setup(token_id, token_secret)

    def on_modal_stop_requested() -> None:
        print("[Modal] stop requested...", flush=True)
        ws_client.stop_modal()

    # A single mutable attribute, read on PortAudio's own capture thread and
    # written from the Qt thread on a button click -- CPython's GIL makes a
    # plain bool read/write like this safe without extra locking.
    audio_state = type("AudioState", (), {"paused": False})()

    def on_pause_toggled(paused: bool) -> None:
        audio_state.paused = paused

    def on_audio_captured(pcm_bytes: bytes) -> None:
        if not audio_state.paused:
            ws_client.send_audio(pcm_bytes)

    def on_audio_activity() -> None:
        if not audio_state.paused:
            bridge.audio_activity.emit()

    overlay = OverlayWindow(
        settings,
        on_src_lang_change=on_src_lang_change,
        on_dest_lang_change=on_dest_lang_change,
        on_close=app.quit,
        on_engine_change=on_engine_change,
        on_tier_change=on_tier_change,
        on_latency_change=ws_client.set_acceptable_latency,
        on_modal_setup_requested=on_modal_setup_requested,
        on_modal_stop_requested=on_modal_stop_requested,
        on_pause_toggled=on_pause_toggled,
    )
    bridge.event_received.connect(overlay.handle_event)
    bridge.audio_activity.connect(overlay.pulse_listening)
    overlay.show()

    ws_client.set_src_lang(settings.src_language)
    ws_client.set_dst_lang(settings.dest_language)
    ws_client.set_engine(settings.engine)
    if settings.engine == "faster-whisper":
        ws_client.set_tier(settings.tier)
    ws_client.set_acceptable_latency(settings.acceptable_latency_s)
    ws_client.start()

    capture = LoopbackCapture(on_audio=on_audio_captured, on_activity=on_audio_activity)
    capture.start()

    source_timer = QTimer()
    source_timer.timeout.connect(lambda: overlay.set_source_app(active_source_process()))
    source_timer.start(SOURCE_POLL_MS)

    def on_app_quit() -> None:
        print("[App] StreamScribe_adaptive closing...", flush=True)

    app.aboutToQuit.connect(on_app_quit)
    app.aboutToQuit.connect(capture.stop)
    app.aboutToQuit.connect(ws_client.stop)

    print("[App] StreamScribe_adaptive started -- overlay is up, listening for system audio.", flush=True)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
