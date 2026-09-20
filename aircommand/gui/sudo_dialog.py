"""SudoPasswordDialog — the launch-time modal that collects the sudo password
Engine.privilege.start() needs (ADR-0002). See docs/design/gui-structure.md
'Sudo password dialog'.
"""

from __future__ import annotations

from typing import Optional

import customtkinter as ctk


class SudoPasswordDialog(ctk.CTkToplevel):
    def __init__(self, master, *, error: Optional[str] = None) -> None:
        super().__init__(master)
        self.title("AirCommand — sudo password")
        self.result: Optional[str] = None

        ctk.CTkLabel(self, text="AirCommand needs your sudo password to control the adapter.").pack(
            padx=20, pady=(20, 10)
        )

        self._entry = ctk.CTkEntry(self, show="*")
        self._entry.pack(padx=20, pady=(0, 10), fill="x")

        self._error_label = ctk.CTkLabel(self, text=error or "", text_color="red")
        self._error_label.pack(padx=20, pady=(0, 10))

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(padx=20, pady=(0, 20), fill="x")

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
