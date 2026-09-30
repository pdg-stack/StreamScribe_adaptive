"""Entry point: wires WASAPI loopback capture -> WebSocket client ->
overlay UI. Runs natively on Windows, outside Docker -- Docker can't reach
host audio devices or the desktop compositor, so this process must run
directly on the host (see README/plan). Run as `python -m frontend.main`
from the repo root.

PLAIN-ENGLISH OVERVIEW (for anyone new to this file):
This is the file that starts the whole app and connects its three main
pieces together, like plugging cables between them:
  1. `capture` (audio_capture.py) grabs audio straight from Windows.
  2. `ws_client` (ws_client.py) sends that audio to the backend over the
     network and receives back the transcribed/translated text.
  3. `overlay` (overlay_window.py) is the actual floating window you see
     on screen, showing that text.
The small `on_*` functions in main() below (on_audio_captured,
on_audio_activity, etc.) are the "cables" -- each one just takes
something that happened in one piece and hands it to the next piece.
Nothing complicated happens in this file itself; it just introduces the
pieces to each other and then starts Qt's main loop (`app.exec()`), which
is what actually keeps the window alive and responsive until you close it.
"""

from __future__ import annotations

import sys
import threading

from PyQt6.QtCore import QObject, QSharedMemory, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication

from .audio_capture import LoopbackCapture
from .audio_source_detector import active_source_process
from .logging_config import log
from .overlay_window import OverlayWindow
from .settings import Settings
from .ws_client import WsClient

SOURCE_POLL_MS = 1000
SINGLETON_KEY = "StreamScribe_adaptive_singleton"
# PortAudio's capture callback (audio_capture.py) fires continuously every
# ~20-60ms as long as the stream is alive, silence included -- it's driven
# by the sound hardware finishing a buffer, not by anyone actually
# speaking. So going several seconds with zero callbacks is a strong,
# direct signal that capture itself has stalled, as opposed to "nobody's
# spoken in a while," which looks completely different (callbacks keep
# firing, the VAD just doesn't see speech in any of them). Checked every
# CAPTURE_WATCHDOG_INTERVAL_MS; a gap past CAPTURE_STALL_THRESHOLD_S logs a
# warning so a future "it just stopped working" report has hard evidence
# either way instead of only a guess.
CAPTURE_WATCHDOG_INTERVAL_MS = 5000
CAPTURE_STALL_THRESHOLD_S = 3.0


def _log_uncaught_exception(exc_type, exc_value, exc_tb) -> None:
    # The console window (and its scrollback) closes the instant the app
    # exits, including on a crash -- an uncaught exception's traceback
    # would otherwise only ever exist in that vanishing window, not the
    # log file, since Python's default excepthook writes straight to
    # stderr and nothing else.
    log.critical("Uncaught exception -- app is about to exit", exc_info=(exc_type, exc_value, exc_tb))
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _log_uncaught_thread_exception(args: threading.ExceptHookArgs) -> None:
    # sys.excepthook only covers the main thread -- PortAudio's capture
    # callback and ws_client's own connect-loop thread wrapper run on
    # their own threads, where an uncaught exception would otherwise just
    # print to stderr and silently end that thread (audio capture or all
    # networking) for the rest of the session.
    log.critical(
        "Uncaught exception on thread %r", args.thread.name if args.thread else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )
    threading.__excepthook__(args)


sys.excepthook = _log_uncaught_exception
threading.excepthook = _log_uncaught_thread_exception


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
        log.warning("StreamScribe_adaptive is already running -- close it first.")
        sys.exit(1)

    log.info("[App] StreamScribe_adaptive starting...")

    app = QApplication(sys.argv)
    settings = Settings.load()
    bridge = _EventBridge()

    def on_connection_change(connected: bool) -> None:
        log.info("[Backend] %s", "connected" if connected else "disconnected -- retrying...")

    ws_client = WsClient(on_event=bridge.event_received.emit, on_connection_change=on_connection_change)

    def on_src_lang_change(code: str) -> None:
        ws_client.set_src_lang(code)

    def on_dest_lang_change(code: str) -> None:
        ws_client.set_dst_lang(code)

    def on_engine_change(engine: str) -> None:
        log.info("[Engine] switched to: %s", engine)
        ws_client.set_engine(engine)

    def on_tier_change(tier: str) -> None:
        log.info("[Engine] model size set to: %s", tier)
        ws_client.set_tier(tier)

    def on_modal_setup_requested(token_id: str, token_secret: str) -> None:
        log.info("[Modal] setup requested...")
        ws_client.start_modal_setup(token_id, token_secret)

    def on_modal_stop_requested() -> None:
        log.info("[Modal] stop requested...")
        ws_client.stop_modal()

    # A single mutable attribute, read on PortAudio's own capture thread and
    # written from the Qt thread on a button click -- CPython's GIL makes a
    # plain bool read/write like this safe without extra locking.
    audio_state = type("AudioState", (), {"paused": False})()

    def on_pause_toggled(paused: bool) -> None:
        audio_state.paused = paused
        ws_client.set_paused(paused)

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

    # See CAPTURE_STALL_THRESHOLD_S above -- `stalled` remembers whether
    # we've already warned about the CURRENT gap, so a long stall logs
    # once (not every 5s) and a recovery gets its own log line too.
    capture_watchdog_state = type("CaptureWatchdogState", (), {"stalled": False})()

    def on_capture_watchdog() -> None:
        gap = capture.seconds_since_last_callback()
        if gap > CAPTURE_STALL_THRESHOLD_S and not capture_watchdog_state.stalled:
            capture_watchdog_state.stalled = True
            log.warning("Audio capture callback hasn't fired in %.1fs -- capture may have stalled", gap)
        elif gap <= CAPTURE_STALL_THRESHOLD_S and capture_watchdog_state.stalled:
            capture_watchdog_state.stalled = False
            log.info("Audio capture callback resumed firing normally")

    capture_watchdog = QTimer()
    capture_watchdog.timeout.connect(on_capture_watchdog)
    capture_watchdog.start(CAPTURE_WATCHDOG_INTERVAL_MS)

    def on_app_quit() -> None:
        log.info("[App] StreamScribe_adaptive closing...")

    app.aboutToQuit.connect(on_app_quit)
    app.aboutToQuit.connect(capture.stop)
    app.aboutToQuit.connect(ws_client.stop)

    log.info("[App] StreamScribe_adaptive started -- overlay is up, listening for system audio.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
