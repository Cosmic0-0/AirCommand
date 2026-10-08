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

Body cells are CTkEntry widgets in "readonly" state, not CTkLabel — this is
why, see add_row()'s docstring. That choice turns out to also fix the drift
bug outright: confirmed empirically (a real on-screen widget, a body value
much longer than its column's pixel width, winfo_width() read back after
update_idletasks()) that CTkEntry's own fixed pixel width does NOT
geometry-propagate past a long value the way CTkLabel's does — the column
stays exactly at Column.width and the overflow text just scrolls inside the
entry's own xview instead of forcing the grid column wider. Header cells
don't have this problem either way (header text is always a short fixed
title like "BSSID"), so build_header() below still uses plain CTkLabel.

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


@dataclass(frozen=True)
class Column:
    title: str
    width: int  # pixels — shared minimum/clamp width for this column's header AND every body cell


def build_header(parent: ctk.CTkFrame, columns: tuple[Column, ...]) -> ctk.CTkFrame:
    """One bold label per column, each pinned to its Column.width. Pass the
    SAME `columns` tuple to add_row()/update_row() below for the body grid --
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
) -> list[ctk.CTkEntry]:
    """Adds one data row of read-only entry cells to `body` (typically a
    CTkScrollableFrame) at row_index, one per column, width-matched against
    `columns` -- see build_header().

    Read-only CTkEntry instead of CTkLabel so a cell's value (a BSSID, an
    SSID, ...) can be mouse-drag-selected and copied (Ctrl+C) like any other
    text field -- a plain CTkLabel can't be selected at all. border_width=0,
    corner_radius=0 and fg_color="transparent" make it render flush with the
    row, same as the CTkLabel it replaces, instead of looking like an input
    box. state="readonly" blocks typing/paste/cut while leaving selection and
    copy untouched -- standard Tk Entry behaviour, confirmed empirically
    below alongside the recipe for changing a readonly entry's text (see
    _set_cell_text())."""
    entries = []
    for col_index, (column, value) in enumerate(zip(columns, values)):
        entry = ctk.CTkEntry(
            body, width=column.width, fg_color="transparent",
            border_width=0, corner_radius=0, justify="left",
        )
        _set_cell_text(entry, value)
        entry.grid(row=row_index, column=col_index, padx=5, pady=2, sticky="w")
        entries.append(entry)
    return entries


def update_row(entries: list[ctk.CTkEntry], columns: tuple[Column, ...], values: tuple[str, ...]) -> None:
    """Updates an existing row's text in place (a re-sighted Network, a
    relabelled Target, ...) -- same cells as add_row()."""
    for entry, value in zip(entries, values):
        _set_cell_text(entry, value)


def _set_cell_text(entry: ctk.CTkEntry, value: str) -> None:
    """Replaces a readonly entry's content. Confirmed empirically: a Tk Entry
    in readonly state silently no-ops .insert()/.delete() instead of raising
    -- state has to go back to "normal" around the edit, then back to
    "readonly", or the new value never actually lands."""
    entry.configure(state="normal")
    entry.delete(0, "end")
    entry.insert(0, value)
    entry.configure(state="readonly")
