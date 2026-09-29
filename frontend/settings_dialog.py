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

from PyQt6.QtCore import QEvent, Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .settings import Settings
from .stepping_slider import SteppingSlider
from .toggle_switch import ToggleSwitch

TIER_OPTIONS = [("auto", "Auto"), ("small", "Small"), ("base", "Base"), ("tiny", "Tiny")]
ENGINE_OPTIONS = [("faster-whisper", "faster-whisper"), ("parakeet", "Parakeet TDT")]

# status -> (dot color, label). Mirrors backend/main.py's modal_setup_status
# values (see docstring there): deploying -> warming up -> ready -> alive
# (heartbeat) -> terminated, plus "stopping" (sent client-side the instant
# Stop is clicked, then confirmed by the backend's own "stopping" ->
# "terminated" pair -- see _handle_modal_stop / stop_modal in main.py).
# Labels are the base text; busy states get an animated "..." suffix from
# _tick_progress so there's visible motion while setup/teardown is in
# flight, not just a static word.
MODAL_STATUS_DISPLAY = {
    "terminated": ("#555555", "Stopped"),
    "deploying": ("#e0b400", "Starting (deploying)"),
    "warming up": ("#e0b400", "Starting (warming up)"),
    "stopping": ("#e0b400", "Stopping"),
    "ready": ("#3fbf50", "Ready"),
    "alive": ("#3fbf50", "Alive"),
}
BUSY_MODAL_STATUSES = {"deploying", "warming up", "stopping"}
PROGRESS_TICK_MS = 400

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

SECTION_LABEL_STYLE = "color: #999999; font-weight: bold; font-size: 11px; margin-top: 8px;"


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

        # Animated "..." on the Modal status label while deploying/warming
        # up/stopping, so there's visible motion during setup/teardown
        # instead of a label that looks stuck. See set_modal_status.
        self._modal_status = "terminated"
        self._status_color = MODAL_STATUS_DISPLAY["terminated"][0]
        self._status_label_base = MODAL_STATUS_DISPLAY["terminated"][1]
        self._progress_dots = 0
        self._progress_timer = QTimer(self)
        self._progress_timer.timeout.connect(self._tick_progress)

        tabs = QTabWidget()
        tabs.addTab(self._build_appearance_tab(), "Appearance")
        tabs.addTab(self._build_model_config_tab(), "Model config")
        tabs.addTab(self._build_advanced_tab(), "Advanced")

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
        """)

    # -- Appearance tab ---------------------------------------------------
    def _build_appearance_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        s = self.settings

        typography_label = QLabel("Typography")
        typography_label.setStyleSheet(SECTION_LABEL_STYLE)
        layout.addWidget(typography_label)

        type_form = QFormLayout()
        self.font_combo = QFontComboBox()
        self.font_combo.setCurrentFont(self.font_combo.currentFont().__class__(s.font_family))
        self.font_combo.currentFontChanged.connect(lambda f: self._update("font_family", f.family()))
        type_form.addRow("Font", self.font_combo)

        self.size_spin = QSpinBox()
        self.size_spin.setRange(8, 72)
        self.size_spin.setValue(s.font_size)
        self.size_spin.valueChanged.connect(lambda v: self._update("font_size", v))
        type_form.addRow("Size", self.size_spin)
        layout.addLayout(type_form)

        text_label = QLabel("Caption text")
        text_label.setStyleSheet(SECTION_LABEL_STYLE)
        text_label.setToolTip(
            "The caption text itself: its fill color, an optional highlight rectangle behind "
            "each line (like YouTube's caption background), and an outline following the "
            "shape of the letters themselves."
        )
        layout.addWidget(text_label)

        color_row = QHBoxLayout()
        self.font_color_btn = self._build_color_button("Font", "font_color", "Caption text color.")
        self.font_bg_color_btn = self._build_color_button(
            "Font background", "font_background_color",
            "Highlight rectangle behind each caption line (like YouTube's caption background) -- "
            "independent of the outline and of the overlay panel below. Pick a transparent color, "
            "or set its opacity to 0, for no background box at all."
        )
        self.outline_color_btn = self._build_color_button(
            "Outline", "outline_color",
            "Outline color following the actual shape of the caption text's letters -- helps "
            "the text stand out when its fill color is close to whatever's behind it."
        )
        color_row.addWidget(self.font_color_btn)
        color_row.addWidget(self.font_bg_color_btn)
        color_row.addWidget(self.outline_color_btn)
        layout.addLayout(color_row)

        text_form = QFormLayout()
        self.font_bg_opacity_slider = SteppingSlider(Qt.Orientation.Horizontal)
        self.font_bg_opacity_slider.setRange(0, 100)
        self.font_bg_opacity_slider.setValue(s.font_background_opacity)
        self.font_bg_opacity_slider.setToolTip("Opacity of the font background highlight (0 = no box).")
        self.font_bg_opacity_slider.valueChanged.connect(lambda v: self._update("font_background_opacity", v))
        text_form.addRow("Font background opacity", self.font_bg_opacity_slider)

        self.outline_width_spin = QDoubleSpinBox()
        self.outline_width_spin.setRange(0, 10)
        self.outline_width_spin.setSingleStep(0.1)
        self.outline_width_spin.setDecimals(1)
        self.outline_width_spin.setSuffix(" px")
        self.outline_width_spin.setValue(s.outline_width)
        self.outline_width_spin.setToolTip("Outline width around the caption text's letters, in pixels (0 = no outline).")
        self.outline_width_spin.valueChanged.connect(lambda v: self._update("outline_width", v))
        text_form.addRow("Text outline width", self.outline_width_spin)
        layout.addLayout(text_form)

        window_label = QLabel("App window")
        window_label.setStyleSheet(SECTION_LABEL_STYLE)
        window_label.setToolTip("The floating overlay panel behind the caption text -- its background color and opacity.")
        layout.addWidget(window_label)

        window_row = QHBoxLayout()
        self.bg_color_btn = self._build_color_button("Background", "background_color", "Overlay panel background color.")
        window_row.addWidget(self.bg_color_btn)
        window_row.addStretch(1)
        layout.addLayout(window_row)

        display_form = QFormLayout()
        self.opacity_slider = SteppingSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(s.background_opacity)
        self.opacity_slider.setToolTip("Opacity of the overlay panel's background.")
        self.opacity_slider.valueChanged.connect(lambda v: self._update("background_opacity", v))
        display_form.addRow("Window background opacity", self.opacity_slider)
        layout.addLayout(display_form)

        layout.addStretch(1)
        return page

    def _build_secret_field(self, placeholder: str, field: str, initial: str) -> tuple[QLineEdit, QHBoxLayout]:
        """A password-masked QLineEdit with an eye-icon toggle button next
        to it, so the value can be checked without retyping it -- used for
        the Modal Token ID/Secret fields."""
        edit = QLineEdit(initial)
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        edit.setPlaceholderText(placeholder)
        edit.editingFinished.connect(lambda: self._update(field, edit.text()))

        reveal_btn = QPushButton("\U0001F441")  # eye
        reveal_btn.setObjectName("revealButton")
        reveal_btn.setCheckable(True)
        reveal_btn.setToolTip("Show/hide")
        reveal_btn.setFixedWidth(28)
        reveal_btn.toggled.connect(
            lambda checked: edit.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)
        )

        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(edit)
        row.addWidget(reveal_btn)
        return edit, row

    def _build_color_button(self, label: str, field: str, tooltip: str = "") -> QPushButton:
        btn = QPushButton(label)
        btn.setObjectName("colorButton")
        if tooltip:
            btn.setToolTip(tooltip)
        self._paint_color_button(btn, getattr(self.settings, field))
        btn.clicked.connect(lambda: self._pick_color(field, btn))
        return btn

    @staticmethod
    def _paint_color_button(btn: QPushButton, hex_color: str) -> None:
        color = QColor(hex_color)
        text_color = "#000000" if color.lightnessF() > 0.5 else "#ffffff"
        # rgba(...), not the raw hex string: Qt's QSS color parser doesn't
        # understand the #AARRGGBB form QColor.name(HexArgb) produces (QSS
        # expects #RRGGBB or rgba(r,g,b,a)), so a translucent pick would
        # otherwise paint the swatch as a wrong/garbled color.
        bg = f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alphaF():.3f})"
        btn.setStyleSheet(
            f"QPushButton#colorButton {{"
            f"  background-color: {bg}; color: {text_color};"
            f"  border: 1px solid #666666; border-radius: 4px; padding: 6px;"
            f"}}"
        )

    # -- Advanced tab -------------------------------------------------------
    def _build_advanced_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        s = self.settings

        form = QFormLayout()
        self.refresh_spin = QSpinBox()
        self.refresh_spin.setRange(100, 5000)
        self.refresh_spin.setSingleStep(100)
        self.refresh_spin.setValue(s.refresh_speed_ms)
        self.refresh_spin.valueChanged.connect(lambda v: self._update("refresh_speed_ms", v))
        form.addRow("Subtitle refresh speed (ms)", self.refresh_spin)
        layout.addLayout(form)

        self.advanced_toggle = self._add_toggle_row(
            layout, "Show advanced diagnostics under the caption", s.advanced_mode, "advanced_mode"
        )
        self.persist_toggle = self._add_toggle_row(
            layout,
            "Persist subtitles (append instead of replace)",
            s.persist_subtitles,
            "persist_subtitles",
            tooltip="On: new subtitles append below older ones, scrollable.\nOff: each new subtitle replaces the last one shown.",
        )
        self.auto_hide_header_toggle = self._add_toggle_row(
            layout, "Auto-hide header (show on hover)", s.auto_hide_header, "auto_hide_header"
        )
        self.auto_hide_footer_toggle = self._add_toggle_row(
            layout, "Auto-hide footer (show on hover)", s.auto_hide_footer, "auto_hide_footer"
        )

        layout.addStretch(1)
        return page

    def _add_toggle_row(self, layout: QVBoxLayout, label: str, checked: bool, field: str, tooltip: str = "") -> ToggleSwitch:
        row = QHBoxLayout()
        toggle = ToggleSwitch()
        toggle.setChecked(checked)
        toggle.toggled.connect(lambda v: self._update(field, v))
        text = QLabel(label)
        if tooltip:
            text.setToolTip(tooltip)
            toggle.setToolTip(tooltip)
        row.addWidget(toggle)
        row.addWidget(text)
        row.addStretch(1)
        layout.addLayout(row)
        return toggle

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
        s = self.settings

        creds_label = QLabel("Modal API credentials (optional)")
        creds_label.setStyleSheet(SECTION_LABEL_STYLE)
        creds_label.setToolTip(
            "Only needed if the backend container has no ambient Modal CLI login of its own -- "
            "the normal case, since Docker doesn't inherit the host's `modal token set`. "
            "Get these from modal.com -> Settings -> API Tokens. Left blank, setup falls back to "
            "whatever ambient auth the container happens to have."
        )
        layout.addWidget(creds_label)

        creds_form = QFormLayout()
        self.modal_token_id_edit, id_row = self._build_secret_field("ak-...", "modal_token_id", s.modal_token_id)
        creds_form.addRow("Token ID", id_row)

        self.modal_token_secret_edit, secret_row = self._build_secret_field("as-...", "modal_token_secret", s.modal_token_secret)
        creds_form.addRow("Token secret", secret_row)
        layout.addLayout(creds_form)

        self.modal_status_label = QLabel("")
        layout.addWidget(self.modal_status_label)

        self.modal_error_label = QLabel("")
        self.modal_error_label.setWordWrap(True)
        self.modal_error_label.setStyleSheet("color: #e0392b; font-size: 11px;")
        self.modal_error_label.setVisible(False)
        layout.addWidget(self.modal_error_label)

        self.modal_setup_btn = QPushButton("Set up Modal instance")
        self.modal_setup_btn.clicked.connect(self._handle_modal_setup)
        layout.addWidget(self.modal_setup_btn)

        self.modal_stop_btn = QPushButton("Stop")
        self.modal_stop_btn.clicked.connect(self._handle_modal_stop)
        layout.addWidget(self.modal_stop_btn)

        return self.cloud_page

    # -- live updates from the backend (modal_setup_status events) --------
    def set_modal_status(self, status: str, error: str | None = None) -> None:
        self._modal_status = status
        self._status_color, self._status_label_base = MODAL_STATUS_DISPLAY.get(
            status, MODAL_STATUS_DISPLAY["terminated"]
        )
        is_busy = status in BUSY_MODAL_STATUSES
        if is_busy and not self._progress_timer.isActive():
            self._progress_dots = 0
            self._progress_timer.start(PROGRESS_TICK_MS)
        elif not is_busy:
            self._progress_timer.stop()
        self._refresh_modal_status_text()

        # A fresh setup/stop attempt (is_busy) clears any previous error
        # rather than leaving it showing next to a status that's since
        # moved on.
        if error:
            self.modal_error_label.setText(f"Setup failed: {error}")
            self.modal_error_label.setVisible(True)
        elif is_busy:
            self.modal_error_label.setVisible(False)

        # Setup only from a fully stopped state; Stop only once Modal is
        # actually usable (not mid-setup/mid-teardown) -- avoids racing a
        # second setup/stop request against one already in flight.
        self.modal_setup_btn.setEnabled(status == "terminated")
        self.modal_setup_btn.setText("Setting up..." if is_busy and status != "stopping" else "Set up Modal instance")
        self.modal_stop_btn.setEnabled(status in ("ready", "alive"))
        self.modal_stop_btn.setText("Stopping..." if status == "stopping" else "Stop")

    def _refresh_modal_status_text(self) -> None:
        dots = "." * (self._progress_dots % 4) if self._modal_status in BUSY_MODAL_STATUSES else ""
        self.modal_status_label.setText(f"● {self._status_label_base}{dots}")
        self.modal_status_label.setStyleSheet(f"color: {self._status_color};")

    def _tick_progress(self) -> None:
        self._progress_dots += 1
        self._refresh_modal_status_text()

    # -- internals -----------------------------------------------------------
    def _update(self, field: str, value) -> None:
        setattr(self.settings, field, value)
        self.settings.save()
        self._on_change(self.settings)

    def _pick_color(self, field: str, btn: QPushButton) -> None:
        initial = QColor(getattr(self.settings, field))
        for i, hex_color in enumerate(self.settings.custom_colors[:16]):
            QColorDialog.setCustomColor(i, QColor(hex_color))

        color = QColorDialog.getColor(
            initial, self, "Pick a color", QColorDialog.ColorDialogOption.ShowAlphaChannel
        )
        if not color.isValid():
            return

        # Only bother with the 8-digit #AARRGGBB form when there's real
        # transparency to keep -- otherwise the plain 6-digit form, so
        # existing (pre-transparency) storage/QSS interpolation of these
        # fields is untouched for the common fully-opaque case.
        hex_value = color.name() if color.alpha() == 255 else color.name(QColor.NameFormat.HexArgb)
        self._update(field, hex_value)
        self._paint_color_button(btn, hex_value)
        self._remember_custom_color(hex_value)

    def _remember_custom_color(self, hex_color: str) -> None:
        # QColorDialog's own custom-color swatches are process-lifetime
        # only; persisting our own list (and repopulating the dialog's
        # swatches from it above) is what makes them survive a restart.
        colors = [c for c in self.settings.custom_colors if c.lower() != hex_color.lower()]
        colors.insert(0, hex_color)
        self.settings.custom_colors = colors[:16]
        self.settings.save()

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
        # Read the fields directly rather than relying on editingFinished
        # having already synced self.settings -- correct in practice
        # (clicking this button focus-outs the line edits first), but not
        # worth depending on. Also updates settings/persists here as a
        # backstop for the same reason.
        token_id = self.modal_token_id_edit.text()
        token_secret = self.modal_token_secret_edit.text()
        self._update("modal_token_id", token_id)
        self._update("modal_token_secret", token_secret)

        # Optimistic: show progress immediately rather than waiting for the
        # round trip (control message -> backend -> to_thread deploy call
        # -> its own first on_status("deploying") -> event back over the
        # WebSocket) before anything visibly happens.
        self.set_modal_status("deploying")
        self._actions["on_modal_setup_requested"](token_id, token_secret)

    def _handle_modal_stop(self) -> None:
        self.set_modal_status("stopping")
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

    def event(self, event) -> bool:
        # Closes on a click anywhere outside this panel -- including
        # another application, the desktop, or the taskbar. An
        # application-level event filter (the previous approach) only
        # ever sees events for THIS app's own windows, so a click on
        # anything else never reached it and the panel stayed open.
        # WindowDeactivate fires whenever this top-level window loses
        # focus for any reason, which covers all of those cases uniformly.
        if event.type() == QEvent.Type.WindowDeactivate and QApplication.activeModalWidget() is None:
            # The activeModalWidget() guard matters: without it, opening a
            # QColorDialog from this panel (a separate window, so it
            # deactivates this one) would close the whole panel the
            # instant the color picker appeared.
            self.close()
        return super().event(event)
