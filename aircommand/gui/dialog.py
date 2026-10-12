"""Shared Grid Watch modal-dialog helpers: the themed CTkToplevel chrome, the
themed entry, the primary/outline buttons, and centering-over-parent that all
of this app's dialogs (sudo prompt, Add-as-Target, Add-manually, deauth
confirm) need. Same role cell.py/table.py already play for the pages -- one
place the recipe lives, so a dialog is styled by *calling* it rather than
re-pasting the hex-and-font incantation a fourth time.

Why this is needed at all: CustomTkinter runs in its default "Light"
appearance mode here (nothing calls set_appearance_mode), so any widget
constructed without an explicit color falls back to CTk's own light theme
default -- a near-white CTkToplevel background (gray92) and the stock blue
CTkButton (#3B8ED0). That is exactly the "doesn't match the rest of the
program" mismatch the dialogs showed: a page sets every color from PALETTE and
reads dark, a dialog left on the defaults read light. These helpers set the
PALETTE tokens explicitly so dialogs match the pages.

The button/entry recipes mirror the pages' own (management_view.py's
_start_button/_make_outline_button, discovery_view.py's _themed_entry) and the
mockup CSS (.btn: 12px/600, 2px radius, 1px --border, transparent;
accent/danger primaries fill with color and use #0a0b0d text; .field input:
--bg fill, --border hairline).
"""

from __future__ import annotations

from typing import Optional

import customtkinter as ctk

from aircommand.gui.theme import CORNER_RADIUS, PALETTE, ui_font


def apply_chrome(
    dialog: ctk.CTkToplevel, title: str, *, min_width: int = 380, min_height: int = 160
) -> None:
    """Grid Watch modal chrome on a freshly-constructed CTkToplevel: the dark
    PALETTE['bg'] background (CTk's own default is near-white gray92 in its
    default Light appearance mode), the window title, a sensible minimum size,
    a fixed (non-resizable) frame, and transient-to-parent so the window
    manager treats it as a child of the app rather than an independent
    top-level. Centering and the input grab happen in make_modal(), once the
    body has been packed and the window knows its real size."""
    dialog.title(title)
    dialog.configure(fg_color=PALETTE["bg"])
    dialog.minsize(min_width, min_height)
    dialog.resizable(False, False)
    try:
        # transient() wants a real top-level as its argument; dialog.master may
        # be an inner frame (e.g. a view), so resolve up to its window.
        parent = dialog.master.winfo_toplevel()
        # Only mark the dialog transient-for a parent that is actually on
        # screen. A transient window whose master is withdrawn/unmapped is never
        # mapped by the X window manager at all -- it just never appears -- so a
        # modal wait_window() on it (App._ask_sudo_password_dialog) then blocks
        # forever with nothing shown. The launch-time sudo prompt is exactly
        # this case: App.__init__ withdraw()s the main window *before* opening
        # it. The other three dialogs (Add-as-Target / Add-manually / deauth
        # confirm) open over the already-deiconified main window, so they still
        # get the transient hint and stay grouped above their parent. Guarded
        # besides because this is pure WM polish -- a failure here must never
        # stop the dialog opening.
        if parent.winfo_ismapped():
            dialog.transient(parent)
    except Exception:
        pass


def make_modal(dialog: ctk.CTkToplevel, *, focus: Optional[ctk.CTkBaseClass] = None) -> None:
    """Center the dialog over its parent, give `focus` keyboard focus (if
    passed), and grab all input. Call once after every widget is packed so the
    centering reads the window's final requested size."""
    dialog.update_idletasks()
    _center_over_parent(dialog)
    if focus is not None:
        focus.focus_set()
    dialog.grab_set()


def _center_over_parent(dialog: ctk.CTkToplevel) -> None:
    try:
        parent = dialog.master.winfo_toplevel()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        if pw <= 1 or ph <= 1:
            return  # parent not mapped yet (e.g. the sudo prompt opens while the
            # main window is still withdrawn) -- let the WM place the dialog
            # rather than pinning it to a corner computed from a 1x1 parent.
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        dw, dh = dialog.winfo_width(), dialog.winfo_height()
        x = px + (pw - dw) // 2
        y = py + (ph - dh) // 2
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    except Exception:
        pass  # centering is polish, never worth raising over


def themed_entry(master, *, show: Optional[str] = None) -> ctk.CTkEntry:
    """A dark, hairline-bordered, flat (2px) entry in Hanken -- the mockup's
    .field input. `show="*"` for a password field."""
    kwargs = dict(
        fg_color=PALETTE["bg"], border_color=PALETTE["border"], text_color=PALETTE["text"],
        corner_radius=CORNER_RADIUS, font=ui_font(12),
    )
    if show is not None:
        kwargs["show"] = show
    return ctk.CTkEntry(master, **kwargs)


def field_label(master, text: str) -> ctk.CTkLabel:
    """A muted 11px field label (the mockup's .field label)."""
    return ctk.CTkLabel(master, text=text, font=ui_font(11), text_color=PALETTE["muted"])


def error_label(master, text: str = "") -> ctk.CTkLabel:
    """Danger-colored validation text, empty until there's something to say."""
    return ctk.CTkLabel(master, text=text, font=ui_font(11), text_color=PALETTE["danger"])


def primary_button(master, text: str, command, *, danger: bool = False) -> ctk.CTkButton:
    """The pages' accent-fill primary-action recipe (management_view.py's
    Start button). `danger=True` swaps accent for danger -- used by the deauth
    confirm's Fire button, same substitution CapturePanel's Fire Deauth makes."""
    color = PALETTE["danger"] if danger else PALETTE["accent"]
    return ctk.CTkButton(
        master, text=text, command=command,
        fg_color=color, hover_color=color, border_color=color,
        text_color=PALETTE["bg"], text_color_disabled=PALETTE["muted"],
        corner_radius=CORNER_RADIUS, font=ui_font(12, "bold"),
    )


def outline_button(master, text: str, command) -> ctk.CTkButton:
    """The pages' flat-bordered outline recipe (management_view.py's
    _make_outline_button) -- used for every dialog's Cancel.

    hover_color is set to PALETTE['panel'] rather than left unset: with a
    transparent fill and no explicit hover_color, CTkButton falls back to its
    own default-Light-theme hover (stock blue #3B8ED0 -- nothing calls
    set_appearance_mode), which was the "button highlights are blue" half of
    the report. The mockup's own rule (.btn-outline:hover -> border-color:
    --muted) tints the border, but CTkButton 6.0.0 has no hover-border kwarg
    (only hover_color, which fills), so a subtle dark panel fill is the closest
    deliberate, non-blue hover available -- 'panel' is one step up from 'bg',
    already the app's raised-surface token."""
    return ctk.CTkButton(
        master, text=text, command=command, fg_color="transparent", border_width=1,
        border_color=PALETTE["border"], text_color=PALETTE["text"],
        hover_color=PALETTE["panel"], corner_radius=CORNER_RADIUS,
        font=ui_font(12, "bold"),
    )
