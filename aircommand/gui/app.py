"""App — owns the single Engine instance. See docs/design/gui-structure.md
'Startup sequencing (App.__init__)' for the pinned sequencing this file
implements.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import customtkinter as ctk

from aircommand.core import Engine, InvalidSudoPasswordError
from aircommand.core.events import (
    DeauthFired,
    HandshakeCaptured,
    NetworkDiscovered,
    NetworkSightingUpdated,
    SudoKeepaliveFailed,
    SudoKeepaliveRecovered,
    TargetAdded,
    TargetRemoved,
)
from aircommand.core.procutil import ProcRunner
from aircommand.gui.audit_log_view import AuditLogView
from aircommand.gui.crack_view import CrackPanel, HandshakePicker, WordlistPicker
from aircommand.gui.discovery_view import NetworksView, TargetPicker
from aircommand.gui.event_pump import GuiEventPump
from aircommand.gui.status_bar import StatusBar
from aircommand.gui.sudo_dialog import SudoPasswordDialog
from aircommand.gui.target_actions_view import TargetActionsView


class App(ctk.CTk):
    def __init__(
        self, db_path: Path, work_dir: Path, adapter: str, proc: Optional[ProcRunner] = None
    ) -> None:
        super().__init__()
        self.title("AirCommand")

        self.engine = Engine(db_path=db_path, work_dir=work_dir, adapter=adapter, proc=proc)

        password = self._ask_sudo_password_dialog()
        if password is None:      # dialog cancelled/closed on the very first prompt --
            self.destroy()        # the pinned sketch only guards the retry branch below,
            raise SystemExit(0)   # but "no degraded mode" applies here too; see report.
        while True:
            try:
                self.engine.privilege.start(password)
                break
            except InvalidSudoPasswordError:
                password = self._ask_sudo_password_dialog(error="Incorrect password — try again")
                if password is None:      # dialog cancelled/closed
                    self.destroy()
                    raise SystemExit(0)   # privilege is required before ANY Action,
                                          # including Discovery — no degraded mode

        reconciliation = self.engine.reconcile_startup()   # sync return, see below —
        # nothing is subscribed to the pump yet, matching the existing comment in
        # the current stub.
        targets_by_id = {t.id: t for t in self.engine.targets.list()}
        self._interrupted_deauth_targets = [
            targets_by_id[tid] for tid in reconciliation.interrupted_deauth_target_ids
            if tid in targets_by_id
        ]  # tid may be absent if the Target was removed from the allowlist between
           # the crash and this launch — silently excluded, nothing left to flag it against.

        self.pump = GuiEventPump(self.engine, self)

        self.status_bar = StatusBar(self, self.engine.privilege.status,
                                     reconciliation, self._interrupted_deauth_targets)
        self.status_bar.pack(side="bottom", fill="x")
        self.pump.on(SudoKeepaliveFailed, self.status_bar.show_sudo_warning)
        self.pump.on(SudoKeepaliveRecovered, self.status_bar.clear_sudo_warning)

        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(fill="both", expand=True)
        self._build_discovery_targets_tab()
        self._build_target_actions_tab()
        self._build_crack_tab()
        self._build_audit_log_tab()   # gets self._interrupted_deauth_targets

        self.protocol("WM_DELETE_WINDOW", self.on_close)   # NOT wired in the
        # current stub — on_close() already exists but nothing calls it.

        self.pump.start()
        self._discovery_handle = self.engine.discovery.start()   # auto-starts;
        # see "Target Actions tab" for how the user frees the radio (a later slice).

    def _ask_sudo_password_dialog(self, error: Optional[str] = None) -> Optional[str]:
        dialog = SudoPasswordDialog(self, error=error)
        self.wait_window(dialog)
        return dialog.result

    def _build_discovery_targets_tab(self) -> None:
        tab = self.tabview.add("Discovery & Targets")
        self._discovery_paused = False
        self._pause_resume_button = ctk.CTkButton(
            tab, text="Pause Discovery", command=self._on_pause_resume_discovery_clicked
        )
        self._pause_resume_button.pack(side="top", anchor="w", padx=10, pady=(10, 0))
        self.networks_view = NetworksView(tab, self)
        self.networks_view.pack(side="top", fill="both", expand=True, padx=10, pady=(5, 5))
        self.target_picker = TargetPicker(tab, self)
        self.target_picker.pack(side="top", fill="both", expand=True, padx=10, pady=(5, 10))
        self.pump.on(NetworkDiscovered, self.networks_view.upsert_row)
        self.pump.on(NetworkSightingUpdated, self.networks_view.upsert_row)
        self.pump.on(TargetAdded, self.target_picker.upsert_row)
        self.pump.on(TargetRemoved, self.target_picker.remove_row)

    def _on_pause_resume_discovery_clicked(self) -> None:
        if self._discovery_paused:
            self._discovery_handle = self.engine.discovery.start()
            self._discovery_paused = False
            self._pause_resume_button.configure(text="Pause Discovery")
        else:
            self._discovery_handle.cancel()
            self._discovery_paused = True
            self._pause_resume_button.configure(text="Resume Discovery")

    def _build_target_actions_tab(self) -> None:
        tab = self.tabview.add("Target Actions")
        self.target_actions_view = TargetActionsView(tab, self)
        self.target_actions_view.pack(fill="both", expand=True, padx=10, pady=10)

    def _build_crack_tab(self) -> None:
        tab = self.tabview.add("Crack")
        self.handshake_picker = HandshakePicker(tab, self, on_selected=self._on_handshake_selected)
        self.handshake_picker.pack(side="top", fill="both", expand=True, pady=(0, 10))
        self.wordlist_picker = WordlistPicker(tab, on_selected=self._on_wordlist_selected)
        self.wordlist_picker.pack(side="top", fill="x", pady=(0, 10))
        self.crack_panel = CrackPanel(tab, self)
        self.crack_panel.pack(side="top", fill="both", expand=True)
        self.pump.on(HandshakeCaptured, self.handshake_picker.append)

    def _on_handshake_selected(self, handshake) -> None:
        self.crack_panel.set_handshake(handshake)

    def _on_wordlist_selected(self, path) -> None:
        self.crack_panel.set_wordlist(path)

    def _build_audit_log_tab(self) -> None:
        tab = self.tabview.add("Audit Log")
        self.audit_log_view = AuditLogView(tab, self, self._interrupted_deauth_targets)
        self.audit_log_view.pack(fill="both", expand=True, padx=10, pady=10)
        self.pump.on(DeauthFired, self.audit_log_view.append)

    def on_close(self) -> None:
        self.engine.shutdown()
        self.destroy()   # missing from the current stub too — shutdown() alone
                          # stops the engine but doesn't close the Tk window
