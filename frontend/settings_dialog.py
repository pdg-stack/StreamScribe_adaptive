"""Settings panel: appearance, model/engine, and cloud (Modal) controls,
applied live to the overlay and persisted via Settings.save().

Deliberately a frameless QWidget, not a QDialog/.exec(): the main overlay is
WindowStaysOnTopHint, so a plain QDialog (no matching stays-on-top flag)
ends up rendered *behind* it -- unreachable to clicks and looking merged
into the overlay. This also gets its own WindowStaysOnTopHint, is parented
to the overlay so it doesn't get an independent taskbar/minimize identity,
and stays fully opaque. Toggled open/closed by the overlay's gear icon and
closed on an outside click (see OverlayWindow._toggle_settings_panel).

`actions` bundles the callbacks that reach past styling into the running
pipeline (engine/tier/latency/Modal setup-stop) -- passed as a dict rather
than half a dozen separate constructor params, since OverlayWindow is the
one place that already holds all of them.
"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .settings import Settings

TIER_OPTIONS = [("auto", "Auto"), ("small", "Small"), ("base", "Base"), ("tiny", "Tiny")]
ENGINE_OPTIONS = [("faster-whisper", "faster-whisper"), ("parakeet", "Parakeet TDT")]

# status -> (dot color, label). Mirrors backend/main.py's modal_setup_status
# values (see docstring there): deploying -> warming up -> ready -> alive
# (heartbeat) -> terminated.
MODAL_STATUS_DISPLAY = {
    "terminated": ("#555555", "Not running"),
    "deploying": ("#e0b400", "Deploying..."),
    "warming up": ("#e0b400", "Warming up..."),
    "ready": ("#3fbf50", "Ready"),
    "alive": ("#3fbf50", "Alive"),
}


class SettingsDialog(QWidget):
    def __init__(
        self,
        settings: Settings,
        on_change: Callable[[Settings], None],
        actions: dict[str, Callable],
        modal_status: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.settings = settings
        self._on_change = on_change
        self._actions = actions

        tabs = QTabWidget()
        tabs.addTab(self._build_appearance_tab(), "Appearance")
        tabs.addTab(self._build_model_tab(), "Model")
        tabs.addTab(self._build_cloud_tab(), "Cloud (Modal)")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(tabs)

        self.set_modal_status(modal_status)

        self.setStyleSheet("""
            SettingsDialog {
                background-color: #262626;
                border: 1px solid #4a4a4a;
                border-radius: 10px;
            }
            QLabel { color: #eeeeee; }
            QTabWidget::pane { border: 1px solid #4a4a4a; border-radius: 6px; }
            QTabBar::tab {
                background: #333333;
                color: #cccccc;
                padding: 6px 12px;
            }
            QTabBar::tab:selected { background: #444444; color: #ffffff; }
        """)

    # -- Appearance tab ---------------------------------------------------
    def _build_appearance_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        s = self.settings

        self.font_combo = QFontComboBox()
        self.font_combo.setCurrentFont(self.font_combo.currentFont().__class__(s.font_family))
        self.font_combo.currentFontChanged.connect(lambda f: self._update("font_family", f.family()))
        form.addRow("Font", self.font_combo)

        self.size_spin = QSpinBox()
        self.size_spin.setRange(10, 72)
        self.size_spin.setValue(s.font_size)
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
        self.opacity_slider.setValue(s.background_opacity)
        self.opacity_slider.valueChanged.connect(lambda v: self._update("background_opacity", v))
        form.addRow("Background opacity", self.opacity_slider)

        self.refresh_spin = QSpinBox()
        self.refresh_spin.setRange(100, 5000)
        self.refresh_spin.setSingleStep(100)
        self.refresh_spin.setValue(s.refresh_speed_ms)
        self.refresh_spin.valueChanged.connect(lambda v: self._update("refresh_speed_ms", v))
        form.addRow("Subtitle refresh speed (ms)", self.refresh_spin)

        self.advanced_check = QCheckBox("Show advanced diagnostics under the caption")
        self.advanced_check.setChecked(s.advanced_mode)
        self.advanced_check.toggled.connect(lambda v: self._update("advanced_mode", v))
        form.addRow(self.advanced_check)

        return page

    # -- Model tab ---------------------------------------------------------
    def _build_model_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        s = self.settings

        self.engine_combo = QComboBox()
        for code, name in ENGINE_OPTIONS:
            self.engine_combo.addItem(name, userData=code)
        self._set_combo_code(self.engine_combo, s.engine)
        self.engine_combo.currentIndexChanged.connect(self._handle_engine_change)
        form.addRow("Engine", self.engine_combo)

        self.tier_combo = QComboBox()
        for code, name in TIER_OPTIONS:
            self.tier_combo.addItem(name, userData=code)
        self._set_combo_code(self.tier_combo, s.tier)
        self.tier_combo.currentIndexChanged.connect(self._handle_tier_change)
        form.addRow("Model size", self.tier_combo)

        self.latency_spin = QDoubleSpinBox()
        self.latency_spin.setRange(1, 20)
        self.latency_spin.setSingleStep(1)
        self.latency_spin.setSuffix(" s")
        self.latency_spin.setValue(s.acceptable_latency_s)
        self.latency_spin.valueChanged.connect(self._handle_latency_change)
        form.addRow("Acceptable delay", self.latency_spin)

        self._update_tier_enabled()
        return page

    # -- Cloud (Modal) tab ---------------------------------------------------
    def _build_cloud_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.local_radio = QRadioButton("Local (CPU)")
        self.modal_radio = QRadioButton("Modal (cloud GPU)")
        self.modal_radio.setChecked(self.settings.inference_mode == "modal")
        self.local_radio.setChecked(self.settings.inference_mode != "modal")
        self.local_radio.toggled.connect(self._handle_inference_mode_change)
        layout.addWidget(self.local_radio)
        layout.addWidget(self.modal_radio)

        self.modal_status_label = QLabel("")
        layout.addWidget(self.modal_status_label)

        self.modal_setup_btn = QPushButton("Set up Modal instance")
        self.modal_setup_btn.clicked.connect(self._handle_modal_setup)
        layout.addWidget(self.modal_setup_btn)

        self.modal_stop_btn = QPushButton("Stop")
        self.modal_stop_btn.clicked.connect(self._handle_modal_stop)
        layout.addWidget(self.modal_stop_btn)

        layout.addStretch(1)
        return page

    # -- live updates from the backend (modal_setup_status events) --------
    def set_modal_status(self, status: str) -> None:
        color, label = MODAL_STATUS_DISPLAY.get(status, MODAL_STATUS_DISPLAY["terminated"])
        self.modal_status_label.setText(f"● {label}")
        self.modal_status_label.setStyleSheet(f"color: {color};")
        self.modal_setup_btn.setEnabled(status == "terminated")
        self.modal_stop_btn.setEnabled(status in ("deploying", "warming up", "ready", "alive"))

    # -- internals -----------------------------------------------------------
    def _update(self, field: str, value) -> None:
        setattr(self.settings, field, value)
        self.settings.save()
        self._on_change(self.settings)

    def _pick_color(self, field: str) -> None:
        color = QColorDialog.getColor(parent=self)
        if color.isValid():
            self._update(field, color.name())

    def _update_tier_enabled(self) -> None:
        self.tier_combo.setEnabled(self.engine_combo.currentData() == "faster-whisper")

    def _handle_engine_change(self, index: int) -> None:
        code = self.engine_combo.itemData(index)
        self._update("engine", code)
        self._update_tier_enabled()
        self._actions["on_engine_change"](code)

    def _handle_tier_change(self, index: int) -> None:
        code = self.tier_combo.itemData(index)
        self._update("tier", code)
        self._actions["on_tier_change"](code)

    def _handle_latency_change(self, value: float) -> None:
        self._update("acceptable_latency_s", value)
        self._actions["on_latency_change"](value)

    def _handle_inference_mode_change(self, local_checked: bool) -> None:
        mode = "local" if local_checked else "modal"
        self._update("inference_mode", mode)
        if mode == "modal":
            # Modal only runs faster-whisper (see plan) -- force the engine
            # choice so there's nothing to silently route around.
            self._set_combo_code(self.engine_combo, "faster-whisper")
        else:
            self._actions["on_modal_stop_requested"]()

    def _handle_modal_setup(self) -> None:
        self.modal_setup_btn.setEnabled(False)
        self._actions["on_modal_setup_requested"]()

    def _handle_modal_stop(self) -> None:
        self._actions["on_modal_stop_requested"]()

    @staticmethod
    def _set_combo_code(combo: QComboBox, code: str) -> None:
        idx = combo.findData(code)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)
