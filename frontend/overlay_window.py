"""Transparent, always-on-top floating overlay -- the core UI. Layout
targets the ViiTor Translate reference screenshot (see plan): a slim pill
toolbar (source-language picker with an Auto Detect option, swap icon,
destination dropdown, settings gear, model-tier badge + CPU-strain light,
close) above a semi-transparent caption panel. Partial-result text updates
in place (no layout jump), rate-limited by the refresh-speed setting so
rapid partial updates don't visibly flicker.
"""

from __future__ import annotations

import time

from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtGui import QColor, QFont, QMouseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizeGrip,
    QVBoxLayout,
    QWidget,
)

from .settings import Settings
from .settings_dialog import SettingsDialog

STRAIN_COLORS = {
    "off": "#555555",
    "green": "#3fbf50",
    "yellow": "#e0b400",
    "red": "#e0392b",
}

# ISO 639-1 codes shared by both language pickers. The source picker
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
AUTO_CODE = "auto"
DEFAULT_DEST_LANGUAGE = "en"  # mirrors Settings.dest_language's default


class OverlayWindow(QWidget):
    def __init__(self, settings: Settings, on_src_lang_change, on_dest_lang_change, on_close) -> None:
        super().__init__()
        self.settings = settings
        self._on_src_lang_change = on_src_lang_change
        self._on_dest_lang_change = on_dest_lang_change
        self._on_close = on_close
        self._settings_panel: SettingsDialog | None = None
        self._last_partial_update = 0.0
        self._last_event: dict | None = None
        self._drag_offset: QPoint | None = None

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

        self._apply_style()

    # -- toolbar -----------------------------------------------------
    def _build_toolbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("toolbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 6, 10, 6)

        self.source_combo = QComboBox()
        self.source_combo.addItem("Auto Detect", userData=AUTO_CODE)
        for code, name in LANGUAGES:
            self.source_combo.addItem(name, userData=code)
        self._set_combo_code(self.source_combo, self.settings.src_language)
        self.source_combo.currentIndexChanged.connect(self._handle_src_change)

        swap_btn = QPushButton("⇄")  # swap arrows
        swap_btn.setObjectName("iconButton")
        swap_btn.setToolTip("Swap source/destination language")
        swap_btn.clicked.connect(self._handle_swap)

        self.dest_combo = QComboBox()
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

        self.tier_label = QLabel("")
        self.tier_label.setObjectName("tierLabel")

        self.settings_btn = QPushButton("⚙")  # gear
        self.settings_btn.setObjectName("iconButton")
        self.settings_btn.clicked.connect(self._toggle_settings_panel)

        close_btn = QPushButton("✕")  # close
        close_btn.setObjectName("iconButton")
        close_btn.clicked.connect(self._handle_close)

        layout.addWidget(lang_selector)
        layout.addStretch(1)
        for w in (self.status_dot, self.tier_label, self.settings_btn, close_btn):
            layout.addWidget(w)

        return bar

    def _build_caption_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("captionPanel")
        layout = QVBoxLayout(panel)

        self.source_app_label = QLabel("")
        self.source_app_label.setObjectName("sourceAppLabel")
        layout.addWidget(self.source_app_label)

        self.caption_label = QLabel("")
        self.caption_label.setWordWrap(True)
        self.caption_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.caption_label)

        self.caption_secondary_label = QLabel("")
        self.caption_secondary_label.setWordWrap(True)
        self.caption_secondary_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.caption_secondary_label)
        layout.addStretch(1)

        grip_row = QHBoxLayout()
        grip_row.addStretch(1)
        grip_row.addWidget(QSizeGrip(self))
        layout.addLayout(grip_row)

        return panel

    # -- public API, called from main.py -------------------------------
    def handle_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "idle":
            self._set_status("off")
            return

        self._set_status(event.get("cpu_status", "off"))
        self.tier_label.setText(event.get("model_tier", ""))

        detected = event.get("detected_lang")
        if detected and self._is_auto_selected():
            name = LANGUAGE_NAMES.get(detected, detected.upper())
            self.source_combo.setItemText(0, f"Auto Detect ({name})")

        if kind == "partial":
            now = time.monotonic()
            if now - self._last_partial_update < self.settings.refresh_speed_ms / 1000:
                return
            self._last_partial_update = now

        self._last_event = event
        self._render_caption()

    def set_source_app(self, name: str | None) -> None:
        self.source_app_label.setText(f"Source: {name}" if name else "")

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        self._apply_style()

    # -- internals -------------------------------------------------------
    def _is_auto_selected(self) -> bool:
        return self.source_combo.currentData() == AUTO_CODE

    def _render_caption(self) -> None:
        if self._last_event is None:
            return
        text = self._last_event.get("text", "")
        translated = self._last_event.get("translated_text", "")
        detected = self._last_event.get("detected_lang")
        dest_code = self.dest_combo.currentData()

        same_lang = bool(detected) and detected == dest_code
        if same_lang or not translated:
            self.caption_label.setText(text)
            self.caption_secondary_label.setText("")
            self.caption_secondary_label.hide()
            return

        self.caption_label.setText(translated)
        self.caption_secondary_label.setText(text)
        self.caption_secondary_label.show()

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

        panel = SettingsDialog(self.settings, on_change=self.apply_settings, parent=self)
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
        self._render_caption()

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
        color = STRAIN_COLORS.get(status, STRAIN_COLORS["off"])
        self.status_dot.setStyleSheet(f"color: {color}; font-size: 14px;")

    def _apply_style(self) -> None:
        s = self.settings
        alpha = int(s.background_opacity / 100 * 255)
        bg = QColor(s.background_color)
        bg_rgba = f"rgba({bg.red()}, {bg.green()}, {bg.blue()}, {alpha})"
        font_color = QColor(s.font_color)
        dim_rgba = f"rgba({font_color.red()}, {font_color.green()}, {font_color.blue()}, 150)"

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
            #tierLabel, #sourceAppLabel {{
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
            QComboBox {{
                color: #eeeeee;
                background: transparent;
                border: none;
                font-size: 11px;
            }}
        """)
        self.caption_label.setFont(QFont(s.font_family, s.font_size))
        self.caption_label.setStyleSheet(f"color: {s.font_color}; background: transparent;")
        self.caption_secondary_label.setFont(QFont(s.font_family, max(s.font_size - 2, 8)))
        self.caption_secondary_label.setStyleSheet(f"color: {dim_rgba}; background: transparent;")

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

    def closeEvent(self, event) -> None:
        self._save_window_position()
        super().closeEvent(event)
