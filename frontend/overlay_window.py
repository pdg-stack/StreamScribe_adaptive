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

import html
import time

from PyQt6.QtCore import QEvent, QPoint, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QMouseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizeGrip,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .caption_icons import LineIconButton
from .settings import Settings
from .settings_dialog import SettingsDialog

STRAIN_COLORS = {
    "off": "#555555",
    "green": "#3fbf50",
    "yellow": "#e0b400",
    "red": "#e0392b",
}

# How long the status light stays "on" after the most recent detected
# activity pulse before reverting to "off" -- see pulse_listening().
LISTENING_TIMEOUT_MS = 5000

# ISO 639-1 codes for faster-whisper's source picker (and unconditionally
# for the destination picker -- translation is NLLB-200, independent of
# whichever ASR engine produced the source text). The source picker
# additionally gets an "Auto Detect" entry (see plan's Translation
# section) prepended in _build_toolbar.
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
    ) -> None:
        super().__init__()
        self.settings = settings
        self._on_src_lang_change = on_src_lang_change
        self._on_dest_lang_change = on_dest_lang_change
        self._on_close = on_close
        self._settings_actions = {
            "on_engine_change": on_engine_change,
            "on_tier_change": on_tier_change,
            "on_latency_change": on_latency_change,
            "on_modal_setup_requested": on_modal_setup_requested,
            "on_modal_stop_requested": on_modal_stop_requested,
        }
        self._settings_panel: SettingsDialog | None = None
        self.modal_status = "terminated"
        self._current_status = "off"
        self._current_tier = ""
        self._current_source_app: str | None = None
        self._last_queue_length = 0
        self._last_known_engine = settings.engine
        self._last_partial_update = 0.0
        self._caption_entries: list[str] = []  # finalized entries (see _format_entry_html)
        self._current_partial_html = ""
        self._drag_offset: QPoint | None = None
        self._is_hovering = False

        self._listening_timer = QTimer(self)
        self._listening_timer.setSingleShot(True)
        self._listening_timer.timeout.connect(self._on_listening_timeout)

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
        root.setSpacing(4)

        root.addWidget(self._build_toolbar())
        root.addWidget(self._build_caption_panel(), stretch=1)
        root.addWidget(self._build_advanced_pane())

        self._apply_style()
        self._apply_auto_hide()

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

        self.source_combo = QComboBox()
        self.source_combo.setObjectName("langCombo")
        self.source_combo.addItem("Auto Detect", userData=AUTO_CODE)
        for code, name in self._source_languages_for_engine(self.settings.engine):
            self.source_combo.addItem(name, userData=code)
        self._set_combo_code(self.source_combo, self.settings.src_language)
        self.source_combo.currentIndexChanged.connect(self._handle_src_change)

        swap_btn = QPushButton("⇄")  # swap arrows
        swap_btn.setObjectName("swapButton")
        swap_btn.setToolTip("Swap source/destination language")
        swap_btn.clicked.connect(self._handle_swap)

        self.dest_combo = QComboBox()
        self.dest_combo.setObjectName("langCombo")
        for code, name in LANGUAGES:
            self.dest_combo.addItem(name, userData=code)
        self._set_combo_code(self.dest_combo, self.settings.dest_language)
        self.dest_combo.currentIndexChanged.connect(self._handle_dest_change)

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

        self.status_dot = QLabel("●")  # status light
        self.status_dot.setObjectName("statusDot")
        self._set_status("off")

        self.settings_btn = QPushButton("⚙")  # gear
        self.settings_btn.setObjectName("iconButton")
        self.settings_btn.clicked.connect(self._toggle_settings_panel)

        close_btn = QPushButton("✕")  # close
        close_btn.setObjectName("iconButton")
        close_btn.clicked.connect(self._handle_close)

        layout.addWidget(lang_selector)
        layout.addStretch(1)
        for w in (self.status_dot, self.settings_btn, close_btn):
            layout.addWidget(w)

        return bar

    def _build_caption_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("captionPanel")
        panel.installEventFilter(self)
        self._caption_panel = panel
        layout = QVBoxLayout(panel)

        # QTextEdit (not QLabel) so long lines wrap to the panel's width and,
        # when "persist subtitles" is on, older lines scroll rather than
        # getting clipped.
        self.caption_view = QTextEdit()
        self.caption_view.setObjectName("captionView")
        self.caption_view.setReadOnly(True)
        self.caption_view.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(self.caption_view, stretch=1)

        # Copy/clear -- bottom-left, only shown while the mouse is over the
        # caption area (see eventFilter's Enter/Leave handling below).
        self.copy_btn = LineIconButton("copy", color="#dddddd")
        self.copy_btn.setToolTip("Copy transcript")
        self.copy_btn.clicked.connect(self._handle_copy_transcript)
        self.copy_btn.setVisible(False)

        self.clear_btn = LineIconButton("clear", color="#e0392b")
        self.clear_btn.setToolTip("Clear transcript")
        self.clear_btn.clicked.connect(self._handle_clear_transcript)
        self.clear_btn.setVisible(False)

        grip_row = QHBoxLayout()
        grip_row.addWidget(self.copy_btn)
        grip_row.addWidget(self.clear_btn)
        grip_row.addStretch(1)
        grip_row.addWidget(QSizeGrip(self))
        layout.addLayout(grip_row)

        return panel

    def _build_advanced_pane(self) -> QFrame:
        # Diagnostic detail hidden from the default view (see plan's
        # Advanced mode): audio source app, active model/tier, host
        # (local/cloud), and queue length + acceptable delay -- toggled in
        # Settings, one item per row.
        pane = QFrame()
        pane.setObjectName("advancedPane")
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(1)
        # Auto-hide fades opacity rather than calling setVisible(False), so
        # the pane keeps its layout slot and the caption panel above it
        # never resizes when the footer hides/shows.
        self._footer_opacity = QGraphicsOpacityEffect(pane)
        pane.setGraphicsEffect(self._footer_opacity)

        self.advanced_app_label = QLabel("")
        self.advanced_model_label = QLabel("")
        self.advanced_host_label = QLabel("")
        self.advanced_queue_label = QLabel("")
        for w in (
            self.advanced_app_label,
            self.advanced_model_label,
            self.advanced_host_label,
            self.advanced_queue_label,
        ):
            w.setObjectName("advancedLabel")
            layout.addWidget(w)

        self._advanced_pane = pane
        self._update_advanced_pane()
        return pane

    # -- public API, called from main.py -------------------------------
    def handle_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "modal_setup_status":
            self.modal_status = event.get("status", "terminated")
            if self._settings_panel is not None:
                self._settings_panel.set_modal_status(self.modal_status)
            self._update_advanced_pane()
            return

        # Queue length (and idle's implicit "0") should always land, even
        # if the event below is an idle transition or a throttled partial.
        if "queue_length" in event:
            self._update_advanced_pane(queue_length=event["queue_length"])

        if kind == "idle":
            self._set_status("off")
            return

        self._set_status(event.get("cpu_status", "off"))
        self._current_tier = event.get("model_tier", "")
        self._update_advanced_pane()

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

    def apply_settings(self, settings: Settings) -> None:
        engine_changed = settings.engine != self._last_known_engine
        self.settings = settings
        self._apply_style()
        self._apply_auto_hide()
        self._update_advanced_pane()
        if engine_changed:
            self._rebuild_source_combo()
            self._last_known_engine = settings.engine

    # -- internals -------------------------------------------------------
    def _is_auto_selected(self) -> bool:
        return self.source_combo.currentData() == AUTO_CODE

    def _rebuild_source_combo(self) -> None:
        allowed = self._source_languages_for_engine(self.settings.engine)
        allowed_codes = {code for code, _ in allowed}
        previous_code = self.source_combo.currentData()

        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem("Auto Detect", userData=AUTO_CODE)
        for code, name in allowed:
            self.source_combo.addItem(name, userData=code)
        new_code = previous_code if previous_code in allowed_codes or previous_code == AUTO_CODE else AUTO_CODE
        self._set_combo_code(self.source_combo, new_code)
        self.source_combo.blockSignals(False)

        if new_code != previous_code:
            self.settings.src_language = new_code
            self.settings.save()
            self._on_src_lang_change(new_code)

    def _apply_auto_hide(self) -> None:
        # Opacity, not setVisible(): hiding the header/footer this way must
        # not change the caption panel's size, so their layout slot always
        # stays reserved -- only their visibility (and, while hidden,
        # click-through) toggles.
        header_shown = self._is_hovering if self.settings.auto_hide_header else True
        self._toolbar_opacity.setOpacity(1.0 if header_shown else 0.0)
        self._toolbar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not header_shown)

        # Advanced mode itself (not auto-hide) still reclaims the footer's
        # space entirely when off -- that's a separate, existing toggle.
        self._advanced_pane.setVisible(self.settings.advanced_mode)
        if self.settings.advanced_mode:
            footer_shown = self._is_hovering if self.settings.auto_hide_footer else True
            self._footer_opacity.setOpacity(1.0 if footer_shown else 0.0)
            self._advanced_pane.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not footer_shown)

    def _update_advanced_pane(self, queue_length: int | None = None) -> None:
        if queue_length is not None:
            self._last_queue_length = queue_length
        self.advanced_app_label.setText(f"Source app: {self._current_source_app or '—'}")
        tier = self._current_tier or self.settings.tier
        self.advanced_model_label.setText(f"Model: {self.settings.engine} ({tier})")
        is_cloud = self.modal_status in ("ready", "alive")
        self.advanced_host_label.setText(f"Host: {'Cloud (Modal)' if is_cloud else 'Local'}")
        self.advanced_queue_label.setText(
            f"Queue: {self._last_queue_length}    Acceptable delay: {self.settings.acceptable_latency_s:g}s"
        )

    def _format_entry_html(self, event: dict) -> str:
        raw_text = event.get("text", "")
        raw_translated = event.get("translated_text", "")
        text = html.escape(raw_text)
        translated = html.escape(raw_translated)
        detected = event.get("detected_lang")
        dest_code = self.dest_combo.currentData()
        s = self.settings

        # Same language either by the engine's own report, or because the
        # translation came back byte-identical (e.g. Parakeet never reports
        # detected_lang, so this is the only signal available there).
        same_lang = (bool(detected) and detected == dest_code) or raw_translated == raw_text
        if same_lang or not raw_translated:
            return f'<div style="color:{s.font_color};">{text}</div>'

        font_color = QColor(s.font_color)
        dim_rgba = f"rgba({font_color.red()}, {font_color.green()}, {font_color.blue()}, 150)"
        secondary_size = max(s.font_size - 2, 8)
        return (
            f'<div style="color:{s.font_color};">{translated}</div>'
            f'<div style="color:{dim_rgba}; font-size:{secondary_size}px;">{text}</div>'
        )

    def _record_caption_event(self, kind: str, event: dict) -> None:
        entry_html = self._format_entry_html(event)
        if kind == "final":
            if self.settings.persist_subtitles:
                self._caption_entries.append(entry_html)
            else:
                self._caption_entries = [entry_html]
            self._current_partial_html = ""
        else:  # partial
            self._current_partial_html = entry_html
            if not self.settings.persist_subtitles:
                self._caption_entries = []
        self._render_caption_view()

    def _render_caption_view(self) -> None:
        blocks = list(self._caption_entries)
        if self._current_partial_html:
            blocks.append(self._current_partial_html)

        scrollbar = self.caption_view.verticalScrollBar()
        # Only follow new text if the user was already at the bottom --
        # otherwise they've scrolled up to read history, and a new line
        # arriving shouldn't yank the view back down. setHtml() itself
        # resets the scroll position, so the previous value is always
        # restored explicitly below regardless of which case applies.
        was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        previous_value = scrollbar.value()

        self.caption_view.setHtml("<br>".join(blocks))

        scrollbar.setValue(scrollbar.maximum() if was_at_bottom else previous_value)

    def _handle_copy_transcript(self) -> None:
        QApplication.clipboard().setText(self.caption_view.toPlainText())

    def _handle_clear_transcript(self) -> None:
        self._caption_entries = []
        self._current_partial_html = ""
        self._render_caption_view()

    def _handle_swap(self) -> None:
        old_src = self.source_combo.currentData()
        old_dst = self.dest_combo.currentData()
        new_src = old_dst
        # The destination picker has no "Auto Detect" option, so swapping
        # away from it needs a fallback destination.
        new_dst = old_src if old_src != AUTO_CODE else DEFAULT_DEST_LANGUAGE

        self._set_combo_code(self.dest_combo, new_dst)
        self._set_combo_code(self.source_combo, new_src)
        if new_src != AUTO_CODE:
            self.source_combo.setItemText(0, "Auto Detect")

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
        QApplication.instance().installEventFilter(self)

    def _on_settings_panel_closed(self) -> None:
        self._settings_panel = None
        QApplication.instance().removeEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if obj is self._caption_panel:
            if event.type() == QEvent.Type.Enter:
                self.copy_btn.setVisible(True)
                self.clear_btn.setVisible(True)
            elif event.type() == QEvent.Type.Leave:
                self.copy_btn.setVisible(False)
                self.clear_btn.setVisible(False)
        if self._settings_panel is not None and event.type() == QEvent.Type.MouseButtonPress:
            pos = event.globalPosition().toPoint()
            inside_panel = self._settings_panel.frameGeometry().contains(pos)
            inside_gear = self.settings_btn.rect().contains(self.settings_btn.mapFromGlobal(pos))
            if not inside_panel and not inside_gear:
                self._settings_panel.close()
        return super().eventFilter(obj, event)

    def _handle_src_change(self, index: int) -> None:
        code = self.source_combo.itemData(index)
        if code != AUTO_CODE:
            self.source_combo.setItemText(0, "Auto Detect")  # reset stale detected-language suffix
        self.settings.src_language = code
        self.settings.save()
        self._on_src_lang_change(code)

    def _handle_dest_change(self, index: int) -> None:
        code = self.dest_combo.itemData(index)
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

    @staticmethod
    def _set_combo_code(combo: QComboBox, code: str) -> None:
        idx = combo.findData(code)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _set_status(self, status: str) -> None:
        self._current_status = status
        color = STRAIN_COLORS.get(status, STRAIN_COLORS["off"])
        self.status_dot.setStyleSheet(f"color: {color}; font-size: 14px;")

    def _apply_style(self) -> None:
        s = self.settings
        alpha = int(s.background_opacity / 100 * 255)
        bg = QColor(s.background_color)
        bg_rgba = f"rgba({bg.red()}, {bg.green()}, {bg.blue()}, {alpha})"

        self.setStyleSheet(f"""
            #toolbar {{
                background-color: rgba(30, 30, 30, {min(alpha + 40, 255)});
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
        self.caption_view.setFont(QFont(s.font_family, s.font_size))
        self.caption_view.viewport().setStyleSheet("background: transparent;")
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
        self._is_hovering = True
        self._apply_auto_hide()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._is_hovering = False
        self._apply_auto_hide()
        super().leaveEvent(event)

    def closeEvent(self, event) -> None:
        self._save_window_position()
        super().closeEvent(event)
