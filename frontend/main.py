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

    app = QApplication(sys.argv)
    settings = Settings.load()
    bridge = _EventBridge()

    ws_client = WsClient(on_event=bridge.event_received.emit)

    def on_src_lang_change(code: str) -> None:
        ws_client.set_src_lang(code)

    def on_dest_lang_change(code: str) -> None:
        ws_client.set_dst_lang(code)

    overlay = OverlayWindow(
        settings,
        on_src_lang_change=on_src_lang_change,
        on_dest_lang_change=on_dest_lang_change,
        on_close=app.quit,
        on_engine_change=ws_client.set_engine,
        on_tier_change=ws_client.set_tier,
        on_latency_change=ws_client.set_acceptable_latency,
        on_modal_setup_requested=ws_client.start_modal_setup,
        on_modal_stop_requested=ws_client.stop_modal,
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

    capture = LoopbackCapture(on_audio=ws_client.send_audio, on_activity=bridge.audio_activity.emit)
    capture.start()

    source_timer = QTimer()
    source_timer.timeout.connect(lambda: overlay.set_source_app(active_source_process()))
    source_timer.start(SOURCE_POLL_MS)

    app.aboutToQuit.connect(capture.stop)
    app.aboutToQuit.connect(ws_client.stop)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
