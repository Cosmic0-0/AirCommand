"""StatusBar: the always-visible bottom bar for the PySide6 port. Privilege
indicator, the one-time reconciliation banner, the generic show_error surface,
and (ADR-0017) the active adapter's label.

See docs/design/gui-structure.md "Status bar" and
docs/design/gui-port/05-boundary-design.md "View mapping" ("Status bar
placement": the bar sits inside the right column only, so App packs it there,
not through QMainWindow.setStatusBar). The handlers and the event mapping port
as is; only the widget construction is new. StatusPill is the same shared
component the Management page's adapter rows use.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from aircommand.core import PrivilegeStatus, Target
from aircommand.core.events import SudoKeepaliveFailed, SudoKeepaliveRecovered
from aircommand.core.reconciliation import ReconciliationSummary
from aircommand.gui.cell import StatusPill
from aircommand.gui.theme import PALETTE

_ACTIVE_TEXT = "Sudo session active"
_LOST_TEXT = "Privilege: LOST, sudo keepalive failing"


class StatusBar(QFrame):
    def __init__(
        self,
        parent: Optional[QWidget],
        privilege_status: PrivilegeStatus,
        reconciliation: ReconciliationSummary,
        interrupted_deauth_targets: list[Target],
        adapter: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("class", "statusbar")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 6, 18, 6)
        layout.setSpacing(10)

        # Always ACTIVE at construction: StatusBar is only ever built after
        # Engine.privilege.start() has already succeeded (App's startup
        # sequencing). show_sudo_warning / clear_sudo_warning flip it later.
        # privilege_status stays a parameter for that assumption, unused beyond it.
        self._privilege_pill = StatusPill(_ACTIVE_TEXT, active=True)
        layout.addWidget(self._privilege_pill)

        self._adapter_label = QLabel(self._adapter_text(adapter))
        self._adapter_label.setProperty("class", "adapterLabel")
        layout.addWidget(self._adapter_label)

        self._reconciliation_banner: Optional[QFrame] = None
        if reconciliation.processes_terminated > 0:
            text = (
                f"Cleaned up {reconciliation.processes_terminated} leftover "
                "process(es) from a previous crash."
            )
            if interrupted_deauth_targets:
                text += ", see the Logs page"   # the Logs page is where AuditLogView lives now

            banner = QFrame()
            banner_layout = QHBoxLayout(banner)
            banner_layout.setContentsMargins(0, 0, 0, 0)
            banner_layout.setSpacing(6)
            message = QLabel(text)
            # No warn-text class exists in the pinned theme, so the one warn color
            # comes straight from PALETTE (still the single source of color).
            message.setStyleSheet(f"color: {PALETTE['warn']};")
            dismiss = QPushButton("×")   # a multiplication sign, the mockup's dismiss glyph
            dismiss.setProperty("class", "chipRemove")
            dismiss.clicked.connect(banner.hide)
            banner_layout.addWidget(message)
            banner_layout.addWidget(dismiss)
            layout.addWidget(banner)
            self._reconciliation_banner = banner

        self._error_label = QLabel("")
        self._error_label.setProperty("class", "errorLabel")
        layout.addWidget(self._error_label)
        layout.addStretch(1)

    def show_sudo_warning(self, event: SudoKeepaliveFailed) -> None:
        self._privilege_pill.set_state(PALETTE["warn"], _LOST_TEXT)

    def clear_sudo_warning(self, event: SudoKeepaliveRecovered) -> None:
        self._privilege_pill.set_state(PALETTE["success"], _ACTIVE_TEXT)

    def show_error(self, message: str) -> None:
        self._error_label.setText(message)

    @staticmethod
    def _adapter_text(adapter: Optional[str]) -> str:
        return f"Adapter: {adapter}" if adapter is not None else "Adapter: --"

    def show_adapter(self, adapter: Optional[str]) -> None:
        """Called by App.on_adapter_selected() (ADR-0017) when the Management
        page switches adapters. There is no bus event for this (ADR-0017
        Decision 16), so it is a plain method call, not a pump subscription."""
        self._adapter_label.setText(self._adapter_text(adapter))
