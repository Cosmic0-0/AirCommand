"""Shared fixed-width-column table helper. ThingsToChange item 1: Discovery's
(and, it turns out, Target Actions/Enumerate's) table columns drift out of
alignment between the header row and the data rows below it.

Root cause, confirmed empirically (not assumed from Tk docs) before writing
this fix: each affected view builds its header row and its body rows as two
SEPARATE .grid() layouts in two different container widgets (a plain header
CTkFrame, a scrollable body CTkScrollableFrame) — the body has to live in its
own scrollable container so the header doesn't scroll away with it, but that
means they're two independent Tk "grid masters", and Tk's grid geometry
manager sizes each grid's own columns to fit only ITS OWN widest cell. A
header cell ("SSID") and a body cell (an actual SSID value) are different
widths, so the columns drift. Tried first and rejected: grid's own `uniform`
column-group option, which sounds like it should solve exactly this — tested
directly against this app's own widgets, and it does NOT synchronize widths
across separate grid masters, only within one.

The fix that DOES work, also confirmed empirically: give every header cell
and every body cell in the same column the SAME fixed pixel width (CTkLabel's
own `width=`), which makes Tk give that column the same size in both grids —
*unless* a cell's actual text is wide enough to need more room than that at
the current font, in which case Tk still expands the column to fit it
anyway (width= is a minimum, not a clamp) and the drift comes right back.
_truncate() below closes that gap by shortening any value that would
overflow its column, with a trailing "…" — not pixel-perfect (this is a
plain character-count estimate against a variable-width font, not a real
text-measurement call), but enough margin that no realistic value in this
app's own data (SSIDs, labels, hostnames, open-port lists) blows out a
column's fixed width.

Only three of this app's five list/table-shaped views actually have this
header+body-as-two-grids shape at all: NetworksView and TargetPicker
(discovery_view.py), and EnumeratePanel (enumerate_view.py). CapturePanel's
handshake list, HandshakePicker (crack_view.py), and AuditLogView each render
one composed string per row via plain .pack() — no columns, no .grid() call
anywhere in those three files, so there's nothing for this bug to affect
there (confirmed by grep, not assumed from a visual skim).
"""

from __future__ import annotations

from dataclasses import dataclass

import customtkinter as ctk

# Rough estimate for this app's default CTkLabel font at its default size —
# not a real text-measurement (Tk can do that via font.measure(), but a
# per-character estimate is enough margin here and avoids a font-handle
# round-trip on every row update). See this module's own docstring for why
# some slack is fine: the goal is "never wider than the fixed column", not
# "truncate at the exact pixel".
_PX_PER_CHAR = 7


@dataclass(frozen=True)
class Column:
    title: str
    width: int  # pixels — shared minimum/clamp width for this column's header AND every body cell


def build_header(parent: ctk.CTkFrame, columns: tuple[Column, ...]) -> ctk.CTkFrame:
    """One bold label per column, each pinned to its Column.width. Pass the
    SAME `columns` tuple to add_row()/update_row() below for the body grid —
    that pairing is what keeps the two separate grid masters aligned."""
    header = ctk.CTkFrame(parent)
    for col_index, column in enumerate(columns):
        ctk.CTkLabel(
            header, text=column.title, font=ctk.CTkFont(weight="bold"),
            width=column.width, anchor="w",
        ).grid(row=0, column=col_index, padx=5, sticky="w")
    return header


def add_row(
    body: ctk.CTkBaseClass, row_index: int, columns: tuple[Column, ...], values: tuple[str, ...]
) -> list[ctk.CTkLabel]:
    """Adds one data row of plain labels to `body` (typically a
    CTkScrollableFrame) at row_index, one per column, width-matched (and
    truncated if needed) against `columns` — see build_header()."""
    labels = []
    for col_index, (column, value) in enumerate(zip(columns, values)):
        label = ctk.CTkLabel(body, text=_truncate(value, column.width), width=column.width, anchor="w")
        label.grid(row=row_index, column=col_index, padx=5, pady=2, sticky="w")
        labels.append(label)
    return labels


def update_row(labels: list[ctk.CTkLabel], columns: tuple[Column, ...], values: tuple[str, ...]) -> None:
    """Updates an existing row's text in place (a re-sighted Network, a
    relabelled Target, ...) — same truncation rule as add_row(), so an update
    can't reintroduce drift that creating the row avoided."""
    for label, column, value in zip(labels, columns, values):
        label.configure(text=_truncate(value, column.width))


def _truncate(text: str, width_px: int) -> str:
    max_chars = max(width_px // _PX_PER_CHAR, 3)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"
