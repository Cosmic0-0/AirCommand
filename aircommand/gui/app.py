"""App — owns the single Engine instance. See docs/design/gui-structure.md
'Startup sequencing (App.__init__)' for the pinned sequencing this file
implements.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import customtkinter as ctk

from aircommand.core import Engine, InvalidSudoPasswordError, RadioCommandFailed
from aircommand.core.domain import StopReason
from aircommand.core.events import (
    DeauthFired,
    DiscoveryStopped,
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
        self.withdraw()   # hide the (still empty) main window until the sudo prompt
        # below succeeds -- otherwise it maps on top of the password dialog, and the
        # user sees two windows at launch. Re-shown by self.deiconify() below.
        self._closing = False   # guards on_close() against a re-entrant WM-close click

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

        self.deiconify()   # privilege is up and the UI is fully built -- show the main
        self.update_idletasks()   # window now (and lay it out) so it's already visible while
        # discovery.start() below blocks on its synchronous airmon-ng call.

        self.pump.start()
        try:
            self._discovery_handle = self.engine.discovery.start()   # auto-starts;
            # see "Target Actions tab" for how the user frees the radio (a later slice).
        except RadioCommandFailed as e:
            self.status_bar.show_error(f"Discovery couldn't start: {e}")
            self._discovery_handle = None
            # Nothing to pause/resume if Discovery never started -- the "else"
            # branch below would otherwise call .cancel() on None.
            self._pause_resume_button.configure(state="disabled")
        else:
            self.pump.on(DiscoveryStopped, self._on_discovery_stopped, only_job=self._discovery_handle.job_id)

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
            try:
                self._discovery_handle = self.engine.discovery.start()
            except RadioCommandFailed as e:
                self.status_bar.show_error(f"Discovery couldn't resume: {e}")
                return  # stay paused; don't touch the stale handle or button text
            self.pump.on(DiscoveryStopped, self._on_discovery_stopped, only_job=self._discovery_handle.job_id)
            self._discovery_paused = False
            self._pause_resume_button.configure(text="Pause Discovery")
        else:
            self._discovery_handle.cancel()
            self._discovery_paused = True
            self._pause_resume_button.configure(text="Resume Discovery")

    def _on_discovery_stopped(self, event: DiscoveryStopped) -> None:
        if event.reason != StopReason.ERROR:
            return  # CANCELLED is the normal Pause-button/shutdown path, already
            # reflected synchronously by the click handler above -- nothing to add.
        message = "Discovery stopped unexpectedly — check your adapter/sudo session, then Resume"
        if event.error_detail is not None:   # best-effort hint from airodump-ng's own
            message += f" ({event.error_detail})"   # stderr -- see events.py's own docstring
        self.status_bar.show_error(message)
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
        # ThingsToChange item 4: Engine.shutdown() can take several real
        # seconds (up to SHUTDOWN_JOB_WAIT_TIMEOUT_S per still-running job,
        # plus blocking airmon-ng/systemctl calls) and used to run entirely on
        # this (the GUI/main) thread with nothing painted -- the window just
        # sat frozen, with no sign anything was happening.
        #
        # Deliberately NOT backgrounding shutdown() on its own thread (a
        # version of this fix was built and then reverted): Engine's main
        # sqlite3 connection is check_same_thread=True (persistence/db.py),
        # bound to whichever thread constructed it -- shutdown()'s own
        # self._db.close() would raise sqlite3.ProgrammingError if run from a
        # different thread (confirmed empirically). Splitting shutdown() to
        # work around that is possible, but the actual background thread plus
        # the self.after()-polling needed to rejoin it measurably slowed down
        # and destabilized this project's own test suite (every GUI test's
        # on_close() paid real Tk/X11 window-creation overhead, which
        # intermittently starved an unrelated real-thread-timing test under
        # full-suite load -- confirmed directly, not assumed, by bisecting
        # which change introduced the new flake). Not worth that risk for what
        # the user actually asked for: "tell me it's closing", not "keep the
        # window perfectly responsive the whole time". Showing this dialog
        # and forcing it to paint BEFORE the blocking call starts already
        # does that -- see docs/final-touches.md's own "at minimum" framing
        # for this exact item.
        if self._closing:
            return
        self._closing = True

        dialog = _ClosingDialog(self)
        self.update_idletasks()   # force the paint now, before engine.shutdown()
        # below blocks this thread -- otherwise the dialog would be created but
        # never actually drawn until shutdown() already finished, defeating the point.

        self.engine.shutdown()
        dialog.destroy()
        self.destroy()


class _ClosingDialog(ctk.CTkToplevel):
    """Modal, no buttons, nothing for the user to do -- shutdown can't be
    cancelled, and this dialog is only ever up for the ordinary case's
    sub-second duration or the stuck-job case's single-digit seconds, not long
    enough to need a progress indicator of its own (see App.on_close()'s own
    comment for why this stays a plain synchronous blocking call rather than
    something that could paint one). Same modal convention as
    SudoPasswordDialog (grab_set(), WM_DELETE_WINDOW overridden to a no-op --
    there's no cancel route once shutdown has started)."""

    def __init__(self, master) -> None:
        super().__init__(master)
        self.title("AirCommand — closing")
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # no escape route -- see class docstring

        # No progress bar: this window never gets another chance to redraw
        # once App.on_close() blocks on engine.shutdown() right after creating
        # this dialog, so an "indeterminate" animation would never actually
        # animate -- it'd just paint one static frame, same as this label
        # does, but less honestly. The label alone already satisfies the
        # actual complaint (nothing currently tells the user closing is in
        # progress at all).
        ctk.CTkLabel(self, text="Closing AirCommand…\nplease wait", justify="center").pack(padx=30, pady=20)

        self.resizable(False, False)
        self.grab_set()
