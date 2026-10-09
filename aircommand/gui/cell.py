"""Shared Grid Watch component patterns: `Cell`, `DashGrid`, `StatusPill`,
`Chip`, `Tag`. See docs/design/gui-redesign-gridwatch.md §3's "Core component
patterns" table -- these are the CustomTkinter equivalents of `.cell`/
`.dash-grid`/`.status-pill`/`.chip`/`.tag`, reused across every redesigned
page rather than rebuilt per view, the same role table.py's Column/
build_header/add_row already play for plain tables.

CustomTkinter has no CSS grid -- DashGrid approximates the design's 12-column,
8px-gap, span-4/6/8/12 layout with Tk's own grid() geometry manager: 12 equal-
weight columns, and place_cell() auto-wraps to the next row once a row's spans
would exceed 12, so callers describe a layout as a flat sequence of
(cell, span) pairs instead of tracking row/column indices themselves.
"""

from __future__ import annotations

import customtkinter as ctk

from aircommand.gui.theme import CORNER_RADIUS, PALETTE, mono_font, ui_font


class DashGrid(ctk.CTkFrame):
    """A 12-column grid container (design spec: .dash-grid, gap 8px, span-4/
    6/8/12 utility widths). place_cell(cell, span) grids `cell` into the next
    available column slot, auto-wrapping to a new row once the current row's
    spans would exceed 12 -- callers lay out a page top-to-bottom as a flat
    sequence of cells without computing row/column indices by hand."""

    _COLUMNS = 12
    _GAP = 4   # half of the design's 8px gap on each side (padx/pady split
    # between adjacent cells, same convention table.py uses for its own padding)

    def __init__(self, master, **kwargs) -> None:
        kwargs.setdefault("fg_color", "transparent")
        super().__init__(master, **kwargs)
        for i in range(self._COLUMNS):
            self.grid_columnconfigure(i, weight=1, uniform="dashcol")
        self._row = 0
        self._col = 0

    def place_cell(self, cell: ctk.CTkBaseClass, span: int) -> None:
        if span < 1 or span > self._COLUMNS:
            raise ValueError(f"span must be between 1 and {self._COLUMNS}, got {span}")
        if self._col + span > self._COLUMNS:
            self._row += 1
            self._col = 0
        cell.grid(
            row=self._row, column=self._col, columnspan=span,
            sticky="nsew", padx=self._GAP, pady=self._GAP,
        )
        self._col += span
        if self._col >= self._COLUMNS:
            self._row += 1
            self._col = 0

    def new_row(self) -> None:
        """Forces the next place_cell() onto a fresh row even if the current
        one isn't full -- for a page that wants an explicit row break rather
        than relying on span arithmetic to land exactly on 12."""
        if self._col != 0:
            self._row += 1
            self._col = 0


class Cell(ctk.CTkFrame):
    """The atomic content container (design spec: .cell / .cell-head /
    .cell-body). Panel background, 1px border, 2px radius; a title bar
    (title left, a literal "⋮" menu glyph right, bottom border) over a
    padded, flex-grow body. `self.body` is where callers pack/grid their
    own content -- this class only builds the chrome around it."""

    def __init__(self, master, title: str, title_color: str | None = None, **kwargs) -> None:
        kwargs.setdefault("fg_color", PALETTE["panel"])
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("border_color", PALETTE["border"])   # override directly,
        # e.g. border_color=PALETTE["danger"] for a danger-bordered cell (design
        # spec: the Capture & Attack page's Deauth section) -- already a plain
        # kwarg, no dedicated parameter needed for this one.
        kwargs.setdefault("corner_radius", CORNER_RADIUS)
        super().__init__(master, **kwargs)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(side="top", fill="x")
        self._title_label = ctk.CTkLabel(
            head, text=title, font=ui_font(11, "bold"),
            text_color=title_color if title_color is not None else PALETTE["text"], anchor="w",
        )
        self._title_label.pack(side="left", padx=10, pady=6)
        ctk.CTkLabel(
            head, text="⋮", font=ui_font(13), text_color=PALETTE["muted"], width=16,
        ).pack(side="right", padx=8)

        ctk.CTkFrame(self, fg_color=PALETTE["border"], height=1, corner_radius=0).pack(
            side="top", fill="x"
        )

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(side="top", fill="both", expand=True, padx=12, pady=10)

    def set_title(self, title: str) -> None:
        self._title_label.configure(text=title)


class StatusPill(ctk.CTkFrame):
    """A small bordered Fira Code label (design spec: .status-pill), muted by
    default, success-green when active -- used both for "Sudo session
    active"/adapter LIVE-OFF indicators and the Management page's Monitor
    mode stat. A Frame wrapping a Label (not a bare Label) so it can carry
    its own border/background, matching the mockup's bordered-pill look
    rather than plain colored text."""

    def __init__(self, master, text: str, active: bool = False, **kwargs) -> None:
        kwargs.setdefault("fg_color", "transparent")
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("corner_radius", CORNER_RADIUS)
        super().__init__(master, **kwargs)
        self.configure(border_color=PALETTE["success"] if active else PALETTE["border"])
        self._label = ctk.CTkLabel(
            self, text=text, font=mono_font(11),
            text_color=PALETTE["success"] if active else PALETTE["muted"],
        )
        self._label.pack(padx=8, pady=2)

    def set_active(self, active: bool, text: str | None = None) -> None:
        color = PALETTE["success"] if active else PALETTE["muted"]
        self.configure(border_color=color if active else PALETTE["border"])
        self._label.configure(text_color=color, **({"text": text} if text is not None else {}))

    def set_state(self, color: str, text: str | None = None) -> None:
        """Like set_active(), but for a caller with more than two real
        states to represent (e.g. StatusBar's privilege indicator -- ACTIVE/
        LOST, both real, not a boolean) -- takes the exact PALETTE color to
        show rather than deriving it from True/False."""
        self.configure(border_color=color)
        self._label.configure(text_color=color, **({"text": text} if text is not None else {}))


class Chip(ctk.CTkFrame):
    """A Target-allowlist-style entry (design spec: .chip) -- a bordered Fira
    Code pill with an inline "×" remove button that turns danger-red on
    hover. `on_remove` is called with no arguments when the × is clicked;
    callers decide what that means (e.g. engine.targets.remove(bssid))."""

    def __init__(self, master, text: str, on_remove, **kwargs) -> None:
        kwargs.setdefault("fg_color", "transparent")
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("border_color", PALETTE["border"])
        kwargs.setdefault("corner_radius", CORNER_RADIUS)
        super().__init__(master, **kwargs)
        ctk.CTkLabel(self, text=text, font=mono_font(11), text_color=PALETTE["text"]).pack(
            side="left", padx=(8, 4), pady=2
        )
        remove_button = ctk.CTkLabel(
            self, text="×", font=ui_font(12), text_color=PALETTE["muted"], cursor="hand2",
        )
        remove_button.pack(side="left", padx=(0, 8), pady=2)
        remove_button.bind("<Button-1>", lambda _event: on_remove())
        remove_button.bind("<Enter>", lambda _event: remove_button.configure(text_color=PALETTE["danger"]))
        remove_button.bind("<Leave>", lambda _event: remove_button.configure(text_color=PALETTE["muted"]))


class Tag(ctk.CTkLabel):
    """A small bordered inline label (design spec: .tag / .tag-warn). `warn`
    flags rows like "Interrupted by crash" in amber; otherwise a plain muted
    outline."""

    def __init__(self, master, text: str, warn: bool = False, **kwargs) -> None:
        color = PALETTE["warn"] if warn else PALETTE["muted"]
        kwargs.setdefault("fg_color", "transparent")
        kwargs.setdefault("corner_radius", CORNER_RADIUS)
        super().__init__(
            master, text=text, font=mono_font(10), text_color=color,
            **kwargs,
        )
