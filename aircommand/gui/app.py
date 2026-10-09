"""App — owns the single Engine instance. See docs/design/gui-structure.md
'Startup sequencing (App.__init__)' for the originally-pinned sequencing this
file implements, and docs/design/gui-redesign-gridwatch.md for the Gridwatch
x Wireframe A shell (collapsible sidebar nav + five pages) this file now
builds instead of the original four-tab CTkTabview.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import customtkinter as ctk

from aircommand.core import (
    AdapterBusy,
    Band,
    BandUnavailable,
    DiscoveryOptions,
    Engine,
    InvalidSudoPasswordError,
    NoAdapterSelected,
    RadioCommandFailed,
)
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
from aircommand.gui.management_view import ManagementView
from aircommand.gui.sidebar import Sidebar
from aircommand.gui.status_bar import StatusBar
from aircommand.gui.sudo_dialog import SudoPasswordDialog
from aircommand.gui.target_actions_view import TargetActionsView
from aircommand.gui.theme import PALETTE

# ADR-0018: sentinel label for the band menu's "nothing picked yet" placeholder.
# Deliberately never added as a key in self._band_choices -- looking it up
# there must never happen (Start/Resume/New Session are all disabled whenever
# the menu reads this), so a future bug that forgets that gate fails loudly
# (KeyError) rather than silently passing bands=None into DiscoveryOptions
# (ADR-0018's "Considered Options", point 3). Defined once here so the exact
# same literal backs both the menu's initial value list and the gate check.
_BAND_UNCHOSEN = "Choose a band…"

# ADR-0017 + the Gridwatch redesign: Management is the landing page -- the
# mockup's own page copy ("The console lands here right after the sudo
# prompt, before Discovery or Capture starts") is a real change from the
# previous four-tab shell, which opened on Discovery & Targets by default
# (CTkTabview's own first-added-tab convention).
_LANDING_PAGE = "management"


class App(ctk.CTk):
    def __init__(
        self, db_path: Path, work_dir: Path, adapter: Optional[str] = None, proc: Optional[ProcRunner] = None
    ) -> None:
        super().__init__()
        self.title("AirCommand")
        self.configure(fg_color=PALETTE["bg"])
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

        # Shell: sidebar (left) + a main column (right) holding the page content
        # area, then a plain statusbar pinned to its bottom -- the design spec's
        # §4. The statusbar sits INSIDE the main column, not spanning the
        # sidebar too (unlike the previous shell, where it spanned the whole
        # window) -- see gui-redesign-gridwatch.md's own "Overall shell" wording.
        self.sidebar = Sidebar(self, on_navigate=self._on_navigate)
        self.sidebar.pack(side="left", fill="y")

        self._main_column = ctk.CTkFrame(self, fg_color=PALETTE["bg"], corner_radius=0)
        self._main_column.pack(side="left", fill="both", expand=True)

        self._content = ctk.CTkFrame(self._main_column, fg_color="transparent")
        self._content.pack(side="top", fill="both", expand=True)

        self.status_bar = StatusBar(
            self._main_column, self.engine.privilege.status, reconciliation,
            self._interrupted_deauth_targets, self.engine.radio.selected_adapter,
        )
        self.status_bar.pack(side="bottom", fill="x")
        self.pump.on(SudoKeepaliveFailed, self.status_bar.show_sudo_warning)
        self.pump.on(SudoKeepaliveRecovered, self.status_bar.clear_sudo_warning)

        self._pages: dict[str, ctk.CTkFrame] = {}
        self._build_management_page()
        self._build_discovery_targets_page()
        self._build_capture_attack_page()
        self._build_cracking_page()
        self._build_logs_page()
        self._show_page(_LANDING_PAGE)

        self.protocol("WM_DELETE_WINDOW", self.on_close)   # NOT wired in the
        # current stub — on_close() already exists but nothing calls it.

        self.deiconify()   # privilege is up and the UI is fully built -- show the main
        self.update_idletasks()   # window now, laid out, rather than on the first mainloop pass.

        self.pump.start()   # Discovery does NOT start here: the operator picks a Band and
        # clicks "Start Discovery" (_start_discovery), so nothing scans -- and airmon-ng
        # doesn't run -- until then.

    def _ask_sudo_password_dialog(self, error: Optional[str] = None) -> Optional[str]:
        dialog = SudoPasswordDialog(self, error=error)
        self.wait_window(dialog)
        return dialog.result

    # -- Sidebar navigation ---------------------------------------------------

    def _on_navigate(self, key: str) -> None:
        self._show_page(key)
        self.sidebar.set_active(key)

    def _show_page(self, key: str) -> None:
        if key not in self._pages:
            raise ValueError(f"unknown page: {key!r}")
        for page_key, page in self._pages.items():
            if page_key == key:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        if key == "management":
            self.management_view.refresh()   # populate the page the operator just opened,
            # same "populating a page the user just opened" exception
            # AuditLogView already relies on -- no bus event for this (ADR-0017).

    # -- Adapter selection (ADR-0017) -----------------------------------------

    def on_adapter_selected(self) -> None:
        """Called by ManagementView right after a successful
        RadioController.select_adapter() -- RadioController itself has no
        EventBus dependency to push this through (ADR-0017 Decision 16), so
        this plain method call is what fans the new selection out to every
        other page that cares: Discovery's band dropdown and the status
        bar's adapter label."""
        self._refresh_band_menu()
        self.status_bar.show_adapter(self.engine.radio.selected_adapter)
        if self._discovery_paused:
            # A Discovery session is scoped to one physical adapter
            # (ADR-0010's session concept) -- switching adapters while
            # Paused doesn't carry the old session over (ADR-0017 pt 17).
            # Note: switching while Scanning/Pausing can't reach this point
            # at all -- select_adapter() itself raises AdapterBusy while the
            # radio is reserved, so ManagementView never calls this method
            # in that case.
            self.networks_view.clear()
            self._discovery_paused = False
            self._discovery_handle = None
            self._pause_resume_button.configure(text="Start Discovery")

    # -- Management page -------------------------------------------------------

    def _build_management_page(self) -> None:
        # Every page is a CTkScrollableFrame, not a plain CTkFrame -- the design
        # spec's §4: "the whole page scrolls as one unit... no page puts its own
        # overflow/fixed-height scroll box inside an individual cell." The view
        # class itself (ManagementView, NetworksView/TargetPicker, etc.) owns no
        # scrolling of its own; this page-level wrapper is the only one.
        page = ctk.CTkScrollableFrame(self._content, fg_color="transparent")
        self._pages["management"] = page
        self.management_view = ManagementView(page, self)
        self.management_view.pack(fill="both", expand=True)

    # -- Discovery & Targets page ----------------------------------------------

    def _build_discovery_targets_page(self) -> None:
        page = ctk.CTkScrollableFrame(self._content, fg_color="transparent")   # §4: page-level
        # scroll, not a per-cell one -- see _build_management_page's own comment.
        self._pages["discovery"] = page
        # _discovery_handle is None until the first scan starts: that is the Idle
        # state (launch, or every start attempt so far failed). _discovery_paused is
        # True only once a scan has ACTUALLY stopped; _discovery_stopping covers the
        # Pause-click -> DiscoveryStopped gap (ADR-0010). Both stay False in Idle.
        self._discovery_handle = None
        self._discovery_paused = False
        self._discovery_stopping = False
        # Read once, here: an adapter's bands don't change while it stays bound,
        # and the dropdown below only offers choices built from them (ADR-0013).
        # May come back empty if no adapter is selected yet at all (ADR-0017) --
        # on_adapter_selected()/_refresh_band_menu() rebuild this later.
        supported = self._read_supported_bands()
        self._band_choices = self._compute_band_choices(supported)
        button_row = ctk.CTkFrame(page, fg_color="transparent")
        button_row.pack(side="top", anchor="w", padx=10, pady=(10, 0))
        self._pause_resume_button = ctk.CTkButton(
            button_row, text="Start Discovery", state="disabled",
            command=self._on_pause_resume_discovery_clicked,
        )  # disabled until a real band is picked -- the menu below reads the
        # placeholder at construction, so there's nothing to start yet (ADR-0018).
        self._pause_resume_button.pack(side="left")
        self._new_session_button = ctk.CTkButton(
            button_row, text="New Session", state="disabled", command=self._on_new_session_clicked
        )
        self._new_session_button.pack(side="left", padx=(10, 0))
        ctk.CTkLabel(button_row, text="Band:").pack(side="left", padx=(10, 0))
        self._band_menu = ctk.CTkOptionMenu(
            button_row,
            values=[_BAND_UNCHOSEN, *list(self._band_choices)],
            # Enabled whenever there's at least one real choice -- even a single-
            # choice adapter still needs one explicit click off the placeholder to
            # satisfy the Start/New Session gate below (ADR-0018 point 5, amending
            # ADR-0013's old "permanently disabled below two choices" rule). Zero
            # choices (no adapter selected at all yet, ADR-0017) keeps it disabled.
            # Otherwise enabled only while no scan is running (Idle or Paused) --
            # _start_discovery and _on_discovery_stopped flip it.
            state="normal" if len(self._band_choices) >= 1 else "disabled",
            command=self._on_band_menu_changed,
        )
        self._band_menu.set(_BAND_UNCHOSEN)   # ADR-0018: nothing pre-selected -- the
        # operator must look at and choose a band before the first scan, rather
        # than silently inheriting ADR-0013's old most-inclusive default.
        self._band_menu.pack(side="left", padx=(10, 0))
        self.networks_view = NetworksView(page, self)
        self.networks_view.pack(side="top", fill="both", expand=True, padx=10, pady=(5, 5))
        self.target_picker = TargetPicker(page, self)
        self.target_picker.pack(side="top", fill="both", expand=True, padx=10, pady=(5, 10))
        self.pump.on(NetworkDiscovered, self.networks_view.upsert_row)
        self.pump.on(NetworkSightingUpdated, self.networks_view.upsert_row)
        self.pump.on(TargetAdded, self.target_picker.upsert_row)
        self.pump.on(TargetRemoved, self.target_picker.remove_row)

    @staticmethod
    def _compute_band_choices(supported: frozenset[Band]) -> dict[str, frozenset[Band]]:
        """Factored out of _build_discovery_targets_page so
        on_adapter_selected()'s _refresh_band_menu() below can recompute the
        same label -> frozenset[Band] mapping after a runtime adapter switch
        (ADR-0017) without the two call sites drifting apart."""
        choices: dict[str, frozenset[Band]] = {}
        for choice in (
            frozenset({Band.GHZ_2_4}), frozenset({Band.GHZ_5}), frozenset({Band.GHZ_2_4, Band.GHZ_5}),
        ):
            if choice <= supported:
                # Labels are built from Band.value: "2.4 GHz", "5 GHz", and, for both,
                # "2.4 + 5 GHz" (the shared " GHz" unit is written once).
                members = [b.value.removesuffix(" GHz") for b in Band if b in choice]
                choices[" + ".join(members) + " GHz"] = choice
        return choices

    def _refresh_band_menu(self) -> None:
        """ADR-0017: re-reads supported_bands() for whichever adapter is now
        bound and rebuilds the Discovery band dropdown from scratch, resetting
        it to ADR-0018's unchosen placeholder -- a different adapter can
        support different bands, so the operator must re-pick rather than
        inheriting whatever the old adapter's dropdown last showed."""
        supported = self._read_supported_bands()
        self._band_choices = self._compute_band_choices(supported)
        self._band_menu.configure(values=[_BAND_UNCHOSEN, *list(self._band_choices)])
        self._band_menu.set(_BAND_UNCHOSEN)
        self._band_menu.configure(state="normal" if len(self._band_choices) >= 1 else "disabled")
        self._update_discovery_gating()

    def _read_supported_bands(self) -> frozenset[Band]:
        try:
            return self.engine.discovery.supported_bands()
        except NoAdapterSelected:
            # Nothing selected at all yet (ADR-0017) -- not a query failure,
            # so no error to show; the Management page is where this gets
            # resolved, and the Discovery tab's own empty band_choices
            # already communicates "nothing to choose yet" via its disabled
            # dropdown and disabled Start button.
            return frozenset()
        except BandUnavailable as e:
            # Fall back to 2.4 GHz rather than offering a band we can't confirm:
            # core's own start() would refuse any other band anyway (ADR-0013).
            self.status_bar.show_error(
                f"Couldn't read this adapter's bands ({e}); offering 2.4 GHz only"
            )
            return frozenset({Band.GHZ_2_4})

    def _on_band_menu_changed(self, _value: str) -> None:
        """Wired as the band menu's command= (ADR-0018 point 3). CTkOptionMenu
        only calls command= from its own dropdown-click callback -- never from
        a programmatic .set() (confirmed against this environment's real
        customtkinter) -- so this genuinely means "the operator just chose
        something," not just "the menu's value changed for any reason."
        Delegates to _update_discovery_gating() rather than deciding the
        buttons' state here directly, so this check stays combined with
        Discovery's own run-state through the one shared code path
        _start_discovery and _on_discovery_stopped also use below."""
        self._update_discovery_gating()

    def _update_discovery_gating(self) -> None:
        """Single source of truth for Start/Pause-Resume's and New Session's
        enabled state, recomputed from the band menu's current value plus
        Discovery's own run-state (ADR-0018 point 3) -- called from
        _on_band_menu_changed, _start_discovery, _on_discovery_stopped, and
        _refresh_band_menu (ADR-0017), instead of separate ad-hoc state-
        setting call sites that could drift out of sync.

        Start/Resume is gated on a real band being chosen only while Idle or
        Paused -- the Pause action itself (same button, mid-scan) needs no
        band permission, and reaching a running scan already required a real
        pick to start it. The placeholder is never re-selected
        programmatically during a run except by _refresh_band_menu (a fresh
        adapter switch resets the gate deliberately -- ADR-0017 pt 17), so a
        picked band otherwise stays chosen for the rest of the session
        (ADR-0018 point 3's "one-time gate in practice"). New Session is
        gated on BOTH being Paused AND a real band being chosen -- in
        practice that combination can't arise through real GUI use (Paused
        is only reachable via a prior successful Start/Resume, which itself
        required a real pick), but it's expressed here as an explicit AND
        rather than relied upon."""
        band_chosen = self._band_menu.get() != _BAND_UNCHOSEN
        if self._discovery_stopping:
            pause_resume_enabled = False   # Pausing: unchanged by ADR-0018
        elif self._discovery_handle is not None and not self._discovery_paused:
            pause_resume_enabled = True    # Scanning: Pause needs no band permission
        else:
            pause_resume_enabled = band_chosen   # Idle or Paused: this click starts/resumes
        new_session_enabled = self._discovery_paused and band_chosen

        self._pause_resume_button.configure(state="normal" if pause_resume_enabled else "disabled")
        self._new_session_button.configure(state="normal" if new_session_enabled else "disabled")

    def _on_pause_resume_discovery_clicked(self) -> None:
        if self._discovery_stopping:
            return   # defensive: the button is already disabled while Pausing
        elif self._discovery_handle is None or self._discovery_paused:
            # Idle (first Start) or Paused (Resume): same start path, table kept.
            self._start_discovery(new_session=False)
        else:
            # Cancel is async (up to one driver tick, plus a privileged kill),
            # and the adapter is only released once the driver finishes -- so
            # this click can't flip to Resume yet, or a fast Resume click would
            # hit AdapterBusy. _on_discovery_stopped finishes the transition.
            self._discovery_stopping = True
            self._pause_resume_button.configure(text="Pausing…", state="disabled")
            self._discovery_handle.cancel()

    def _on_new_session_clicked(self) -> None:
        if not self._discovery_paused:
            return   # defensive: the button is only enabled while paused
        self._start_discovery(new_session=True)

    def _start_discovery(self, *, new_session: bool) -> None:
        """Shared by the first Start (from Idle), Resume (keeps the table) and
        New Session (clears it) -- ADR-0010. Scans whichever Band the dropdown
        shows; the dropdown is only enabled while no scan is running, so each
        start can pick a different one. The lookup below can never hit the
        placeholder: Start/Resume/New Session are all disabled until a real
        band is chosen (ADR-0018 point 3), so by the time any of them can be
        clicked, self._band_menu.get() is already a real key in
        self._band_choices."""
        options = DiscoveryOptions(bands=self._band_choices[self._band_menu.get()])
        try:
            handle = self.engine.discovery.start(options)
        except (RadioCommandFailed, AdapterBusy, BandUnavailable, NoAdapterSelected) as e:
            if new_session:
                what = "start a new session"
            elif self._discovery_handle is None:
                what = "start"
            else:
                what = "resume"
            self.status_bar.show_error(f"Discovery couldn't {what}: {e}")
            return  # stay Idle/paused; table, buttons and band menu untouched
        self._discovery_handle = handle
        self.pump.on(DiscoveryStopped, self._on_discovery_stopped, only_job=handle.job_id)
        if new_session:
            self.networks_view.clear()   # only once start() succeeded, so a failed
            # start leaves the old rows in place
        self._discovery_paused = False
        self._pause_resume_button.configure(text="Pause Discovery")
        self._update_discovery_gating()   # Scanning: Pause enabled, New Session disabled (ADR-0018)
        self._band_menu.configure(state="disabled")   # no Band change mid-scan

    def _on_discovery_stopped(self, event: DiscoveryStopped) -> None:
        if event.reason == StopReason.ERROR:
            message = "Discovery stopped unexpectedly — check your adapter/sudo session, then Resume"
            if event.error_detail is not None:   # best-effort hint from airodump-ng's own
                message += f" ({event.error_detail})"   # stderr -- see events.py's own docstring
            self.status_bar.show_error(message)
        elif not self._discovery_stopping:
            return  # CANCELLED not from the Pause button (e.g. Engine.shutdown() during
            # close) -- nothing to do.
        # The buttons wait for this event rather than the click because the driver
        # releases the adapter before publishing DiscoveryStopped, so Resume/New
        # Session are safe to start from here (ADR-0010).
        self._discovery_stopping = False
        self._discovery_paused = True
        self._pause_resume_button.configure(text="Resume Discovery")
        self._update_discovery_gating()   # Paused: Resume/New Session both gated on a real pick (ADR-0018)
        if len(self._band_choices) >= 1:   # a single real choice is still pickable (ADR-0018 pt 5)
            self._band_menu.configure(state="normal")

    # -- Capture & Attack page -------------------------------------------------

    def _build_capture_attack_page(self) -> None:
        page = ctk.CTkScrollableFrame(self._content, fg_color="transparent")   # §4
        self._pages["capture"] = page
        self.target_actions_view = TargetActionsView(page, self)
        self.target_actions_view.pack(fill="both", expand=True, padx=10, pady=10)

    # -- Cracking page ----------------------------------------------------------

    def _build_cracking_page(self) -> None:
        page = ctk.CTkScrollableFrame(self._content, fg_color="transparent")   # §4
        self._pages["cracking"] = page
        self.handshake_picker = HandshakePicker(page, self, on_selected=self._on_handshake_selected)
        self.handshake_picker.pack(side="top", fill="both", expand=True, pady=(0, 10))
        self.wordlist_picker = WordlistPicker(page, on_selected=self._on_wordlist_selected)
        self.wordlist_picker.pack(side="top", fill="x", pady=(0, 10))
        self.crack_panel = CrackPanel(page, self)
        self.crack_panel.pack(side="top", fill="both", expand=True)
        self.pump.on(HandshakeCaptured, self.handshake_picker.append)

    def _on_handshake_selected(self, handshake) -> None:
        self.crack_panel.set_handshake(handshake)

    def _on_wordlist_selected(self, path) -> None:
        self.crack_panel.set_wordlist(path)

    # -- Logs page ----------------------------------------------------------------

    def _build_logs_page(self) -> None:
        page = ctk.CTkScrollableFrame(self._content, fg_color="transparent")   # §4
        self._pages["logs"] = page
        self.audit_log_view = AuditLogView(page, self, self._interrupted_deauth_targets)
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
