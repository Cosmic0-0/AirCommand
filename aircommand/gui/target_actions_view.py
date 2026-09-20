"""TargetActionsView — composes TargetSelector + CapturePanel + EnumeratePanel;
owns the deauth confirm dialog. See docs/design/gui-structure.md 'Target
Actions tab'.
"""

from __future__ import annotations

from typing import Optional

import customtkinter as ctk

from aircommand.core import Target
from aircommand.core.events import TargetAdded, TargetRemoved
from aircommand.gui.capture_view import CapturePanel
from aircommand.gui.enumerate_view import EnumeratePanel
from aircommand.gui.target_selector import TargetSelector


class TargetActionsView(ctk.CTkFrame):
    def __init__(self, master, app) -> None:
        super().__init__(master)
        self._app = app

        # CapturePanel/EnumeratePanel are constructed before TargetSelector,
        # even though they're visually packed below it: TargetSelector.__init__
        # synchronously calls on_change (via _rebuild) with the initial
        # selection, and _on_target_changed below needs self.capture_panel/
        # self.enumerate_panel to already exist by the time that first call
        # happens. Packing order (which determines the actual layout) is set
        # separately below and is unaffected by this construction order.
        self.capture_panel = CapturePanel(self, app, confirm_deauth=self._confirm_deauth_dialog)
        self.enumerate_panel = EnumeratePanel(self, app)
        self.target_selector = TargetSelector(self, app, on_change=self._on_target_changed)

        self.target_selector.pack(side="top", fill="x", pady=(0, 10))
        self.capture_panel.pack(side="top", fill="both", expand=True, pady=(0, 10))
        self.enumerate_panel.pack(side="top", fill="both", expand=True)

        app.pump.on(TargetAdded, self.target_selector.upsert_row)
        app.pump.on(TargetRemoved, self.target_selector.remove_row)

    def _on_target_changed(self, target: Optional[Target]) -> None:
        self.capture_panel.set_target(target)
        self.enumerate_panel.set_target(target)

    def _confirm_deauth_dialog(self, target: Target) -> bool:
        from tkinter import messagebox

        return messagebox.askyesno(
            "Confirm deauth",
            f"This will actively transmit deauthentication frames at {target.ssid} "
            f"({target.bssid}) to force a handshake. Every firing is logged. Continue?",
        )
