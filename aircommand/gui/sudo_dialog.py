"""SudoPasswordDialog — the launch-time modal that collects the sudo password
Engine.privilege.start() needs (ADR-0002). See docs/design/gui-structure.md
'Sudo password dialog'.
"""

from __future__ import annotations

from typing import Optional

import customtkinter as ctk

from aircommand.gui.theme import CORNER_RADIUS, PALETTE, ui_font


class SudoPasswordDialog(ctk.CTkToplevel):
    def __init__(self, master, *, error: Optional[str] = None) -> None:
        super().__init__(master)
        self.title("AirCommand — sudo password")
        self.result: Optional[str] = None

        # Themed to match the app's other CTkToplevel modals (_AddAsTargetDialog /
        # _AddManuallyDialog in discovery_view.py): primary instruction copy in
        # Hanken via ui_font() and PALETTE["text"], not CTk's own default Roboto
        # label -- that default was the visible "doesn't match the rest of the
        # program" gap the user reported.
        ctk.CTkLabel(
            self, text="AirCommand needs your sudo password to control the adapter.",
            font=ui_font(12), text_color=PALETTE["text"],
        ).pack(padx=20, pady=(20, 10))

        # Same dark/bordered/flat entry recipe as _AddManuallyDialog's _themed_entry()
        # (dark PALETTE["bg"] fill, hairline PALETTE["border"], 2px shape-lock radius,
        # Hanken ui_font) -- CTk's unstyled default entry was a rounded light box,
        # the single most jarring mismatch here. show="*" stays: this is a password.
        self._entry = ctk.CTkEntry(
            self, show="*", fg_color=PALETTE["bg"], border_color=PALETTE["border"],
            text_color=PALETTE["text"], corner_radius=CORNER_RADIUS, font=ui_font(12),
        )
        self._entry.pack(padx=20, pady=(0, 10), fill="x")
        self._entry.bind("<Return>", lambda event: self._on_ok())

        # Same error-label convention as both sibling dialogs: PALETTE["danger"]
        # text at ui_font(11).
        self._error_label = ctk.CTkLabel(
            self, text=error or "", font=ui_font(11), text_color=PALETTE["danger"]
        )
        self._error_label.pack(padx=20, pady=(0, 10))

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(padx=20, pady=(0, 20), fill="x")

        # OK/Cancel are left on CTk's default button styling on purpose: both
        # sibling modals (_AddAsTargetDialog / _AddManuallyDialog) do the same for
        # their Add/Cancel, so this keeps every dialog's buttons uniform. (Moving
        # all dialogs onto the pages' accent/outline button vocabulary would be a
        # separate, cross-dialog change, not a sudo-only one.)
        self._ok_button = ctk.CTkButton(button_row, text="OK", command=self._on_ok)
        self._ok_button.pack(side="right", padx=(10, 0))

        self._cancel_button = ctk.CTkButton(button_row, text="Cancel", command=self._on_cancel)
        self._cancel_button.pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self._entry.focus_set()
        self.grab_set()

    def _on_ok(self) -> None:
        self.result = self._entry.get()
        self.destroy()

    def _on_cancel(self) -> None:
        self.result = None
        self.destroy()
