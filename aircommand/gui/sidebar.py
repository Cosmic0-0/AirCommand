"""Sidebar: the collapsible left nav rail for the PySide6 port.

See docs/design/gui-redesign-gridwatch.md section 3 ("Sidebar specifics") and
docs/design/gui-port/05-boundary-design.md "View mapping" and "Motion": a left
QFrame of nav buttons, 196px expanded and 54px collapsed, the collapse animated
with one QPropertyAnimation over a width property (140ms, OutCubic,
interruptible), exactly as the spike did (spikes/gui-port/spike.py). The
instant toggle the CustomTkinter sidebar documented as a deliberate
simplification is now a real animation, which restores the Gridwatch spec.

on_navigate(key) reports a click; set_active(key) paints the active row without
echoing a click back, so App drives the rail from its own page switch.

Each nav row is a QPushButton carrying the "nav" class the theme styles, with an
"active" property the QSS selects on. Its icon reproduces the Tk rail's row: a
6px dot then an 18px stroke icon, both accent on the active row and muted
otherwise. The icon shapes are the Tk canvas approximations of the mockup's
SVGs, redrawn with QPainter into a pixmap. Below the halfway width the label,
the dot, and the wordmark hide and the icon stays, so the collapsed rail still
tells the pages apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import Property, QEasingCurve, QPointF, QPropertyAnimation, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from aircommand.gui import theme
from aircommand.gui.theme import PALETTE, wordmark_font

_WIDTH_EXPANDED = 196
_WIDTH_COLLAPSED = 54
# One module constant for the collapse duration, next to the width constants.
# Set it to 0 to turn the motion off (01-criteria "Motion fidelity bar"): the
# animation then jumps straight to the end width with no eased frames.
COLLAPSE_MS = 140
_HALFWAY = (_WIDTH_EXPANDED + _WIDTH_COLLAPSED) / 2

_ICON = 18      # the icon square, as the Tk icon canvas
_DOT = 6        # the active-indicator dot, as the Tk dot canvas
_DOT_GAP = 6    # space between dot and icon, the Tk dot's right padding


def _button_text(label: str) -> str:
    """A QPushButton reads "&" as a mnemonic marker: it hides the ampersand,
    underlines the next character, and adds an Alt shortcut. Doubling it shows
    a literal "&", as the Tk rail did."""
    return label.replace("&", "&&")


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    icon: str   # one of _draw_icon's recognized kinds


_MAIN_ITEMS = (
    NavItem("management", "Management", "management"),
    NavItem("discovery", "Discovery & Targets", "discovery"),
    NavItem("capture", "Capture & Attack", "capture"),
    NavItem("cracking", "Cracking", "cracking"),
)
_FOOTER_ITEM = NavItem("logs", "Logs", "logs")
ALL_ITEMS = _MAIN_ITEMS + (_FOOTER_ITEM,)   # the full key set App's page map must cover


def _draw_icon(painter: QPainter, kind: str, color: QColor) -> None:
    """The Tk rail's 18x18 icon shapes, in the same canvas coordinates. Oval
    and rectangle arguments are (x1, y1, x2, y2) bounding boxes, as Tk takes them."""
    pen = QPen(color)
    pen.setWidthF(1.4)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    def line(x1: float, y1: float, x2: float, y2: float) -> None:
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    def oval(x1: float, y1: float, x2: float, y2: float) -> None:
        painter.drawEllipse(QRectF(x1, y1, x2 - x1, y2 - y1))

    if kind == "management":   # three vertical sliders with a knob each
        for x in (5, 9, 13):
            line(x, 2, x, 16)
        oval(3, 6, 7, 10)
        oval(7, 10, 11, 14)
        oval(11, 4, 15, 8)
    elif kind == "discovery":   # concentric circles, a beacon/target glyph
        oval(2, 2, 16, 16)
        oval(6, 6, 12, 12)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        oval(8, 8, 10, 10)
    elif kind == "capture":   # radiating marks around a center ring
        oval(6, 6, 12, 12)
        line(9, 1, 9, 5)
        line(9, 13, 9, 17)
        line(1, 9, 5, 9)
        line(13, 9, 17, 9)
    elif kind == "cracking":   # a key
        oval(1, 7, 9, 15)
        line(8, 11, 17, 11)
        line(13, 11, 13, 15)
        line(16, 11, 16, 15)
    elif kind == "logs":   # a document with three lines of text
        painter.drawRect(QRectF(3, 1, 12, 16))
        pen.setWidthF(1.2)
        painter.setPen(pen)
        for y in (6, 9, 12):
            line(5.5, y, 12.5, y)
    else:
        raise ValueError(f"unknown icon kind: {kind!r}")


def _render_nav_icon(kind: str, color: str, with_dot: bool) -> QPixmap:
    """One nav row's icon: the dot then the icon when expanded, the icon alone
    when collapsed."""
    offset = _DOT + _DOT_GAP if with_dot else 0
    pixmap = QPixmap(offset + _ICON, _ICON)
    pixmap.fill(Qt.GlobalColor.transparent)
    qcolor = QColor(color)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if with_dot:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(qcolor)
        painter.drawEllipse(QRectF(0, (_ICON - _DOT) / 2, _DOT, _DOT))
    painter.translate(offset, 0)
    _draw_icon(painter, kind, qcolor)
    painter.end()
    return pixmap


class Sidebar(QFrame):
    """The left nav rail. on_navigate(key) fires on every click, including a
    re-click of the active row; App owns switching the page content, this
    class only renders the rail and reports clicks."""

    def __init__(self, on_navigate: Callable[[str], None], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setProperty("class", "sidebar")
        self._on_navigate = on_navigate
        self._collapsed = False
        self._width = _WIDTH_EXPANDED
        self._labels_shown = True
        self._active_key = _MAIN_ITEMS[0].key
        self._items: dict[str, NavItem] = {}
        self._buttons: dict[str, QPushButton] = {}
        self.setFixedWidth(_WIDTH_EXPANDED)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(1)

        header = QHBoxLayout()
        self._wordmark = QLabel("AIRCOMMAND")
        self._wordmark.setProperty("class", "logo")
        self._wordmark.setFont(wordmark_font())
        self._collapse_button = QPushButton("‹")   # a left chevron; swaps glyph on collapse
        self._collapse_button.setProperty("class", "collapse")
        self._collapse_button.setFixedSize(21, 21)
        self._collapse_button.clicked.connect(self.toggle_collapse)
        header.addWidget(self._wordmark)
        header.addStretch(1)
        header.addWidget(self._collapse_button)
        layout.addLayout(header)

        for item in _MAIN_ITEMS:
            layout.addWidget(self._make_button(item))
        layout.addStretch(1)
        layout.addWidget(self._make_button(_FOOTER_ITEM))

        # One width property animated per collapse, as the spike measured.
        self._anim = QPropertyAnimation(self, b"barWidth")
        self._anim.setDuration(COLLAPSE_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self.set_active(self._active_key)

    def _make_button(self, item: NavItem) -> QPushButton:
        button = QPushButton(_button_text(item.label))
        button.setProperty("class", "nav")
        button.setProperty("active", "false")
        button.clicked.connect(lambda _checked=False, key=item.key: self._on_navigate(key))
        self._items[item.key] = item
        self._buttons[item.key] = button
        return button

    def set_active(self, key: str) -> None:
        if key not in self._buttons:
            raise ValueError(f"unknown nav key: {key!r}")
        self._active_key = key
        for row_key, button in self._buttons.items():
            button.setProperty("active", "true" if row_key == key else "false")
            theme.repolish(button)   # Qt does not restyle on a property change on its own
        self._apply_icons()

    def _apply_icons(self) -> None:
        """Redraw every row's icon for the current active key and width: accent
        on the active row, muted elsewhere, with the dot only while labels show."""
        for key, button in self._buttons.items():
            color = PALETTE["accent"] if key == self._active_key else PALETTE["muted"]
            pixmap = _render_nav_icon(self._items[key].icon, color, with_dot=self._labels_shown)
            button.setIcon(QIcon(pixmap))
            button.setIconSize(pixmap.size())

    def toggle_collapse(self) -> None:
        self._collapsed = not self._collapsed
        self._collapse_button.setText("›" if self._collapsed else "‹")
        # Retarget from the current width, so a second click mid-flight reverses
        # smoothly instead of restarting (interruptible, as the spike did).
        self._anim.stop()
        self._anim.setStartValue(self._width)
        self._anim.setEndValue(_WIDTH_COLLAPSED if self._collapsed else _WIDTH_EXPANDED)
        self._anim.start()

    def _get_bar_width(self) -> int:
        return self._width

    def _set_bar_width(self, width: int) -> None:
        self._width = width
        self.setFixedWidth(width)
        # Labels, dots, and the wordmark switch at the halfway width and do not
        # animate (spike.py). Icons stay at every width.
        show = width > _HALFWAY
        if show == self._labels_shown:
            return
        self._labels_shown = show
        self._wordmark.setVisible(show)
        for key, button in self._buttons.items():
            button.setText(_button_text(self._items[key].label) if show else "")
        self._apply_icons()

    barWidth = Property(int, _get_bar_width, _set_bar_width)
