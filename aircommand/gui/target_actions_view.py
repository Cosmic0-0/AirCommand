"""TargetActionsView — composes TargetSelector + CapturePanel + EnumeratePanel;
owns the deauth confirm dialog. See docs/design/gui-structure.md 'Target
Actions tab'.
"""

from __future__ import annotations

from typing import Optional

import customtkinter as ctk

from aircommand.core import Target
from aircommand.core.events import TargetAdded, TargetRemoved
from aircommand.gui import dialog
from aircommand.gui.capture_view import CapturePanel
from aircommand.gui.enumerate_view import EnumeratePanel
from aircommand.gui.target_selector import TargetSelector
from aircommand.gui.theme import PALETTE, ui_font


class TargetActionsView(ctk.CTkFrame):
    def __init__(self, master, app) -> None:
        # fg_color="transparent" so the page's dark PALETTE["bg"] shows through
        # the gaps around the cells, exactly like this page's sibling containers
        # (NetworksView, ManagementView, CapturePanel, ...). Left unset, this
        # frame fell back to CustomTkinter's default -- which, because nothing
        # sets dark appearance mode, resolves to the light-theme gray86 -- and
        # since CapturePanel/EnumeratePanel/TargetSelector are all transparent,
        # that near-white showed through as the "odd white areas" on this page.
        super().__init__(master, fg_color="transparent")
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
        # ADR-0001: deauth stays in scope only *with* extra confirmation
        # friction, on every firing, with no way to skip it. This replaces the
        # old tkinter.messagebox.askyesno (which can't be themed and read as a
        # light native popup against the dark app) with a themed modal that
        # returns the same bool. The gate is unchanged: this still blocks until
        # the operator answers and CapturePanel still refuses to start unless it
        # returns True. Default focus is on Cancel and Enter/Escape both cancel,
        # so an accidental keypress can never fire the burst.
        confirm = _DeauthConfirmDialog(self, target)
        self.wait_window(confirm)
        return confirm.result


class _DeauthConfirmDialog(ctk.CTkToplevel):
    """Themed replacement for the native yes/no deauth confirmation. `result`
    is False unless the operator explicitly clicks Fire Deauth -- closing the
    window, Cancel, Enter and Escape all leave it False."""

    def __init__(self, master, target: Target) -> None:
        super().__init__(master)
        self.result = False
        dialog.apply_chrome(self, "Confirm deauth", min_width=440, min_height=180)

        ctk.CTkLabel(
            self,
            text=(
                f"This will actively transmit deauthentication frames at {target.ssid} "
                f"({target.bssid}) to force a handshake. Every firing is logged. Continue?"
            ),
            font=ui_font(12), text_color=PALETTE["text"], wraplength=400, justify="left",
        ).pack(padx=20, pady=(20, 16))

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(padx=20, pady=(0, 20), fill="x")
        # Fire is the danger primary; Cancel is the outline default. Fire packs
        # to the right of Cancel but is NOT the focus default -- see _on_cancel.
        self._fire_button = dialog.primary_button(button_row, "Fire Deauth", self._on_fire, danger=True)
        self._fire_button.pack(side="right", padx=(10, 0))
        self._cancel_button = dialog.outline_button(button_row, "Cancel", self._on_cancel)
        self._cancel_button.pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.bind("<Escape>", lambda _event: self._on_cancel())
        self.bind("<Return>", lambda _event: self._on_cancel())  # Enter cancels, never fires
        dialog.make_modal(self, focus=self._cancel_button)

    def _on_fire(self) -> None:
        self.result = True
        self.destroy()

    def _on_cancel(self) -> None:
        self.result = False
        self.destroy()
