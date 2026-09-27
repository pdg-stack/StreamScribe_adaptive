"""Settings panel: font/color/opacity/refresh-speed controls, applied live
to the overlay and persisted via Settings.save().

Deliberately a frameless QWidget, not a QDialog/.exec(): the main overlay is
WindowStaysOnTopHint, so a plain QDialog (no matching stays-on-top flag)
ends up rendered *behind* it -- unreachable to clicks and looking merged
into the overlay. This also gets its own WindowStaysOnTopHint, is parented
to the overlay so it doesn't get an independent taskbar/minimize identity,
and stays fully opaque. Toggled open/closed by the overlay's gear icon and
closed on an outside click (see OverlayWindow._toggle_settings_panel).
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QColorDialog,
    QFontComboBox,
    QFormLayout,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .settings import Settings


class SettingsDialog(QWidget):
    def __init__(self, settings: Settings, on_change, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.settings = settings
        self._on_change = on_change

        form = QFormLayout()

        self.font_combo = QFontComboBox()
        self.font_combo.setCurrentFont(self.font_combo.currentFont().__class__(settings.font_family))
        self.font_combo.currentFontChanged.connect(lambda f: self._update("font_family", f.family()))
        form.addRow("Font", self.font_combo)

        self.size_spin = QSpinBox()
        self.size_spin.setRange(10, 72)
        self.size_spin.setValue(settings.font_size)
        self.size_spin.valueChanged.connect(lambda v: self._update("font_size", v))
        form.addRow("Font size", self.size_spin)

        font_color_btn = QPushButton("Pick font color")
        font_color_btn.clicked.connect(lambda: self._pick_color("font_color"))
        form.addRow(font_color_btn)

        bg_color_btn = QPushButton("Pick background color")
        bg_color_btn.clicked.connect(lambda: self._pick_color("background_color"))
        form.addRow(bg_color_btn)

        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(settings.background_opacity)
        self.opacity_slider.valueChanged.connect(lambda v: self._update("background_opacity", v))
        form.addRow("Background opacity", self.opacity_slider)

        self.refresh_spin = QSpinBox()
        self.refresh_spin.setRange(100, 5000)
        self.refresh_spin.setSingleStep(100)
        self.refresh_spin.setValue(settings.refresh_speed_ms)
        self.refresh_spin.valueChanged.connect(lambda v: self._update("refresh_speed_ms", v))
        form.addRow("Subtitle refresh speed (ms)", self.refresh_spin)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addLayout(form)

        self.setStyleSheet("""
            SettingsDialog {
                background-color: #262626;
                border: 1px solid #4a4a4a;
                border-radius: 10px;
            }
            QLabel {
                color: #eeeeee;
            }
        """)

    def _update(self, field: str, value) -> None:
        setattr(self.settings, field, value)
        self.settings.save()
        self._on_change(self.settings)

    def _pick_color(self, field: str) -> None:
        color = QColorDialog.getColor(parent=self)
        if color.isValid():
            self._update(field, color.name())

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)
