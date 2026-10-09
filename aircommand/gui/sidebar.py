"""Sidebar — collapsible icon+label nav shell for the Gridwatch x Wireframe A
redesign. See docs/design/gui-redesign-gridwatch.md §3 ("Sidebar specifics")
and §4. Five destinations: four main items (Management, Discovery & Targets,
Capture & Attack, Cracking) in the main list, Logs pinned separately in a
footer slot -- visually set apart but functionally identical (same click
behavior, same nav API, same row widget shape).

Collapse is an instant width toggle, not the design spec's .14s eased
transition -- CustomTkinter has no built-in animated resize, and hand-rolling
one via repeated .after() calls was judged not worth the complexity for a
sidebar width change; flagged here as a known, deliberate simplification
rather than a silently dropped requirement.

Icons are drawn with a plain tkinter.Canvas (a handful of lines/ovals per
icon) rather than true SVG -- CustomTkinter has no SVG renderer, and adding
one (cairosvg/Pillow) would be a new dependency for a cosmetic detail. These
are deliberately close approximations of the mockup's icon shapes, not a
pixel-identical reproduction -- see theme.py's own docstring for the same
"best-effort, not guaranteed" framing applied to fonts.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from typing import Callable

import customtkinter as ctk

from aircommand.gui.theme import PALETTE, ui_font

_WIDTH_EXPANDED = 196
_WIDTH_COLLAPSED = 54


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


def _draw_icon(canvas: tk.Canvas, kind: str, color: str) -> None:
    """A 18x18 canvas, approximating the mockup's 14x14 stroke SVGs with a
    couple of lines/ovals -- see module docstring."""
    line = {"fill": color, "width": 1.4}
    stroke = {"fill": "", "outline": color, "width": 1.4}
    if kind == "management":   # three vertical sliders with a dot each
        for x in (5, 9, 13):
            canvas.create_line(x, 2, x, 16, **line)
        canvas.create_oval(3, 6, 7, 10, **stroke)
        canvas.create_oval(7, 10, 11, 14, **stroke)
        canvas.create_oval(11, 4, 15, 8, **stroke)
    elif kind == "discovery":   # concentric circles -- a beacon/target glyph
        canvas.create_oval(2, 2, 16, 16, **stroke)
        canvas.create_oval(6, 6, 12, 12, **stroke)
        canvas.create_oval(8, 8, 10, 10, fill=color, outline="")
    elif kind == "capture":   # radiating waves around a center dot
        canvas.create_oval(6, 6, 12, 12, **stroke)
        canvas.create_line(9, 1, 9, 5, **line)
        canvas.create_line(9, 13, 9, 17, **line)
        canvas.create_line(1, 9, 5, 9, **line)
        canvas.create_line(13, 9, 17, 9, **line)
    elif kind == "cracking":   # a key
        canvas.create_oval(1, 7, 9, 15, **stroke)
        canvas.create_line(8, 11, 17, 11, **line)
        canvas.create_line(13, 11, 13, 15, **line)
        canvas.create_line(16, 11, 16, 15, **line)
    elif kind == "logs":   # a document with three lines of text
        canvas.create_rectangle(3, 1, 15, 17, **stroke)
        for y in (6, 9, 12):
            canvas.create_line(5.5, y, 12.5, y, fill=color, width=1.2)
    else:
        raise ValueError(f"unknown icon kind: {kind!r}")


class Sidebar(ctk.CTkFrame):
    """The design spec's left nav rail. `on_navigate(key)` is called with the
    clicked item's key whenever the operator picks a destination (including
    re-clicking the already-active one) -- the caller (App) owns actually
    switching page content; this class only renders the rail and reports
    clicks. `set_active(key)` updates which row shows the accent dot without
    triggering a further on_navigate call, so App can drive the rail's
    display from its own page-switch logic without an echo."""

    def __init__(self, master, on_navigate: Callable[[str], None], **kwargs) -> None:
        kwargs.setdefault("fg_color", PALETTE["rail"])
        kwargs.setdefault("corner_radius", 0)
        kwargs.setdefault("width", _WIDTH_EXPANDED)
        super().__init__(master, **kwargs)
        self.pack_propagate(False)   # hold an explicit width regardless of content
        self._on_navigate = on_navigate
        self._collapsed = False
        self._active_key = _MAIN_ITEMS[0].key
        self._rows: dict[str, dict] = {}

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(side="top", fill="x", padx=10, pady=10)
        self._header = header

        self._logo_row = ctk.CTkFrame(header, fg_color="transparent")
        self._logo_row.pack(side="left")
        logo_canvas = tk.Canvas(self._logo_row, width=18, height=18, bg=PALETTE["rail"], highlightthickness=0)
        logo_canvas.pack(side="left")
        _draw_icon(logo_canvas, "discovery", PALETTE["accent"])   # a stand-in wifi-ish mark
        self._wordmark = ctk.CTkLabel(
            self._logo_row, text="AIRCOMMAND", font=ui_font(12, "bold"), text_color=PALETTE["text"],
        )
        self._wordmark.pack(side="left", padx=(8, 0))

        self._collapse_canvas = tk.Canvas(header, width=16, height=16, bg=PALETTE["rail"], highlightthickness=0, cursor="hand2")
        self._collapse_canvas.pack(side="right")
        self._collapse_canvas.bind("<Button-1>", lambda _event: self.toggle_collapse())
        self._draw_chevron()

        nav_list = ctk.CTkFrame(self, fg_color="transparent")
        nav_list.pack(side="top", fill="x", padx=6)
        for item in _MAIN_ITEMS:
            self._build_row(nav_list, item)

        footer = ctk.CTkFrame(self, fg_color="transparent", border_width=1, border_color=PALETTE["border"])
        footer.pack(side="bottom", fill="x", padx=6, pady=6)
        self._build_row(footer, _FOOTER_ITEM)

        self.set_active(self._active_key)

    def _build_row(self, parent, item: NavItem) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=2, cursor="hand2")
        row.pack(side="top", fill="x", pady=1)

        dot_canvas = tk.Canvas(row, width=6, height=6, bg=PALETTE["rail"], highlightthickness=0)
        dot_canvas.pack(side="left", padx=(9, 6), pady=8)
        dot = dot_canvas.create_oval(0, 0, 6, 6, fill=PALETTE["muted"], outline="")

        icon_canvas = tk.Canvas(row, width=18, height=18, bg=PALETTE["rail"], highlightthickness=0)
        icon_canvas.pack(side="left", pady=5)
        _draw_icon(icon_canvas, item.icon, PALETTE["muted"])

        label = ctk.CTkLabel(row, text=item.label, font=ui_font(12), text_color=PALETTE["muted"], anchor="w")
        label.pack(side="left", padx=(6, 0), pady=8, fill="x", expand=True)

        for widget in (row, dot_canvas, icon_canvas, label):
            widget.bind("<Button-1>", lambda _event, key=item.key: self._on_navigate(key))
            widget.bind("<Enter>", lambda _event, r=row, k=item.key: self._on_hover(r, k, True))
            widget.bind("<Leave>", lambda _event, r=row, k=item.key: self._on_hover(r, k, False))

        self._rows[item.key] = {
            "row": row, "dot_canvas": dot_canvas, "dot": dot,
            "icon_canvas": icon_canvas, "icon_kind": item.icon, "label": label,
        }

    def _on_hover(self, row, key: str, entering: bool) -> None:
        if key == self._active_key:
            return   # active row keeps its own background regardless of hover
        row.configure(fg_color=PALETTE["panel"] if entering else "transparent")

    def set_active(self, key: str) -> None:
        """Updates which row shows the accent dot/background -- does NOT call
        on_navigate (App drives this from its own already-decided page
        switch, not the other way around)."""
        if key not in self._rows:
            raise ValueError(f"unknown nav key: {key!r}")
        self._active_key = key
        for row_key, parts in self._rows.items():
            active = row_key == key
            color = PALETTE["accent"] if active else PALETTE["muted"]
            parts["row"].configure(fg_color=PALETTE["panel"] if active else "transparent")
            parts["dot_canvas"].itemconfigure(parts["dot"], fill=color)
            parts["label"].configure(text_color=PALETTE["text"] if active else PALETTE["muted"])
            parts["icon_canvas"].delete("all")
            _draw_icon(parts["icon_canvas"], parts["icon_kind"], color if active else PALETTE["muted"])

    def toggle_collapse(self) -> None:
        self._collapsed = not self._collapsed
        self.configure(width=_WIDTH_COLLAPSED if self._collapsed else _WIDTH_EXPANDED)
        self._wordmark.pack_forget() if self._collapsed else self._wordmark.pack(side="left", padx=(8, 0))
        self._draw_chevron()
        for parts in self._rows.values():
            if self._collapsed:
                parts["dot_canvas"].pack_forget()
                parts["label"].pack_forget()
            else:
                # Re-insert dot/label in their original left-to-right order --
                # icon_canvas is already packed, so these go back after it.
                parts["dot_canvas"].pack(side="left", padx=(9, 6), pady=8, before=parts["icon_canvas"])
                parts["label"].pack(side="left", padx=(6, 0), pady=8, fill="x", expand=True)

    def _draw_chevron(self) -> None:
        self._collapse_canvas.delete("all")
        # Points left when expanded (click to collapse), right when collapsed
        # (click to expand) -- stands in for the mockup's 180°-rotating chevron.
        if self._collapsed:
            self._collapse_canvas.create_line(6, 3, 11, 8, 6, 13, fill=PALETTE["muted"], width=1.4)
        else:
            self._collapse_canvas.create_line(11, 3, 6, 8, 11, 13, fill=PALETTE["muted"], width=1.4)
