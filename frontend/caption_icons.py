"""Small hand-drawn line icons (copy, clear/trash) for the caption panel's
corner toolbar, in place of emoji glyphs -- emoji render at inconsistent
sizes/weights across fonts, which was the "not the same size" complaint.
Both icons share one fixed size and pop briefly on click via a scale
animation, purely for click feedback.
"""

from __future__ import annotations

from PyQt6.QtCore import QEasingCurve, QPointF, QPropertyAnimation, QRectF, Qt, pyqtProperty
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QAbstractButton, QWidget

ICON_SIZE = 22


class LineIconButton(QAbstractButton):
    def __init__(self, kind: str, color: str = "#dddddd", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind = kind  # "copy" | "clear"
        self._color = QColor(color)
        self._scale = 1.0
        self.setFixedSize(ICON_SIZE, ICON_SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._anim = QPropertyAnimation(self, b"iconScale")
        self._anim.setDuration(220)
        self._anim.setKeyValues([(0.0, 1.0), (0.4, 1.35), (1.0, 1.0)])
        self._anim.setEasingCurve(QEasingCurve.Type.OutBack)
        self.clicked.connect(self._anim.start)

    def _get_scale(self) -> float:
        return self._scale

    def _set_scale(self, value: float) -> None:
        self._scale = value
        self.update()

    iconScale = pyqtProperty(float, _get_scale, _set_scale)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = self.rect().center()
        painter.translate(center)
        painter.scale(self._scale, self._scale)
        painter.translate(-center)

        pen = QPen(self._color)
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if self._kind == "copy":
            painter.drawRoundedRect(QRectF(4, 7, 11, 11), 2, 2)
            painter.drawRoundedRect(QRectF(7, 4, 11, 11), 2, 2)
        else:  # clear (trash can)
            painter.drawLine(QPointF(5, 7), QPointF(17, 7))
            painter.drawLine(QPointF(9, 7), QPointF(9.5, 4.5))
            painter.drawLine(QPointF(13, 7), QPointF(12.5, 4.5))
            painter.drawLine(QPointF(9.5, 4.5), QPointF(12.5, 4.5))
            painter.drawRoundedRect(QRectF(6.5, 7, 9, 11), 1.5, 1.5)
            painter.drawLine(QPointF(9, 10), QPointF(9, 15))
            painter.drawLine(QPointF(11, 10), QPointF(11, 15))
