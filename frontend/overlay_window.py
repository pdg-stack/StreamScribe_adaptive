"""Transparent, always-on-top floating overlay -- the core UI. Layout
targets the ViiTor Translate reference screenshot (see plan): a slim pill
toolbar (a "language selector" chip -- source picker with an Auto Detect
option, swap icon, destination picker -- a CPU-strain light, settings gear,
close) above a semi-transparent caption panel. Partial-result text updates
in place (no layout jump), rate-limited by the refresh-speed setting so
rapid partial updates don't visibly flicker. The model-tier badge and
source-app label are diagnostic detail, not everyday UI -- they live only
in the advanced pane (see plan's Advanced mode), hidden by default.
"""

from __future__ import annotations

import time

from PyQt6.QtCore import QEvent, QPoint, Qt, QTimer
from PyQt6.QtGui import QColor, QCursor, QFont, QMouseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizeGrip,
    QVBoxLayout,
    QWidget,
)

from .caption_icons import LineIconButton
from .caption_view import CaptionView
from .language_dropdown import LanguageDropdown
from .logging_config import log
from .settings import Settings
from .settings_dialog import SettingsDialog
from .transcript_store import TranscriptStore

STRAIN_COLORS = {
    "off": "#555555",
    "green": "#3fbf50",
    "yellow": "#e0b400",
    "red": "#e0392b",
}

# How long the status light stays "on" after the most recent detected
# activity pulse before reverting to "off" -- see pulse_listening().
LISTENING_TIMEOUT_MS = 5000

# Grace period between the cursor leaving the overlay and the auto-hidden
# header/footer actually fading out -- see _update_hover_state().
HIDE_DELAY_MS = 300

# Safety-net poll interval for _update_hover_state -- see its own timer
# setup in __init__ for why this exists: Qt's Enter/Leave delivery on a
# frameless, always-on-top, click-through-toggling window like this one is
# a known-flaky spot (confirmed as the cause of "header won't auto-hide,
# intermittently" -- a single missed Leave event left _is_hovering stuck
# True forever, since nothing else ever re-checked it). Cheap enough
# (one QCursor.pos() + one rect containment test) to just always run.
HOVER_POLL_MS = 500

# How often _check_delayed polls for staleness, and the multiple of the
# acceptable-latency setting past which a silent pipeline counts as
# "delayed" -- mirrors the backend's own QUEUE_PREEMPTION_FACTOR (1.2), the
# same threshold at which the backend itself starts dropping stale backlog.
DELAYED_CHECK_INTERVAL_MS = 250
DELAYED_FACTOR = 1.2

# How long a finalized entry (or a <delayed> marker) waits before actually
# being written to the transcript history DB -- see transcript_store.py's
# docstring for why this is a deliberate settle margin, not a wait for
# more text.
TRANSCRIPT_STORE_DEBOUNCE_MS = 1500

# ISO 639-1 codes for faster-whisper's source AND destination pickers (both
# filtered identically by the active engine -- see _source_languages_for_
# engine). The source picker additionally gets an "Auto Detect" entry (see
# plan's Translation section) prepended in _build_toolbar.
LANGUAGES = [
    ("en", "English"), ("hi", "Hindi"), ("es", "Spanish"), ("fr", "French"),
    ("de", "German"), ("zh", "Chinese"), ("ja", "Japanese"), ("ko", "Korean"),
    ("ar", "Arabic"), ("pt", "Portuguese"), ("ru", "Russian"), ("it", "Italian"),
    ("tr", "Turkish"), ("nl", "Dutch"), ("pl", "Polish"), ("vi", "Vietnamese"),
    ("th", "Thai"), ("id", "Indonesian"), ("ur", "Urdu"), ("bn", "Bengali"),
]
LANGUAGE_NAMES = dict(LANGUAGES)
# Parakeet TDT covers only these (see plan's ASR research) -- no Chinese/
# Japanese, unlike faster-whisper's full list above.
PARAKEET_LANGUAGE_CODES = {"en", "es", "ru", "it", "pt"}
AUTO_CODE = "auto"
DEFAULT_DEST_LANGUAGE = "en"  # mirrors Settings.dest_language's default


class OverlayWindow(QWidget):
    def __init__(
        self,
        settings: Settings,
        on_src_lang_change,
        on_dest_lang_change,
        on_close,
        on_engine_change,
        on_tier_change,
        on_latency_change,
        on_modal_setup_requested,
        on_modal_stop_requested,
        on_pause_toggled,
    ) -> None:
        super().__init__()
        self.settings = settings
        self._on_src_lang_change = on_src_lang_change
        self._on_dest_lang_change = on_dest_lang_change
        self._on_close = on_close
        self._on_pause_toggled = on_pause_toggled
        self._settings_actions = {
            "on_engine_change": on_engine_change,
            "on_tier_change": on_tier_change,
            "on_latency_change": on_latency_change,
            "on_modal_setup_requested": on_modal_setup_requested,
            "on_modal_stop_requested": on_modal_stop_requested,
        }
        self._settings_panel: SettingsDialog | None = None
        # eventFilter unconditionally references these (built by the
        # _build_* methods below) -- Qt can dispatch an event through an
        # installed filter synchronously, mid-construction, before all of
        # them exist yet. An AttributeError escaping eventFilter (a Qt
        # virtual-override callback) doesn't raise cleanly -- it corrupts
        # Qt's internal state and crashes the process natively. Placeholder
        # None values here mean eventFilter's own `obj in (...)` /
        # attribute-access checks are always safe, however early they fire.
        self._toolbar = None
        self._caption_panel = None
        self._advanced_pane = None
        self.caption_view = None
        self.copy_btn = None
        self.clear_btn = None
        self.settings_btn = None
        self.modal_status = "terminated"
        self._current_status = "off"
        self._current_tier = ""
        self._current_source_app: str | None = None
        self._last_queue_length = 0
        self._last_worker_count: int | None = None  # unknown until the backend's first event
        self._last_known_engine = settings.engine
        self._last_partial_update = 0.0
        # Raw content, not pre-rendered paragraphs: _entry_paragraphs()
        # renders each entry fresh at *render* time (every
        # _render_caption_view() call), so a live style change (font/
        # background/outline color) repaints existing lines immediately
        # instead of only affecting whatever caption arrives next.
        self._caption_entries: list[dict] = []  # finalized entries
        self._current_partial_entry: dict | None = None
        self._drag_offset: QPoint | None = None
        self._is_hovering = False

        # When the current in-progress utterance's first partial arrived --
        # its "start" time for the transcript-history DB, since no single
        # backend event ever carries the segment's true start, only when it
        # closed (see _record_caption_event). None between utterances.
        self._pending_utterance_started_at: float | None = None
        # Whether a <delayed> streak is currently active, independent of
        # persist_subtitles -- used only to dedupe transcript-history DB
        # writes to one row per continuous delayed streak (see
        # _show_delayed_marker), separate from that on-screen dedup check.
        self._delayed_marker_active = False
        try:
            self._transcript_store: TranscriptStore | None = TranscriptStore()
        except Exception:
            # A history DB the app can't open (disk full, permissions,
            # anything) must never block live transcription -- log it once
            # and just skip persistence for the session instead.
            log.exception("Could not open transcript history DB -- history won't be recorded this session")
            self._transcript_store = None

        self._listening_timer = QTimer(self)
        self._listening_timer.setSingleShot(True)
        self._listening_timer.timeout.connect(self._on_listening_timeout)

        self._hide_delay_timer = QTimer(self)
        self._hide_delay_timer.setSingleShot(True)
        self._hide_delay_timer.timeout.connect(self._commit_hide)

        # See HOVER_POLL_MS above -- event-driven Enter/Leave tracking alone
        # can get permanently stuck if Qt ever fails to deliver one Leave
        # event; this periodically re-derives the true state instead of
        # trusting that every event arrives.
        self._hover_poll_timer = QTimer(self)
        self._hover_poll_timer.timeout.connect(self._update_hover_state)
        self._hover_poll_timer.start(HOVER_POLL_MS)

        # See _check_delayed(): the backend doesn't expose a per-segment
        # "now waiting" signal (only the eventual result, if it isn't
        # preempted away), so this approximates "a segment is overdue" by
        # watching how long it's been since *any* transcript event arrived
        # while audio is actively playing.
        self._last_result_at = time.time()
        self._delayed_timer = QTimer(self)
        self._delayed_timer.timeout.connect(self._check_delayed)
        self._delayed_timer.start(DELAYED_CHECK_INTERVAL_MS)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(520, 220)
        if settings.window_x is not None and settings.window_y is not None:
            self.move(settings.window_x, settings.window_y)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_toolbar())
        root.addWidget(self._build_caption_panel(), stretch=1)
        root.addWidget(self._build_advanced_pane())

        # Enter/Leave on the toolbar or footer specifically (not just self)
        # also needs to reach _update_hover_state -- see its docstring for
        # why (moving onto a child widget fires a spurious Leave on self).
        self._toolbar.installEventFilter(self)
        self._advanced_pane.installEventFilter(self)

        self._apply_style()
        self._apply_auto_hide()
        # The very first _reposition_caption_overlays() call (end of
        # _build_caption_panel) runs before this window's layout has
        # actually settled -- at that point captionPanel isn't parented
        # into root yet, so caption_view still has some arbitrary pre-
        # layout size, not its real one. A later resize (the user
        # resizing the window) correctly re-triggers it via the
        # QEvent.Resize eventFilter path, but nothing does for the
        # *initial* settling, leaving the copy/clear icons and resize
        # grip stuck at positions computed from that wrong early size
        # (confirmed: calling this again with the real size immediately
        # corrects it). Deferring one event-loop turn runs it after
        # show()'s layout pass has actually finished.
        QTimer.singleShot(0, self._reposition_caption_overlays)

    # -- toolbar -----------------------------------------------------
    def _source_languages_for_engine(self, engine: str) -> list[tuple[str, str]]:
        if engine == "parakeet":
            return [(code, name) for code, name in LANGUAGES if code in PARAKEET_LANGUAGE_CODES]
        return LANGUAGES

    def _build_toolbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("toolbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 6, 10, 6)
        self._toolbar = bar
        # Auto-hide fades opacity rather than calling setVisible(False), so
        # the toolbar keeps its layout slot and the caption panel below it
        # never resizes when the header hides/shows.
        self._toolbar_opacity = QGraphicsOpacityEffect(bar)
        bar.setGraphicsEffect(self._toolbar_opacity)

        self.source_combo = LanguageDropdown()
        self.source_combo.setObjectName("langCombo")
        self.source_combo.addItem("Auto Detect", userData=AUTO_CODE)
        for code, name in self._source_languages_for_engine(self.settings.engine):
            self.source_combo.addItem(name, userData=code)
        self.source_combo.set_current_code(self.settings.src_language)
        self.source_combo.codeChanged.connect(self._handle_src_change)

        swap_btn = QPushButton("⇄")  # swap arrows
        swap_btn.setObjectName("swapButton")
        swap_btn.setToolTip("Swap source/destination language")
        swap_btn.clicked.connect(self._handle_swap)

        self.dest_combo = LanguageDropdown()
        self.dest_combo.setObjectName("langCombo")
        for code, name in self._source_languages_for_engine(self.settings.engine):
            self.dest_combo.addItem(name, userData=code)
        self.dest_combo.set_current_code(self.settings.dest_language)
        self.dest_combo.codeChanged.connect(self._handle_dest_change)

        # Source picker, swap, destination picker grouped into one visual
        # "language selector" chip -- distinct from the status/settings/close
        # icons on the right.
        lang_selector = QFrame()
        lang_selector.setObjectName("languageSelector")
        lang_layout = QHBoxLayout(lang_selector)
        lang_layout.setContentsMargins(8, 2, 8, 2)
        lang_layout.setSpacing(4)
        for w in (self.source_combo, swap_btn, self.dest_combo):
            lang_layout.addWidget(w)

        # "▮▮" (pause, two bars) / "▶" (play/resume), both
        # from the Geometric Shapes block -- the previous pause glyph
        # ("⏸", Miscellaneous Technical) is a different Unicode block
        # that a lot of fonts render with an emoji-style presentation,
        # visually mismatched against the plain triangle used for play.
        self.pause_btn = QPushButton("▮▮")  # pause/resume audio capture
        self.pause_btn.setObjectName("iconButton")
        self.pause_btn.setCheckable(True)
        self.pause_btn.setToolTip("Pause audio capture")
        self.pause_btn.toggled.connect(self._handle_pause_toggle)

        self.status_dot = QLabel("●")  # status light
        self.status_dot.setObjectName("statusDot")
        self._set_status("off")

        self.settings_btn = QPushButton("⚙")  # gear
        self.settings_btn.setObjectName("iconButton")
        self.settings_btn.clicked.connect(self._toggle_settings_panel)

        close_btn = QPushButton("✕")  # close
        close_btn.setObjectName("closeButton")
        close_btn.clicked.connect(self._handle_close)

        layout.addWidget(lang_selector)
        layout.addStretch(1)
        for w in (self.pause_btn, self.status_dot, self.settings_btn, close_btn):
            layout.addWidget(w)

        return bar

    def _build_caption_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("captionPanel")
        self._caption_panel = panel
        panel.installEventFilter(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        # Custom-painted (see caption_view.py), not QTextEdit/rich-text:
        # that's what makes a true per-glyph text outline and an
        # independent font-background rectangle possible at all. Its own
        # scrollbar (always visible, permanently reserved width) already
        # spans this widget's full height with nothing extra needed.
        self.caption_view = CaptionView()
        self.caption_view.setObjectName("captionView")
        self.caption_view.installEventFilter(self)
        layout.addWidget(self.caption_view, stretch=1)

        # Copy/clear -- top-right corner of the text field, floating over
        # it as plain children (NOT part of any layout), so toggling their
        # visibility on hover never reflows/resizes the caption area or
        # shifts the scroll position the way it did as layout items.
        self.copy_btn = LineIconButton("copy", color="#dddddd", parent=self.caption_view)
        self.copy_btn.setToolTip("Copy transcript")
        self.copy_btn.clicked.connect(self._handle_copy_transcript)
        self.copy_btn.setVisible(False)

        self.clear_btn = LineIconButton("clear", color="#e0392b", parent=self.caption_view)
        self.clear_btn.setToolTip("Clear transcript")
        self.clear_btn.clicked.connect(self._handle_clear_transcript)
        self.clear_btn.setVisible(False)

        # Also a plain child of caption_view, not a layout row below it --
        # "a frame with the text box and resizer, and the scrollbar in the
        # parent frame spanning the whole vertical height of the text
        # box": the resizer sits inset in caption_view's own bottom-right
        # corner, just to the left of its scrollbar, rather than in a
        # separate row that would otherwise shrink the scrollbar's span.
        # QSizeGrip resolves which *window* to resize by walking up from
        # its parent to the nearest top-level, so parenting it to
        # caption_view (not self) still resizes the overlay correctly.
        self.size_grip = QSizeGrip(self.caption_view)

        self._reposition_caption_overlays()
        return panel

    def _build_advanced_pane(self) -> QFrame:
        # Diagnostic detail hidden from the default view (see plan's
        # Advanced mode), 2 rows x 4 columns: queue/threads/delay/source
        # app, then host/model/size -- toggled in Settings.
        pane = QFrame()
        pane.setObjectName("advancedPane")
        grid = QGridLayout(pane)
        grid.setContentsMargins(10, 4, 10, 4)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(1)
        # Auto-hide fades opacity rather than calling setVisible(False), so
        # the pane keeps its layout slot and the caption panel above it
        # never resizes when the footer hides/shows.
        self._footer_opacity = QGraphicsOpacityEffect(pane)
        pane.setGraphicsEffect(self._footer_opacity)

        self.advanced_queue_label = QLabel("")
        self.advanced_threads_label = QLabel("")
        self.advanced_delay_label = QLabel("")
        self.advanced_app_label = QLabel("")
        self.advanced_host_label = QLabel("")
        self.advanced_model_label = QLabel("")
        self.advanced_size_label = QLabel("")
        row0 = (self.advanced_queue_label, self.advanced_threads_label, self.advanced_delay_label, self.advanced_app_label)
        row1 = (self.advanced_host_label, self.advanced_model_label, self.advanced_size_label)
        for col, w in enumerate(row0):
            w.setObjectName("advancedLabel")
            grid.addWidget(w, 0, col)
        for col, w in enumerate(row1):
            w.setObjectName("advancedLabel")
            grid.addWidget(w, 1, col)

        # Source (row0 col3) tends to hold the longest text (application
        # names) -- give it more of the pane's width than the others,
        # which are short and fixed-ish ("Queue: 3", "Threads: 4").
        grid.setColumnStretch(3, 2)

        self._advanced_pane = pane
        self._update_advanced_pane()
        return pane

    # -- public API, called from main.py -------------------------------
    def handle_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "modal_setup_status":
            new_status = event.get("status", "terminated")
            error = event.get("error")
            if new_status != self.modal_status:
                log.info("[Modal] status: %s -> %s", self.modal_status, new_status)
            if error:
                log.error("[Modal] setup failed: %s", error)
            self.modal_status = new_status
            if self._settings_panel is not None:
                self._settings_panel.set_modal_status(self.modal_status, error)
            self._update_advanced_pane()
            return

        if kind == "queue_update":
            # A pure gauge broadcast (backend main.py's _sequencer_ticker,
            # every 200ms) -- NOT evidence of real transcript activity, so
            # this must not touch _last_result_at below: that would defeat
            # _check_delayed's staleness detection, since this arrives
            # continuously even while nothing is actually being
            # transcribed. Its only job is keeping Queue/Threads accurate
            # (e.g. after a preempted backlog or a model switch flushes
            # the queue) without waiting for a transcript event that may
            # not be coming for a while.
            self._update_advanced_pane(queue_length=event.get("queue_length"), workers=event.get("workers"))
            return

        # Any transcript-pipeline event -- partial, final, or idle -- is
        # evidence the pipeline is still responding; see _check_delayed().
        self._last_result_at = time.time()

        # Queue length/worker count (and idle's implicit "0"/unchanged
        # count) should always land, even if the event below is an idle
        # transition or a throttled partial.
        if "queue_length" in event or "workers" in event:
            self._update_advanced_pane(
                queue_length=event.get("queue_length"), workers=event.get("workers")
            )

        if kind == "idle":
            self._set_status("off")
            return

        self._set_status(event.get("cpu_status", "off"))
        new_tier = event.get("model_tier", "")
        if new_tier and new_tier != self._current_tier:
            log.info("[Engine] model tier: %s -> %s", self._current_tier or "(none)", new_tier)
        self._current_tier = new_tier
        self._update_advanced_pane()
        if kind in ("partial", "final"):
            # Full text, not just a length -- this is the raw text/
            # translated_text exactly as received from the backend
            # pipeline, before any of this file's own display logic
            # (same-language collapsing, show_source_transcript,
            # _shown_source_dest below) decides what actually gets
            # rendered. The transcript history DB only ever stores the
            # latter (see _record_caption_event/_shown_source_dest) --
            # diffing a DB row against the matching line here is how a
            # bug in the FE rendering path (as opposed to the ASR/
            # translation engines themselves) would show up.
            log.info(
                "[Transcript] kind=%s cpu=%s tier=%s text=%r translated_text=%r",
                kind, event.get("cpu_status"), new_tier, event.get("text", ""), event.get("translated_text", ""),
            )

        detected = event.get("detected_lang")
        if detected and self._is_auto_selected():
            name = LANGUAGE_NAMES.get(detected, detected.upper())
            self.source_combo.setItemText(0, f"Auto Detect ({name})")

        if kind == "partial":
            now = time.monotonic()
            if now - self._last_partial_update < self.settings.refresh_speed_ms / 1000:
                return
            self._last_partial_update = now

        self._record_caption_event(kind, event)

    def set_source_app(self, name: str | None) -> None:
        self._current_source_app = name
        self._update_advanced_pane()

    def pulse_listening(self) -> None:
        """Called (from the audio thread, via a Qt signal) the instant real
        audio is detected -- gives immediate feedback rather than waiting
        several seconds for the backend to actually transcribe a segment.
        Only lights up from "off": never overrides a real cpu_status the
        backend already reported for the segment in progress. Restarts the
        listening timeout regardless, so the light still turns back off a
        fixed interval after audio actually stops even if the last thing
        the backend reported was yellow/red."""
        if self._current_status == "off":
            self._set_status("green")
        self._listening_timer.start(LISTENING_TIMEOUT_MS)

    def _on_listening_timeout(self) -> None:
        self._set_status("off")

    def _check_delayed(self) -> None:
        if self._current_status == "off":
            return  # no audio active right now, nothing to be overdue
        threshold = self.settings.acceptable_latency_s * DELAYED_FACTOR
        if time.time() - self._last_result_at > threshold:
            self._show_delayed_marker()
            self._last_result_at = time.time()  # start a fresh window instead of re-showing every tick

    def _show_delayed_marker(self) -> None:
        already_delayed = bool(self._caption_entries) and self._caption_entries[-1].get("marker") == "delayed"
        is_new_delayed_streak = not self._delayed_marker_active
        if self.settings.persist_subtitles:
            # Repeated delayed detections describe the same ongoing gap,
            # not a new one each time -- don't pile up a fresh <delayed>
            # line for every poll tick while it continues.
            if not already_delayed:
                self._caption_entries.append({"marker": "delayed"})
        else:
            self._caption_entries = [{"marker": "delayed"}]
        self._current_partial_entry = None
        self._pending_utterance_started_at = None
        if is_new_delayed_streak:
            self._delayed_marker_active = True
            now = time.time()
            self._queue_transcript_row(
                is_delayed=True, start_ts=now, end_ts=now,
                source_text="<delayed>", dest_text="<delayed>",
                source_lang=None, dest_lang=self.dest_combo.currentData(),
                auto_detect=self._is_auto_selected(),
            )
        self._render_caption_view()

    def apply_settings(self, settings: Settings) -> None:
        engine_changed = settings.engine != self._last_known_engine
        self.settings = settings
        self._apply_style()
        self._apply_auto_hide()
        self._update_advanced_pane()
        if engine_changed:
            self._rebuild_language_combos()
            self._last_known_engine = settings.engine

    # -- internals -------------------------------------------------------
    def _is_auto_selected(self) -> bool:
        return self.source_combo.currentData() == AUTO_CODE

    def _rebuild_language_combos(self) -> None:
        allowed = self._source_languages_for_engine(self.settings.engine)
        allowed_codes = {code for code, _ in allowed}

        previous_src = self.source_combo.currentData()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem("Auto Detect", userData=AUTO_CODE)
        for code, name in allowed:
            self.source_combo.addItem(name, userData=code)
        new_src = previous_src if previous_src in allowed_codes or previous_src == AUTO_CODE else AUTO_CODE
        self.source_combo.set_current_code(new_src)
        self.source_combo.blockSignals(False)
        if new_src != previous_src:
            self.settings.src_language = new_src
            self.settings.save()
            self._on_src_lang_change(new_src)

        previous_dst = self.dest_combo.currentData()
        self.dest_combo.blockSignals(True)
        self.dest_combo.clear()
        for code, name in allowed:
            self.dest_combo.addItem(name, userData=code)
        # DEFAULT_DEST_LANGUAGE ("en") is in every engine's language set, so
        # it's always a safe fallback -- unlike the source picker, the
        # destination has no Auto Detect option to fall back to instead.
        new_dst = previous_dst if previous_dst in allowed_codes else DEFAULT_DEST_LANGUAGE
        self.dest_combo.set_current_code(new_dst)
        self.dest_combo.blockSignals(False)
        if new_dst != previous_dst:
            self.settings.dest_language = new_dst
            self.settings.save()
            self._on_dest_lang_change(new_dst)

    def _apply_auto_hide(self) -> None:
        # Opacity, not setVisible(): hiding the header/footer this way must
        # not change the caption panel's size, so their layout slot always
        # stays reserved -- only their visibility (and, while hidden,
        # click-through) toggles.
        header_shown = self._is_hovering if self.settings.auto_hide_header else True
        self._toolbar_opacity.setOpacity(1.0 if header_shown else 0.0)
        self._toolbar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not header_shown)

        # Same opacity-only approach for "Show advanced diagnostics" itself,
        # not just its auto-hide -- setVisible(False) here used to reclaim
        # the footer's layout slot entirely, which let the caption text
        # area grow to fill the freed space the instant the toggle was
        # switched off. auto_hide_footer only matters while advanced_mode
        # is actually on; off, the footer stays hidden regardless of hover.
        footer_shown = self.settings.advanced_mode and (self._is_hovering if self.settings.auto_hide_footer else True)
        self._footer_opacity.setOpacity(1.0 if footer_shown else 0.0)
        self._advanced_pane.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not footer_shown)

    def _current_host_text(self) -> str:
        # Visible even with the settings panel closed -- local is the
        # actual fallback the whole time Modal isn't "ready"/"alive" (see
        # backend/main.py: engine_name only ever becomes "modal" once
        # deploy_and_warm_up() succeeds), so this always reflects which
        # engine is *actually* running, not just what was requested.
        return {
            "deploying": "Cloud (starting…)",
            "warming up": "Cloud (warming up…)",
            "stopping": "Cloud (stopping…)",
            "ready": "Cloud (Modal)",
            "alive": "Cloud (Modal)",
        }.get(self.modal_status, "Local")

    def _update_advanced_pane(self, queue_length: int | None = None, workers: int | None = None) -> None:
        if queue_length is not None:
            self._last_queue_length = queue_length
        if workers is not None:
            self._last_worker_count = workers
        self.advanced_queue_label.setText(f"Queue: {self._last_queue_length}")
        self.advanced_threads_label.setText(f"Threads: {self._last_worker_count if self._last_worker_count is not None else '—'}")
        self.advanced_delay_label.setText(f"Delay: {self.settings.acceptable_latency_s:g}s")
        self.advanced_app_label.setText(f"Source: {self._current_source_app or '—'}")
        self.advanced_host_label.setText(f"Host: {self._current_host_text()}")
        self.advanced_model_label.setText(f"Model: {self.settings.engine}")
        tier = self._current_tier or self.settings.tier
        # Parakeet has exactly one checkpoint, no tiers (see
        # parakeet_engine.py) -- its model_tier is always literally the
        # string "parakeet", which just repeats "Model: parakeet" right
        # next to it. Nothing meaningful to show there in that case.
        self.advanced_size_label.setText(f"Size: {tier}" if tier != "parakeet" else "Size: —")

    def _shown_source_dest(self, entry: dict) -> tuple[str, str]:
        """(source_text, dest_text) exactly as actually displayed for this
        entry -- "" for whichever side has no distinct line shown (e.g.
        dest_text=="" when same_lang collapses to a single line, or
        source_text=="" when show_source_transcript is off). The single
        place _entry_paragraphs, _entry_plain_text, and the transcript-
        history DB write (_record_caption_event) all derive from, so what
        gets saved to the DB can never drift from what's actually on
        screen -- the raw engine text/translated_text this decision is
        made FROM is what the [Transcript] log line records instead (see
        handle_event), so the two can be diffed against each other to
        isolate a rendering-side bug from an engine-side one."""
        if entry.get("marker") == "delayed":
            return "<delayed>", "<delayed>"

        raw_text = entry.get("text", "")
        raw_translated = entry.get("translated_text", "")
        detected = entry.get("detected_lang")
        dest_code = entry.get("dest_code")

        # Same language either by the engine's own report, or because the
        # translation came back byte-identical (e.g. Parakeet never reports
        # detected_lang, so this is the only signal available there).
        same_lang = (bool(detected) and detected == dest_code) or raw_translated == raw_text
        if same_lang or not raw_translated:
            return raw_text, ""
        if not self.settings.show_source_transcript:
            return "", raw_translated
        return raw_text, raw_translated

    def _entry_paragraphs(self, entry: dict) -> list[tuple[str, QColor, int, bool]]:
        """One caption entry -> one or two (text, color, font_size, italic)
        paragraphs for CaptionView.render() -- two when there's a
        translated primary line plus a dimmer original secondary line."""
        s = self.settings
        if entry.get("marker") == "delayed":
            return [("<delayed>", QColor(s.font_color), s.font_size, True)]

        source_text, dest_text = self._shown_source_dest(entry)
        if source_text and dest_text:
            dim = QColor(s.font_color)
            dim.setAlpha(150)
            return [
                (dest_text, QColor(s.font_color), s.font_size, False),
                (source_text, dim, max(s.font_size - 2, 8), False),
            ]
        return [(dest_text or source_text, QColor(s.font_color), s.font_size, False)]

    def _entry_plain_text(self, entry: dict) -> str:
        if entry.get("marker") == "delayed":
            return "<delayed>"
        source_text, dest_text = self._shown_source_dest(entry)
        if source_text and dest_text:
            return f"{dest_text}\n{source_text}"
        return dest_text or source_text

    def _record_caption_event(self, kind: str, event: dict) -> None:
        entry = {
            "text": event.get("text", ""),
            "translated_text": event.get("translated_text", ""),
            "detected_lang": event.get("detected_lang"),
            # Snapshotted now, not re-read live at render time: an older
            # entry must keep judging "same language?" against the
            # destination that was actually active when it was recorded,
            # even after the user later changes the destination picker.
            "dest_code": self.dest_combo.currentData(),
        }
        # A real event ending -- any delayed streak is over.
        self._delayed_marker_active = False
        if kind == "final":
            if self.settings.persist_subtitles:
                self._caption_entries.append(entry)
            else:
                self._caption_entries = [entry]
            self._current_partial_entry = None
            # "final" is genuinely finished per the backend's own
            # segmentation contract (vad_segmenter.py) -- no further
            # partial will ever refine this one -- so this is exactly the
            # point to persist it. start_ts falls back to now for the rare
            # case a "final" arrives with no partial ever seen before it
            # (a very short utterance); end_ts prefers the backend's own
            # segment_closed_at over local receipt time.
            start_ts = self._pending_utterance_started_at or time.time()
            end_ts = event.get("segment_closed_at") or time.time()
            self._pending_utterance_started_at = None
            source_lang = entry["detected_lang"] or (
                None if self._is_auto_selected() else self.source_combo.currentData()
            )
            # What's actually shown, not the raw engine text/translated_text
            # -- see _shown_source_dest's docstring. The raw values are what
            # the [Transcript] log line above already recorded, so a diff
            # between that log and this DB row is exactly how a FE
            # rendering bug (vs. an ASR/translation engine one) would show up.
            shown_source, shown_dest = self._shown_source_dest(entry)
            self._queue_transcript_row(
                is_delayed=False, start_ts=start_ts, end_ts=end_ts,
                source_text=shown_source, dest_text=shown_dest,
                source_lang=source_lang, dest_lang=entry["dest_code"],
                auto_detect=self._is_auto_selected(),
            )
        else:  # partial
            if self._current_partial_entry is None:
                # First partial of a new utterance -- its start time, since
                # no backend event ever reports a segment's true start,
                # only when it closed.
                self._pending_utterance_started_at = time.time()
            self._current_partial_entry = entry
            if not self.settings.persist_subtitles:
                self._caption_entries = []
        self._render_caption_view()

    def _queue_transcript_row(
        self, *, is_delayed: bool, start_ts: float, end_ts: float, source_text: str, dest_text: str,
        source_lang: str | None, dest_lang: str | None, auto_detect: bool,
    ) -> None:
        """Schedules a debounced (TRANSCRIPT_STORE_DEBOUNCE_MS) write to the
        transcript history DB -- see transcript_store.py for why this is
        delayed rather than immediate. Everything is snapshotted into the
        closure now, at the moment the entry became final/delayed, not
        re-read from live state when the timer actually fires."""
        if self._transcript_store is None:
            return
        store = self._transcript_store
        host = self._current_host_text()
        model = self.settings.engine
        size = self._current_tier or self.settings.tier
        source_app = self._current_source_app
        QTimer.singleShot(
            TRANSCRIPT_STORE_DEBOUNCE_MS,
            lambda: store.insert(
                start_ts=start_ts, end_ts=end_ts, is_delayed=is_delayed, auto_detect=auto_detect,
                source_lang=source_lang, dest_lang=dest_lang, source_text=source_text, dest_text=dest_text,
                host=host, model=model, size=size, source_app=source_app,
            ),
        )

    def _font_background_color(self) -> QColor | None:
        """None means no box at all -- either by the color's own alpha or
        by font_background_opacity being 0. The two multiply rather than
        either alone deciding: a translucent picked color still fades
        further as opacity drops, and opacity=0 always means no box
        regardless of what color was picked."""
        s = self.settings
        color = QColor(s.font_background_color)
        color.setAlpha(round(color.alpha() * (s.font_background_opacity / 100)))
        return color if color.alpha() > 0 else None

    def _render_caption_view(self) -> None:
        s = self.settings
        entries = list(self._caption_entries)
        if self._current_partial_entry is not None:
            entries.append(self._current_partial_entry)

        paragraphs: list[tuple[str, QColor, int, bool]] = []
        for entry in entries:
            paragraphs.extend(self._entry_paragraphs(entry))
        plain_text = "\n".join(self._entry_plain_text(e) for e in entries)

        self.caption_view.render(
            paragraphs, plain_text,
            font_background=self._font_background_color(),
            outline_color=QColor(s.outline_color),
            outline_width=s.outline_width,
        )

    def _reposition_caption_overlays(self) -> None:
        margin = 6
        # The scrollbar always occupies its own strip on the right (see
        # caption_view.py) -- keep the icons clear of it rather than
        # floating over it.
        scrollbar_width = self.caption_view.verticalScrollBar().sizeHint().width()
        width = self.caption_view.width() - scrollbar_width
        y = margin
        x = width - self.clear_btn.width() - margin
        self.clear_btn.move(x, y)
        x -= self.copy_btn.width() + 4
        self.copy_btn.move(x, y)
        self.copy_btn.raise_()
        self.clear_btn.raise_()

        # Inset a couple pixels in from the bottom and from the
        # scrollbar's left edge -- "slightly inside and to the left of
        # the scroll bar."
        grip_x = width - self.size_grip.width() - 2
        grip_y = self.caption_view.height() - self.size_grip.height() - 2
        self.size_grip.move(grip_x, grip_y)
        self.size_grip.raise_()

    def _handle_copy_transcript(self) -> None:
        QApplication.clipboard().setText(self.caption_view.to_plain_text())

    def _handle_clear_transcript(self) -> None:
        self._caption_entries = []
        self._current_partial_entry = None
        self._render_caption_view()

    def _handle_pause_toggle(self, checked: bool) -> None:
        self.pause_btn.setText("▶" if checked else "▮▮")
        self.pause_btn.setToolTip("Resume audio capture" if checked else "Pause audio capture")
        self._on_pause_toggled(checked)

    def _handle_swap(self) -> None:
        old_src = self.source_combo.currentData()
        old_dst = self.dest_combo.currentData()
        new_src = old_dst
        # The destination picker has no "Auto Detect" option, so swapping
        # away from it needs a fallback destination.
        new_dst = old_src if old_src != AUTO_CODE else DEFAULT_DEST_LANGUAGE

        # set_current_code() deliberately doesn't emit codeChanged (unlike
        # QComboBox.setCurrentIndex, which fires even when set
        # programmatically) -- call the handlers explicitly so the swap
        # still persists and notifies the backend, not just the UI text.
        self.dest_combo.set_current_code(new_dst)
        self._handle_dest_change(new_dst)
        self.source_combo.set_current_code(new_src)
        self._handle_src_change(new_src)

    def _toggle_settings_panel(self) -> None:
        if self._settings_panel is not None:
            self._settings_panel.close()
            return

        panel = SettingsDialog(
            self.settings,
            on_change=self.apply_settings,
            actions=self._settings_actions,
            modal_status=self.modal_status,
            parent=self,
        )
        panel.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        panel.destroyed.connect(self._on_settings_panel_closed)
        panel.adjustSize()
        anchor = self.settings_btn.mapToGlobal(self.settings_btn.rect().bottomLeft())
        panel.move(anchor.x() - panel.width() + self.settings_btn.width(), anchor.y() + 6)
        panel.show()
        panel.raise_()
        panel.activateWindow()
        self._settings_panel = panel

    def _on_settings_panel_closed(self) -> None:
        self._settings_panel = None

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.Enter, QEvent.Type.Leave) and obj in (self._toolbar, self._caption_panel, self._advanced_pane):
            self._update_hover_state()

        if obj is self._caption_panel:
            if event.type() == QEvent.Type.Enter:
                self.copy_btn.setVisible(True)
                self.clear_btn.setVisible(True)
            elif event.type() == QEvent.Type.Leave:
                self.copy_btn.setVisible(False)
                self.clear_btn.setVisible(False)

        if obj is self.caption_view and event.type() == QEvent.Type.Resize:
            self._reposition_caption_overlays()

        return super().eventFilter(obj, event)

    def _update_hover_state(self) -> None:
        """Re-derives hover purely from cursor position vs. this window's
        own screen rect -- not from which specific child widget an Enter/
        Leave event was targeted at. Qt fires Leave on this window when the
        cursor moves onto a *child* widget too (the toolbar, the caption
        panel, the footer), even though the cursor never actually left the
        window's bounds; treating that as a real leave was what made an
        auto-hidden header/footer impossible to reach ("locked in") -- the
        cursor crossing from the caption panel into the toolbar looked
        identical to it leaving the window entirely."""
        hovering = self.frameGeometry().contains(QCursor.pos())
        if hovering:
            # Always stop, not just when _is_hovering was already False:
            # a hide can be *pending* (timer running) while _is_hovering is
            # still True, since that flag only flips in _commit_hide. Using
            # "hovering == self._is_hovering" as a no-op shortcut here would
            # skip cancelling that pending hide on a quick re-entry.
            self._hide_delay_timer.stop()
            if not self._is_hovering:
                self._is_hovering = True
                self._apply_auto_hide()
        elif self._is_hovering and not self._hide_delay_timer.isActive():
            self._hide_delay_timer.start(HIDE_DELAY_MS)

    def _commit_hide(self) -> None:
        self._is_hovering = self.frameGeometry().contains(QCursor.pos())
        self._apply_auto_hide()

    def _handle_src_change(self, code: str) -> None:
        if code != AUTO_CODE:
            self.source_combo.setItemText(0, "Auto Detect")  # reset stale detected-language suffix
        self.settings.src_language = code
        self.settings.save()
        self._on_src_lang_change(code)

    def _handle_dest_change(self, code: str) -> None:
        self.settings.dest_language = code
        self.settings.save()
        self._on_dest_lang_change(code)
        self._render_caption_view()

    def _handle_close(self) -> None:
        self._save_window_position()
        self._on_close()

    def _save_window_position(self) -> None:
        self.settings.window_x = self.x()
        self.settings.window_y = self.y()
        self.settings.save()

    def _set_status(self, status: str) -> None:
        self._current_status = status
        color = STRAIN_COLORS.get(status, STRAIN_COLORS["off"])
        self.status_dot.setStyleSheet(f"color: {color}; font-size: 14px;")

    def _apply_style(self) -> None:
        s = self.settings
        # background_color/background_opacity is the app window's own
        # translucency -- the floating toolbar/caption panel/footer chrome
        # -- not the caption text (that's outline_color/outline_width and
        # font_background_color/opacity, painted directly in
        # caption_view.py). Restored to that after a round where it was
        # temporarily repurposed as a text highlight; this is the
        # original, correct scope.
        alpha = int(s.background_opacity / 100 * 255)
        bg = QColor(s.background_color)
        bg_rgba = f"rgba({bg.red()}, {bg.green()}, {bg.blue()}, {alpha})"

        self.setStyleSheet(f"""
            #toolbar {{
                background-color: rgba(30, 30, 30, {min(alpha + 40, 255)});
                border: 1px solid rgba(255, 255, 255, 60);
                border-radius: 14px;
            }}
            #captionPanel {{
                background-color: {bg_rgba};
                border-radius: 14px;
            }}
            #languageSelector {{
                background-color: rgba(255, 255, 255, 20);
                border-radius: 12px;
            }}
            #advancedPane {{
                background-color: rgba(0, 0, 0, {min(alpha + 20, 255)});
                border: 1px solid rgba(255, 255, 255, 60);
                border-radius: 10px;
            }}
            #advancedLabel {{
                color: #cccccc;
                font-size: 11px;
            }}
            #iconButton {{
                background: transparent;
                color: #eeeeee;
                border: none;
                font-size: 14px;
                padding: 2px 6px;
            }}
            #iconButton:hover {{
                background-color: rgba(255, 255, 255, 30);
                border-radius: 4px;
            }}
            #closeButton {{
                background: transparent;
                color: #eeeeee;
                border: none;
                font-size: 15px;
                font-weight: bold;
                padding: 2px 6px;
            }}
            #closeButton:hover {{
                background-color: rgba(255, 255, 255, 30);
                border-radius: 4px;
            }}
            #langCombo {{
                color: #eeeeee;
                background-color: rgba(255, 255, 255, 12);
                border: 1px solid rgba(255, 255, 255, 70);
                border-radius: 4px;
                padding: 1px 4px;
                font-size: 11px;
            }}
            #swapButton {{
                background: transparent;
                color: #eeeeee;
                border: 1px solid rgba(255, 255, 255, 70);
                border-radius: 4px;
                font-size: 14px;
                padding: 2px 6px;
            }}
            #swapButton:hover {{
                background-color: rgba(255, 255, 255, 30);
            }}
            #captionView {{
                background: transparent;
                border: none;
            }}
        """)
        self.caption_view.set_base_font(QFont(s.font_family, s.font_size))
        self._render_caption_view()

    # -- window dragging (frameless -> drag from anywhere) ----------------
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None:
            self._drag_offset = None
            self._save_window_position()

    def enterEvent(self, event) -> None:
        self._update_hover_state()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._update_hover_state()
        super().leaveEvent(event)

    def closeEvent(self, event) -> None:
        self._save_window_position()
        super().closeEvent(event)
