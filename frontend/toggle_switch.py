"""A small iOS/Android-style toggle switch: a pill track that fills with
color when checked, and a circular knob that sits left when unchecked and
right when checked. Drop-in for QCheckBox where used as a boolean switch
(same isChecked()/setChecked()/toggled API) -- a plain QCheckBox's tick-box
look doesn't read as a switch, which is what Settings' on/off options are.
"""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QAbstractButton, QWidget

TRACK_ON = QColor("#3fbf50")
TRACK_OFF = QColor("#555555")
KNOB_COLOR = QColor("#ffffff")


class ToggleSwitch(QAbstractButton):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(38, 20)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        track_rect = QRectF(0, 0, self.width(), self.height())
        painter.setBrush(TRACK_ON if self.isChecked() else TRACK_OFF)
        painter.drawRoundedRect(track_rect, self.height() / 2, self.height() / 2)

        knob_diameter = self.height() - 4
        knob_x = self.width() - knob_diameter - 2 if self.isChecked() else 2
        painter.setBrush(KNOB_COLOR)
        painter.drawEllipse(QRectF(knob_x, 2, knob_diameter, knob_diameter))
