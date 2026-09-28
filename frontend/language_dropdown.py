"""A combo-box-like control that opens a 2-column popup of choices,
instead of QComboBox's native single-column popup. Setting a custom
QListView with wrapping on a QComboBox to force multi-column layout was
tried first and confirmed to break selection outright -- the popup
flickered and closed immediately on open. True multi-column QComboBox
popups are a known-fragile area of Qt (the popup-positioning/sizing logic
assumes a scrollable single-column list).

The popup is Qt.WindowType.Tool with manual outside-click detection,
mirroring the settings panel's own proven pattern, rather than
Qt.WindowType.Popup's automatic dismiss-on-outside-click -- kept for
consistency with that pattern once it was already being built out to
chase down a native crash that turned out to be unrelated (a missing-
attribute bug in OverlayWindow.eventFilter, fixed there).
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QGridLayout, QPushButton, QWidget

POPUP_STYLE = """
    QWidget#languageDropdownPopup {
        background-color: #262626;
        border: 1px solid #4a4a4a;
        border-radius: 8px;
    }
    QPushButton {
        background: transparent;
        color: #eeeeee;
        border: none;
        text-align: left;
        padding: 4px 10px;
        border-radius: 4px;
        font-size: 12px;
    }
    QPushButton:hover {
        background-color: rgba(255, 255, 255, 35);
    }
"""


class LanguageDropdown(QPushButton):
    """Same currentData()/set_current_code() shape as the QComboBox usage
    it replaces, plus codeChanged in place of currentIndexChanged."""

    codeChanged = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[tuple[str, str]] = []
        self._current_code: str | None = None
        self._popup: QWidget | None = None
        self.clicked.connect(self._toggle_popup)

    def set_items(self, items: list[tuple[str, str]]) -> None:
        self._items = items
        self._refresh_text()

    def clear(self) -> None:
        self._items = []

    def addItem(self, text: str, userData=None) -> None:
        self._items.append((userData, text))

    def set_current_code(self, code: str) -> None:
        self._current_code = code
        self._refresh_text()

    def currentData(self) -> str | None:
        return self._current_code

    def setItemText(self, index: int, text: str) -> None:
        """Matches the one QComboBox call site that rewrites the current
        item's label in place (the "Auto Detect (<language>)" suffix) --
        index is always 0 there (Auto Detect is always first)."""
        if index == 0 and self._items:
            self._items[0] = (self._items[0][0], text)
        if self._current_code == (self._items[0][0] if self._items else None):
            self.setText(text)

    def _refresh_text(self) -> None:
        label = next((name for c, name in self._items if c == self._current_code), self._current_code or "")
        self.setText(label)

    def _toggle_popup(self) -> None:
        if self._popup is not None:
            self._close_popup()
            return
        if not self._items:
            return

        popup = QWidget(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        popup.setObjectName("languageDropdownPopup")
        popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        popup.setStyleSheet(POPUP_STYLE)

        grid = QGridLayout(popup)
        grid.setContentsMargins(6, 6, 6, 6)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(1)

        rows = max(-(-len(self._items) // 2), 1)  # ceil(count / 2)
        for i, (code, name) in enumerate(self._items):
            btn = QPushButton(name)
            btn.clicked.connect(lambda checked=False, c=code: self._select(c))
            row, col = i % rows, i // rows
            grid.addWidget(btn, row, col)

        popup.adjustSize()
        anchor = self.mapToGlobal(self.rect().bottomLeft())
        popup.move(anchor)
        popup.show()
        popup.installEventFilter(self)
        self._popup = popup
        QApplication.instance().installEventFilter(self)

    def _close_popup(self) -> None:
        if self._popup is not None:
            QApplication.instance().removeEventFilter(self)
            self._popup.close()
            self._popup.deleteLater()
            self._popup = None

    def _select(self, code: str) -> None:
        self._close_popup()
        if code != self._current_code:
            self._current_code = code
            self._refresh_text()
            self.codeChanged.emit(code)

    def eventFilter(self, obj, event) -> bool:
        if self._popup is not None and event.type() == QEvent.Type.MouseButtonPress:
            pos = event.globalPosition().toPoint()
            inside_popup = self._popup.frameGeometry().contains(pos)
            inside_self = self.rect().contains(self.mapFromGlobal(pos))
            if not inside_popup and not inside_self:
                self._close_popup()
        return super().eventFilter(obj, event)
