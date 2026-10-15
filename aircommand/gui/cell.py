"""Shared Grid Watch components for the PySide6 port: Cell, DashGrid,
StatusPill, Chip, Tag. These are the Qt equivalents of the mockup's
.cell / .dash-grid / .status-pill / .chip / .tag (docs/design/
gui-redesign-gridwatch.md section 3), reused across every page.

The visual tokens live in theme.py's one QSS string; each widget here sets a
`class` (and sometimes `danger`, `state`, `warn`) property the QSS selects on.
A state change flips a property and calls theme.repolish so Qt restyles.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from aircommand.gui.theme import PALETTE, repolish

# The hex-to-token reverse map lets StatusPill.set_state keep its old signature
# (a caller passes PALETTE["danger"]) while the QSS selects on a named state.
_STATE_BY_COLOR = {
    PALETTE["success"]: "success",
    PALETTE["danger"]: "danger",
    PALETTE["warn"]: "warn",
    PALETTE["muted"]: "muted",
}


class DashGrid(QWidget):
    """A 12-column grid (design spec: .dash-grid, gap 8px, span-4/6/8/12).
    place_cell(widget, span) drops `widget` into the next free slot and wraps to
    a new row once the current row's spans would exceed 12, so a page is laid out
    as a flat sequence of (widget, span) pairs without row/column bookkeeping."""

    _COLUMNS = 12

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(8)   # the design's 8px gap
        for column in range(self._COLUMNS):
            self._grid.setColumnStretch(column, 1)
        self._row = 0
        self._col = 0

    def place_cell(self, widget: QWidget, span: int) -> None:
        if span < 1 or span > self._COLUMNS:
            raise ValueError(f"span must be between 1 and {self._COLUMNS}, got {span}")
        if self._col + span > self._COLUMNS:
            self._row += 1
            self._col = 0
        self._grid.addWidget(widget, self._row, self._col, 1, span)
        self._col += span
        if self._col >= self._COLUMNS:
            self._row += 1
            self._col = 0

    def new_row(self) -> None:
        """Force the next place_cell onto a fresh row even if the current one is
        not full."""
        if self._col != 0:
            self._row += 1
            self._col = 0


class Cell(QFrame):
    """The atomic content container (design spec: .cell / .cell-head /
    .cell-body): panel background, 1px border, a title bar over a padded body.
    Callers add their content to `self.body_layout`. `danger=True` is the
    red-bordered, red-title Deauth cell."""

    def __init__(self, title: str, *, danger: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setProperty("class", "cell")
        flag = "true" if danger else "false"
        self.setProperty("danger", flag)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        head = QFrame()
        head.setProperty("class", "cellHead")
        head.setProperty("danger", flag)
        head_layout = QHBoxLayout(head)
        head_layout.setContentsMargins(10, 7, 10, 7)
        self._title_label = QLabel(title)
        self._title_label.setProperty("class", "cellTitle")
        self._title_label.setProperty("danger", flag)
        menu = QLabel("⋮")   # the mockup's vertical-ellipsis menu glyph
        menu.setProperty("class", "cellMenu")
        head_layout.addWidget(self._title_label)
        head_layout.addStretch(1)
        head_layout.addWidget(menu)
        outer.addWidget(head)

        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(12, 12, 12, 12)
        outer.addWidget(self.body, 1)

    def set_title(self, title: str) -> None:
        self._title_label.setText(title)


class StatusPill(QLabel):
    """A small bordered Fira Code label (design spec: .status-pill): muted by
    default, success-green when active. set_state takes an exact PALETTE color
    for a caller with more than two states (the status bar's ACTIVE/LOST)."""

    def __init__(self, text: str, active: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setProperty("class", "pill")
        self.setProperty("state", "success" if active else "muted")

    def set_active(self, active: bool, text: Optional[str] = None) -> None:
        self._set("success" if active else "muted", text)

    def set_state(self, color: str, text: Optional[str] = None) -> None:
        self._set(_STATE_BY_COLOR.get(color, "muted"), text)

    def _set(self, state: str, text: Optional[str]) -> None:
        if text is not None:
            self.setText(text)
        self.setProperty("state", state)
        repolish(self)


class Chip(QFrame):
    """A Target-allowlist entry (design spec: .chip): a bordered Fira Code label
    with an inline remove button that turns danger-red on hover. `on_remove` is
    called with no arguments when it is clicked."""

    def __init__(self, text: str, on_remove: Callable[[], None], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setProperty("class", "chip")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(6)
        label = QLabel(text)
        label.setProperty("class", "chipText")
        remove = QPushButton("×")   # a multiplication sign, the mockup's remove glyph
        remove.setProperty("class", "chipRemove")
        remove.clicked.connect(lambda: on_remove())
        layout.addWidget(label)
        layout.addWidget(remove)


class Tag(QLabel):
    """A small bordered inline label (design spec: .tag / .tag-warn). `warn`
    flags rows like "Interrupted by crash" in amber."""

    def __init__(self, text: str, warn: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setProperty("class", "tag")
        self.setProperty("warn", "true" if warn else "false")
