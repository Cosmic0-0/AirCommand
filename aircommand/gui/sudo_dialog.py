"""SudoPasswordDialog — the launch-time modal that collects the sudo password
Engine.privilege.start() needs (ADR-0002). See docs/design/gui-structure.md
'Sudo password dialog'.

Styled entirely through aircommand/gui/dialog.py's shared Grid Watch helpers
(themed toplevel background, themed entry, accent/outline buttons, centering)
-- the same helpers the Add-as-Target / Add-manually / deauth-confirm dialogs
use, so all four match the pages instead of falling back to CustomTkinter's
light-theme defaults (near-white window, stock-blue buttons).
"""

from __future__ import annotations

from typing import Optional

import customtkinter as ctk

from aircommand.gui import dialog
from aircommand.gui.theme import PALETTE, ui_font


class SudoPasswordDialog(ctk.CTkToplevel):
    def __init__(self, master, *, error: Optional[str] = None) -> None:
        super().__init__(master)
        self.result: Optional[str] = None
        dialog.apply_chrome(self, "AirCommand — sudo password", min_width=420, min_height=190)

        ctk.CTkLabel(
            self, text="AirCommand needs your sudo password to control the adapter.",
            font=ui_font(12), text_color=PALETTE["text"], wraplength=380, justify="left",
        ).pack(padx=20, pady=(20, 10))

        self._entry = dialog.themed_entry(self, show="*")  # show="*" stays: this is a password.
        self._entry.pack(padx=20, pady=(0, 10), fill="x")
        self._entry.bind("<Return>", lambda event: self._on_ok())

        self._error_label = dialog.error_label(self, error or "")
        self._error_label.pack(padx=20, pady=(0, 10))

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(padx=20, pady=(0, 20), fill="x")

        # OK is the accent primary; Cancel is the outline recipe -- the pages'
        # own button vocabulary, not CTk's default blue (the previous mismatch).
        self._ok_button = dialog.primary_button(button_row, "OK", self._on_ok)
        self._ok_button.pack(side="right", padx=(10, 0))

        self._cancel_button = dialog.outline_button(button_row, "Cancel", self._on_cancel)
        self._cancel_button.pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        dialog.make_modal(self, focus=self._entry)

    def _on_ok(self) -> None:
        self.result = self._entry.get()
        self.destroy()

    def _on_cancel(self) -> None:
        self.result = None
        self.destroy()
