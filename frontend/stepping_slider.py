"""A QSlider whose groove click-and-hold repeatedly steps toward the
clicked position, like a scrollbar's page region, instead of jumping
there immediately and then sitting idle for the rest of the hold. Plain
QSlider (via Qt's default style hints) sets the value once on press and
does nothing further unless the mouse actually moves -- confirmed with a
held, stationary press in testing -- which doesn't satisfy "press and hold
keeps changing the value" the way QSpinBox's up/down buttons already do
natively. Clicking directly on the handle still drags normally.
"""

from __future__ import annotations

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QSlider, QStyle, QStyleOptionSlider

INITIAL_DELAY_MS = 300
REPEAT_INTERVAL_MS = 60


class SteppingSlider(QSlider):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._target_value: int | None = None
        self._repeat_timer = QTimer(self)
        self._repeat_timer.setInterval(INITIAL_DELAY_MS)
        self._repeat_timer.timeout.connect(self._step_once)

    def _style_option(self) -> QStyleOptionSlider:
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        return opt

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        opt = self._style_option()
        handle_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderHandle, self
        )
        if handle_rect.contains(event.position().toPoint()):
            super().mousePressEvent(event)  # normal drag-the-handle behavior
            return

        self._target_value = self._value_at(event.position().toPoint())
        self._repeat_timer.setInterval(INITIAL_DELAY_MS)
        self._step_once()
        self._repeat_timer.start()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._repeat_timer.stop()
        self._target_value = None
        super().mouseReleaseEvent(event)

    def _step_once(self) -> None:
        if self._target_value is None:
            self._repeat_timer.stop()
            return
        self._repeat_timer.setInterval(REPEAT_INTERVAL_MS)  # fast repeat after the first, deliberate tick
        step = self.pageStep() or 1
        value = self.value()
        if value < self._target_value:
            self.setValue(min(value + step, self._target_value))
        elif value > self._target_value:
            self.setValue(max(value - step, self._target_value))
        if self.value() == self._target_value:
            self._repeat_timer.stop()

    def _value_at(self, pos) -> int:
        opt = self._style_option()
        groove_rect = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderGroove, self)
        handle_rect = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderHandle, self)

        if self.orientation() == Qt.Orientation.Horizontal:
            handle_len = handle_rect.width()
            slider_min, slider_max = groove_rect.x(), groove_rect.right() - handle_len + 1
            click_pos = pos.x() - handle_len // 2
        else:
            handle_len = handle_rect.height()
            slider_min, slider_max = groove_rect.y(), groove_rect.bottom() - handle_len + 1
            click_pos = pos.y() - handle_len // 2

        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), click_pos - slider_min, slider_max - slider_min,
            opt.upsideDown,
        )
