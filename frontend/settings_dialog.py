"""Settings panel: appearance and model-config controls, applied live to
the overlay and persisted via Settings.save().

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
    QHBoxLayout,
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

COMBO_STYLE = """
    QComboBox {
        color: #eeeeee;
        background-color: #333333;
        border: 1px solid #5a5a5a;
        border-radius: 4px;
        padding: 3px 6px;
    }
    QComboBox:disabled {
        color: #888888;
        background-color: #2a2a2a;
        border: 1px solid #444444;
    }
"""

# Renders a QCheckBox as a pill-shaped on/off toggle switch (grey = off,
# green = on) rather than a tick-box -- used for the boolean settings below,
# not just Advanced mode, so they read consistently as switches.
TOGGLE_STYLE = """
    QCheckBox::indicator {
        width: 34px;
        height: 18px;
        border-radius: 9px;
        background-color: #555555;
        border: 1px solid #444444;
    }
    QCheckBox::indicator:checked {
        background-color: #3fbf50;
        border: 1px solid #2e8f3d;
    }
"""


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
        tabs.addTab(self._build_model_config_tab(), "Model config")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(tabs)

        self.set_modal_status(modal_status)
        self._update_mode_pages()

        self.setStyleSheet(f"""
            SettingsDialog {{
                background-color: #262626;
                border: 1px solid #4a4a4a;
                border-radius: 10px;
            }}
            QLabel {{ color: #eeeeee; }}
            QTabWidget::pane {{ border: 1px solid #4a4a4a; border-radius: 6px; }}
            QTabBar::tab {{
                background: #333333;
                color: #cccccc;
                padding: 6px 12px;
            }}
            QTabBar::tab:selected {{ background: #444444; color: #ffffff; }}
            {COMBO_STYLE}
            {TOGGLE_STYLE}
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

        self.persist_check = QCheckBox("Persist subtitles (append instead of replace)")
        self.persist_check.setToolTip(
            "On: new subtitles append below older ones, scrollable.\n"
            "Off: each new subtitle replaces the last one shown."
        )
        self.persist_check.setChecked(s.persist_subtitles)
        self.persist_check.toggled.connect(lambda v: self._update("persist_subtitles", v))
        form.addRow(self.persist_check)

        return page

    # -- Model config tab ---------------------------------------------------
    def _build_model_config_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        s = self.settings

        form = QFormLayout()
        self.latency_spin = QDoubleSpinBox()
        self.latency_spin.setRange(1, 20)
        self.latency_spin.setSingleStep(1)
        self.latency_spin.setSuffix(" s")
        self.latency_spin.setValue(s.acceptable_latency_s)
        self.latency_spin.valueChanged.connect(self._handle_latency_change)
        form.addRow("Acceptable delay", self.latency_spin)
        layout.addLayout(form)

        radio_row = QHBoxLayout()
        self.local_radio = QRadioButton("Local")
        self.cloud_radio = QRadioButton("Cloud (Modal.com GPU)")
        self.cloud_radio.setChecked(s.inference_mode == "modal")
        self.local_radio.setChecked(s.inference_mode != "modal")
        self.local_radio.toggled.connect(self._handle_inference_mode_change)
        radio_row.addWidget(self.local_radio)
        radio_row.addWidget(self.cloud_radio)
        radio_row.addStretch(1)
        layout.addLayout(radio_row)

        layout.addWidget(self._build_local_page())
        layout.addWidget(self._build_cloud_page())
        layout.addStretch(1)
        return page

    def _build_local_page(self) -> QWidget:
        self.local_page = QWidget()
        form = QFormLayout(self.local_page)
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

        self._update_tier_enabled()
        return self.local_page

    def _build_cloud_page(self) -> QWidget:
        self.cloud_page = QWidget()
        layout = QVBoxLayout(self.cloud_page)
        layout.setContentsMargins(0, 4, 0, 0)

        self.modal_status_label = QLabel("")
        layout.addWidget(self.modal_status_label)

        self.modal_setup_btn = QPushButton("Set up Modal instance")
        self.modal_setup_btn.clicked.connect(self._handle_modal_setup)
        layout.addWidget(self.modal_setup_btn)

        self.modal_stop_btn = QPushButton("Stop")
        self.modal_stop_btn.clicked.connect(self._handle_modal_stop)
        layout.addWidget(self.modal_stop_btn)

        return self.cloud_page

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

    def _update_mode_pages(self) -> None:
        is_cloud = self.cloud_radio.isChecked()
        self.local_page.setVisible(not is_cloud)
        self.cloud_page.setVisible(is_cloud)

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
        self._update_mode_pages()
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
