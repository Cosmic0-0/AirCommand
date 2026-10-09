"""Grid Watch visual theme: color tokens, the shape rule, and font helpers for
the Gridwatch x Wireframe A redesign. See docs/design/gui-redesign-gridwatch.md
§3 for the approved spec this mirrors exactly (hex codes, the 2px-radius
"shape consistency lock", the two font roles).

CustomTkinter has no CSS -- this module is the single place every view reads
its colors/radius/fonts from, so the token set stays centralized rather than
hex codes scattered across every view file (the same reasoning table.py
already uses for column widths, applied one layer up).

Font fidelity is best-effort, not guaranteed, and that limitation is
deliberate rather than silently papered over: Hanken Grotesk and Fira Code
are Google Fonts, not bundled with this app or guaranteed present on the
machine Tk is running on -- unlike a browser, Tk can't fetch a webfont at
runtime. CustomTkinter itself ships its own Roboto .ttf and installs it by
copying into ~/.fonts/ at import time (see its own font_manager.py) -- the
same trick could bundle the real Grid Watch fonts too, but that means
shipping third-party font binaries in this repo, a repository-hygiene call
this redesign pass doesn't make unilaterally. Instead, ui_font()/mono_font()
check tkinter.font.families() once (lazily -- needs a live Tk root, so this
can't happen at import time) and fall back to CTk's own bundled default
(Roboto) for UI text, or a common system monospace for data/mono contexts,
if the real family isn't installed. Install Hanken Grotesk/Fira Code
yourself (e.g. into ~/.fonts/, from fonts.google.com) for a pixel-exact
match; everything still renders correctly without them.
"""

from __future__ import annotations

import tkinter.font as tkfont
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import customtkinter as ctk

PALETTE = {
    "bg": "#0a0b0d",       # page background
    "rail": "#0d0f12",     # sidebar + statusbar background
    "panel": "#121418",    # card/cell background
    "border": "#262b30",   # all hairline borders
    "text": "#dde2e6",     # primary text
    "muted": "#7d868f",    # secondary text, labels, inactive icons
    "accent": "#b4d94a",   # lime -- active-state indicator, primary buttons, links
    "danger": "#c1544a",   # deauth section, destructive actions
    "success": "#49ab7a",  # monitor-mode-on / live / active indicators
    "warn": "#d99a3f",     # flagged rows, e.g. "Interrupted by crash"
}

CORNER_RADIUS = 2   # the one border-radius value used everywhere -- cells,
# buttons, inputs, pills, chips. No exceptions; this is a deliberate "shape
# consistency lock" per the design spec, not a default to vary per widget.

_UI_FAMILY = "Hanken Grotesk"
_MONO_FAMILY = "Fira Code"
_MONO_FALLBACK = "Courier New"   # a near-universal Tk monospace fallback;
# CTk has no bundled mono equivalent to its Roboto UI fallback, so this one
# needs an explicit name rather than None.


@lru_cache(maxsize=1)
def _available_families() -> frozenset[str]:
    # Needs a live Tk interpreter -- only safe to call once a root window
    # exists, i.e. from inside widget construction, never at import time.
    # Cached because tkfont.families() is a real Tcl round-trip and this gets
    # called from every single themed widget's constructor.
    return frozenset(tkfont.families())


def ui_font(size: int = 12, weight: str = "normal") -> "ctk.CTkFont":
    """Hanken Grotesk if installed, else CTk's own bundled default (Roboto) --
    family=None lets CTkFont pick that itself rather than this module naming
    "Roboto" explicitly, since that's CTk's own fallback choice to own, not
    this theme's."""
    import customtkinter as ctk

    family = _UI_FAMILY if _UI_FAMILY in _available_families() else None
    return ctk.CTkFont(family=family, size=size, weight=weight)


def mono_font(size: int = 12, weight: str = "normal") -> "ctk.CTkFont":
    """Fira Code if installed, else a common system monospace -- used for all
    data/mono contexts: adapter names, MAC addresses, table cell data, status
    pills, the statusbar, stat values (per the design spec's typography
    rule)."""
    import customtkinter as ctk

    family = _MONO_FAMILY if _MONO_FAMILY in _available_families() else _MONO_FALLBACK
    return ctk.CTkFont(family=family, size=size, weight=weight)
