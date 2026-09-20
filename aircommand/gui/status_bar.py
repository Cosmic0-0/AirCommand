"""StatusBar — the always-visible bottom bar: privilege indicator, one-time
reconciliation banner, and the generic show_error() surface. See
docs/design/gui-structure.md 'Status bar'.
"""

from __future__ import annotations

from typing import Optional

import customtkinter as ctk

from aircommand.core import PrivilegeStatus, Target
from aircommand.core.events import SudoKeepaliveFailed, SudoKeepaliveRecovered
from aircommand.core.reconciliation import ReconciliationSummary

_ACTIVE_TEXT = f"Privilege: {PrivilegeStatus.ACTIVE.value.upper()}"
_LOST_TEXT = "Privilege: LOST — sudo keepalive failing"
_WARNING_COLOR = "orange"


class StatusBar(ctk.CTkFrame):
    def __init__(
        self,
        master,
        privilege_status: PrivilegeStatus,
        reconciliation: ReconciliationSummary,
        interrupted_deauth_targets: list[Target],
    ) -> None:
        super().__init__(master)

        self._privilege_label = ctk.CTkLabel(self, text=f"Privilege: {privilege_status.value.upper()}")
        self._default_text_color = self._privilege_label.cget("text_color")
        self._privilege_label.pack(side="left", padx=10, pady=5)

        self._reconciliation_banner: Optional[ctk.CTkFrame] = None
        if reconciliation.processes_terminated > 0:
            text = f"Cleaned up {reconciliation.processes_terminated} leftover process(es) from a previous crash."
            if interrupted_deauth_targets:
                text += " — see the Audit Log tab"

            banner = ctk.CTkFrame(self, fg_color="transparent")
            ctk.CTkLabel(banner, text=text).pack(side="left", padx=(10, 5))
            ctk.CTkButton(
                banner, text="×", width=20, command=banner.destroy
            ).pack(side="left")
            banner.pack(side="left", padx=10, pady=5)
            self._reconciliation_banner = banner

        self._error_label = ctk.CTkLabel(self, text="")
        self._error_label.pack(side="left", padx=10, pady=5)

    def show_sudo_warning(self, event: SudoKeepaliveFailed) -> None:
        self._privilege_label.configure(text=_LOST_TEXT, text_color=_WARNING_COLOR)

    def clear_sudo_warning(self, event: SudoKeepaliveRecovered) -> None:
        self._privilege_label.configure(text=_ACTIVE_TEXT, text_color=self._default_text_color)

    def show_error(self, message: str) -> None:
        self._error_label.configure(text=message)
