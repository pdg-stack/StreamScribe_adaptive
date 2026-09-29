"""Custom-painted, scrollable caption renderer.

Replaces QTextEdit's rich-text/HTML rendering. Qt's rich-text engine
(confirmed empirically, not assumed) silently drops `border` and
`text-shadow` on a styled span/div, and has no `-webkit-text-stroke`
equivalent at all -- so there is no way to get a real per-glyph text
outline, or a background rectangle sized independently to each line, by
feeding it HTML. Both are first-class here because the glyphs are painted
directly: a QPainterPath built from the text is filled (the font color)
and optionally stroked (the outline, following the actual letterforms,
not a box around them), and an optional highlight rectangle is drawn
behind each line from its own measured text width -- not the widget's
full window width the way an HTML block/table background would be.

Word-wrap and scroll-to-bottom are computed synchronously here (a plain
Python layout pass over measured QFontMetrics line widths), which also
sidesteps the async-document-settling timing issues QTextEdit had for
reliably landing at the true scroll maximum.
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QAbstractScrollArea, QFrame

SIDE_MARGIN = 8
TOP_MARGIN = 4
PARAGRAPH_GAP = 4  # extra gap between entries, on top of normal line spacing -- kept small/"paragraph-like" on purpose


@dataclass
class _Paragraph:
    text: str
    color: QColor
    font_size: int
    italic: bool = False


@dataclass
class _LaidLine:
    text: str
    color: QColor
    font: QFont
    y: int
    height: int


class CaptionView(QAbstractScrollArea):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._base_family = "Segoe UI"
        self._paragraphs: list[_Paragraph] = []
        self._plain_text = ""
        self._lines: list[_LaidLine] = []
        self._content_height = 0
        self._font_background: QColor | None = None
        self._outline_color = QColor("#444444")
        self._outline_width = 0

        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Always visible, with a permanently reserved (not overlay) strip
        # -- and always in the wider "hover" width, not just on hover --
        # so it's unambiguous whether/where the view is scrolled.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.viewport().setAutoFillBackground(False)
        self.setStyleSheet("""
            QAbstractScrollArea { background: transparent; border: none; }
            QScrollBar:vertical {
                width: 16px;
                background: rgba(255, 255, 255, 10);
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 90);
                min-height: 24px;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical:hover { background: rgba(255, 255, 255, 140); }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
        """)

    # -- public API ---------------------------------------------------------
    def set_base_font(self, font: QFont) -> None:
        self._base_family = font.family()

    def to_plain_text(self) -> str:
        return self._plain_text

    def render(
        self,
        paragraphs: list[tuple[str, str, int, bool]],
        plain_text: str,
        font_background: QColor | None,
        outline_color: QColor,
        outline_width: int,
    ) -> None:
        """paragraphs: list of (text, hex_or_rgba_color, font_size, italic).
        Caller (OverlayWindow) is responsible for turning caption entries
        into this flat structure -- see _build_paragraphs there -- so this
        class only has to know how to lay out and paint text, not the
        primary/secondary-translation/delayed-marker content model."""
        self._paragraphs = [_Paragraph(text, QColor(color), size, italic) for text, color, size, italic in paragraphs]
        self._plain_text = plain_text
        self._font_background = font_background
        self._outline_color = outline_color
        self._outline_width = outline_width
        self._relayout()
        # Unconditional, not "only if already at the bottom": that smart-
        # follow version kept proving unreliable in practice, so this
        # always snaps to the bottom on every render. Safe to do
        # synchronously (unlike QTextEdit's setHtml): _relayout() just
        # computed the true content height itself, nothing to wait for.
        vsb = self.verticalScrollBar()
        vsb.setValue(vsb.maximum())

    # -- layout ---------------------------------------------------------
    def _relayout(self) -> None:
        width = max(self.viewport().width() - 2 * SIDE_MARGIN, 10)
        lines: list[_LaidLine] = []
        y = TOP_MARGIN
        for para in self._paragraphs:
            font = QFont(self._base_family, para.font_size)
            font.setItalic(para.italic)
            metrics = QFontMetrics(font)
            wrapped = self._wrap_text(para.text, metrics, width) if para.text else [""]
            for line_text in wrapped:
                h = metrics.height()
                lines.append(_LaidLine(text=line_text, color=para.color, font=font, y=y, height=h))
                y += h
            y += PARAGRAPH_GAP
        self._lines = lines
        self._content_height = y

        vsb = self.verticalScrollBar()
        viewport_h = self.viewport().height()
        vsb.setRange(0, max(0, self._content_height - viewport_h))
        vsb.setPageStep(max(viewport_h, 1))
        self.viewport().update()

    @staticmethod
    def _wrap_text(text: str, metrics: QFontMetrics, max_width: int) -> list[str]:
        words = text.split(" ")
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if not current or metrics.horizontalAdvance(candidate) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current or not lines:
            lines.append(current)
        return lines

    # -- painting ---------------------------------------------------------
    def paintEvent(self, event) -> None:
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        offset = self.verticalScrollBar().value()
        viewport_h = self.viewport().height()

        for line in self._lines:
            top = line.y - offset
            if top + line.height < 0:
                continue
            if top > viewport_h:
                break
            self._paint_line(painter, line, top)
        painter.end()

    def _paint_line(self, painter: QPainter, line: _LaidLine, top: int) -> None:
        metrics = QFontMetrics(line.font)
        x = SIDE_MARGIN
        baseline_y = top + metrics.ascent()

        if line.text and self._font_background is not None and self._font_background.alpha() > 0:
            text_width = metrics.horizontalAdvance(line.text)
            rect = QRectF(x - 3, top, text_width + 6, line.height)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._font_background)
            painter.drawRoundedRect(rect, 3, 3)

        if not line.text:
            return

        path = QPainterPath()
        path.addText(QPointF(x, baseline_y), line.font, line.text)

        if self._outline_width > 0:
            pen = QPen(self._outline_color)
            pen.setWidthF(self._outline_width * 2)  # centered on the path -- half is under the fill
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(line.color)
        painter.drawPath(path)

    # -- Qt plumbing ---------------------------------------------------------
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()
