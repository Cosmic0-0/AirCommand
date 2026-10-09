"""StatusBar — the always-visible bottom bar: privilege indicator, one-time
reconciliation banner, the generic show_error() surface, and (ADR-0017) the
active adapter's label. See docs/design/gui-structure.md 'Status bar' and
the design spec's §3/§4 (the rail background; "a 'Sudo session active'
status pill plus the active adapter's label, both Fira Code, 11px" --
StatusPill is the same component the Management page's adapter rows use).
The adapter label is live-updatable, not seeded once -- adapter selection
can now change mid-session (ADR-0017's select_adapter()), unlike privilege/
reconciliation which are fixed for the session by the time this widget is
constructed.
"""

from __future__ import annotations

from typing import Optional

import customtkinter as ctk

from aircommand.core import PrivilegeStatus, Target
from aircommand.core.events import SudoKeepaliveFailed, SudoKeepaliveRecovered
from aircommand.core.reconciliation import ReconciliationSummary
from aircommand.gui.cell import StatusPill
from aircommand.gui.theme import PALETTE, mono_font, ui_font

_ACTIVE_TEXT = "Sudo session active"
_LOST_TEXT = "Privilege: LOST — sudo keepalive failing"


class StatusBar(ctk.CTkFrame):
    def __init__(
        self,
        master,
        privilege_status: PrivilegeStatus,
        reconciliation: ReconciliationSummary,
        interrupted_deauth_targets: list[Target],
        adapter: Optional[str] = None,
    ) -> None:
        super().__init__(master, fg_color=PALETTE["rail"], corner_radius=0)

        # Always ACTIVE at construction time -- StatusBar is only ever built
        # after Engine.privilege.start() has already succeeded (app.py's own
        # startup sequencing); show_sudo_warning()/clear_sudo_warning() are
        # what later flip this to the LOST state. privilege_status is still
        # accepted as a param (unused beyond this assumption) rather than
        # dropped, since changing App's own call site isn't this restyle's job.
        self._privilege_pill = StatusPill(self, _ACTIVE_TEXT, active=True)
        self._privilege_pill.pack(side="left", padx=10, pady=5)

        self._adapter_label = ctk.CTkLabel(
            self, text=self._adapter_text(adapter), font=mono_font(11), text_color=PALETTE["muted"],
        )
        self._adapter_label.pack(side="left", padx=10, pady=5)

        self._reconciliation_banner: Optional[ctk.CTkFrame] = None
        if reconciliation.processes_terminated > 0:
            text = f"Cleaned up {reconciliation.processes_terminated} leftover process(es) from a previous crash."
            if interrupted_deauth_targets:
                text += " — see the Logs page"   # renamed from "Audit Log tab" --
                # this redesign's Logs page is what AuditLogView now lives on.

            banner = ctk.CTkFrame(self, fg_color="transparent")
            ctk.CTkLabel(banner, text=text, font=ui_font(11), text_color=PALETTE["warn"]).pack(
                side="left", padx=(10, 5)
            )
            ctk.CTkButton(
                banner, text="×", width=20, command=banner.destroy, fg_color="transparent",
                text_color=PALETTE["muted"], hover_color=PALETTE["panel"],
            ).pack(side="left")
            banner.pack(side="left", padx=10, pady=5)
            self._reconciliation_banner = banner

        self._error_label = ctk.CTkLabel(self, text="", font=ui_font(11), text_color=PALETTE["danger"])
        self._error_label.pack(side="left", padx=10, pady=5)

    def show_sudo_warning(self, event: SudoKeepaliveFailed) -> None:
        self._privilege_pill.set_state(PALETTE["warn"], _LOST_TEXT)

    def clear_sudo_warning(self, event: SudoKeepaliveRecovered) -> None:
        self._privilege_pill.set_state(PALETTE["success"], _ACTIVE_TEXT)

    def show_error(self, message: str) -> None:
        self._error_label.configure(text=message)

    @staticmethod
    def _adapter_text(adapter: Optional[str]) -> str:
        return f"Adapter: {adapter}" if adapter is not None else "Adapter: --"

    def show_adapter(self, adapter: Optional[str]) -> None:
        """Called by App.on_adapter_selected() (ADR-0017) whenever the
        Management page successfully switches adapters -- there's no bus
        event for this (see ADR-0017 Decision 16), so this is a plain method
        call, not a pump subscription."""
        self._adapter_label.configure(text=self._adapter_text(adapter))
