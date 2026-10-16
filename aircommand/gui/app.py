"""App: the QMainWindow shell for the PySide6 port. Owns the single Engine
instance and is the one wiring site.

See docs/design/gui-structure.md "Startup sequencing (App.__init__)" for the
pinned sequencing, docs/design/gui-redesign-gridwatch.md for the Gridwatch x
Wireframe A shell (collapsible sidebar nav plus five pages), and
docs/design/gui-port/05-boundary-design.md "View mapping", "Launch order", and
"Shutdown" for the Qt shape.

The shell, sidebar, status bar, and sudo dialog landed in F3; P1 to P5 then gave
every page in the QStackedWidget its real view. Each page builder composes that
page's view and wires the page's own engine calls and pump subscriptions (the
Discovery band state machine, the global HandshakeCaptured feed to the cracking
picker, the global DeauthFired feed to the audit log). Shell-level wiring also
lives here: the sudo prompt and retry, startup reconciliation, navigation, the
keepalive status-bar subscriptions, and shutdown.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

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
from aircommand.gui.dialog import Dialog, outline_button, primary_button
from aircommand.gui.discovery_view import NetworksView, TargetPicker
from aircommand.gui.event_pump import GuiEventPump
from aircommand.gui.management_view import ManagementView
from aircommand.gui.sidebar import Sidebar
from aircommand.gui.status_bar import StatusBar
from aircommand.gui.sudo_dialog import SudoPasswordDialog
from aircommand.gui.target_actions_view import TargetActionsView

# ADR-0018: sentinel label for the band menu's "nothing picked yet" placeholder.
# Deliberately never a key in self._band_choices, so a future bug that forgets
# the Start/Resume/New Session gate fails loudly (KeyError) rather than silently
# passing bands=None into DiscoveryOptions. Defined once so the same literal
# backs the menu's initial value and the gate check (ADR-0018 "Considered
# Options", point 3).
_BAND_UNCHOSEN = "Choose a band..."

# ADR-0017 plus the Gridwatch redesign: Management is the landing page, the
# page the console opens on right after the sudo prompt.
_LANDING_PAGE = "management"


class App(QMainWindow):
    def __init__(
        self, db_path: Path, work_dir: Path, adapter: Optional[str] = None, proc: Optional[ProcRunner] = None
    ) -> None:
        super().__init__()
        self.setWindowTitle("AirCommand")
        # Open at a usable size, sized to the widest page, and keep the sidebar
        # and narrower pages usable with a minimum. Past the minimum the window
        # resizes freely and each page scrolls as one unit.
        self.resize(1200, 720)
        self.setMinimumSize(1040, 640)
        self._closing = False   # guards on_close() against a re-entrant close

        self.engine = Engine(db_path=db_path, work_dir=work_dir, adapter=adapter, proc=proc)

        password = self._ask_sudo_password_dialog()
        if password is None:      # cancelled/closed on the very first prompt: privilege
            raise SystemExit(0)   # is required before ANY Action, so there is no degraded mode
        while True:
            try:
                self.engine.privilege.start(password)
                break
            except InvalidSudoPasswordError:
                password = self._ask_sudo_password_dialog(error="Incorrect password, try again")
                if password is None:      # cancelled/closed on a retry: same no-degraded-mode rule
                    raise SystemExit(0)

        reconciliation = self.engine.reconcile_startup()   # sync return; nothing is on the pump yet
        targets_by_id = {t.id: t for t in self.engine.targets.list()}
        self._interrupted_deauth_targets = [
            targets_by_id[tid]
            for tid in reconciliation.interrupted_deauth_target_ids
            if tid in targets_by_id
        ]  # a tid may be absent if the Target was removed between the crash and this launch

        self.pump = GuiEventPump(self.engine, self)

        # Shell: a QHBoxLayout of the sidebar and a right column. The right
        # column is a QVBoxLayout of the page stack and the status bar, so the
        # bar sits inside the right column and does not span the sidebar (05
        # "Status bar placement").
        central = QWidget()
        central.setObjectName("page")
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        self.sidebar = Sidebar(on_navigate=self._on_navigate)
        shell.addWidget(self.sidebar)

        self._right_column = QWidget()
        right = QVBoxLayout(self._right_column)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        self._stack = QStackedWidget()
        right.addWidget(self._stack, 1)

        self.status_bar = StatusBar(
            self._right_column,
            self.engine.privilege.status,
            reconciliation,
            self._interrupted_deauth_targets,
            self.engine.radio.selected_adapter,
        )
        right.addWidget(self.status_bar)
        shell.addWidget(self._right_column, 1)
        self.setCentralWidget(central)

        self.pump.on(SudoKeepaliveFailed, self.status_bar.show_sudo_warning)
        self.pump.on(SudoKeepaliveRecovered, self.status_bar.clear_sudo_warning)

        self._pages: dict[str, QWidget] = {}
        self._build_management_page()
        self._build_discovery_targets_page()
        self._build_capture_attack_page()
        self._build_cracking_page()
        self._build_logs_page()
        self._on_navigate(_LANDING_PAGE)

        self.show()   # privilege is up and the UI is built; show the window last (05 "Launch order")

    def _ask_sudo_password_dialog(self, error: Optional[str] = None) -> Optional[str]:
        dialog = SudoPasswordDialog(error=error)
        dialog.exec()
        return dialog.result

    # -- Sidebar navigation ---------------------------------------------------

    def _on_navigate(self, key: str) -> None:
        self._show_page(key)
        self.sidebar.set_active(key)

    def _show_page(self, key: str) -> None:
        if key not in self._pages:
            raise ValueError(f"unknown page: {key!r}")
        self._stack.setCurrentWidget(self._pages[key])
        if key == "management":
            # Populate the page the operator just opened, exactly as the Tk
            # version did: no bus event for this, the page polls the radio
            # (ADR-0017). For the landing page this runs during __init__.
            self.management_view.refresh()

    # -- Adapter selection (ADR-0017) -----------------------------------------

    def on_adapter_selected(self) -> None:
        """Called by ManagementView right after a successful
        RadioController.select_adapter(). RadioController has no EventBus
        (ADR-0017), so this plain method call fans the new selection out to the
        pages that care: the Discovery band menu, the status bar's adapter
        label, and a paused Discovery session."""
        self._refresh_band_menu()
        self.status_bar.show_adapter(self.engine.radio.selected_adapter)
        if self._discovery_paused:
            # A Discovery session is scoped to one physical adapter (ADR-0010).
            # Switching adapters while Paused does not carry the old session over
            # (ADR-0017 pt 17). Switching while Scanning/Pausing cannot reach this
            # point: select_adapter() raises AdapterBusy while the radio is
            # reserved, so ManagementView never calls this method then.
            self.networks_view.clear()
            self._discovery_paused = False
            self._discovery_handle = None
            self._pause_resume_button.setText("Start Discovery")

    # -- Page builders ---------------------------------------------------------
    # Each P-slice replaces its placeholder with the real view and that page's
    # own engine wiring. The pages still on a placeholder only need a distinct
    # widget per key so navigation can switch between them.

    def _scroll_page(self, content: QWidget) -> QScrollArea:
        # Every page scrolls as one unit (the Tk CTkScrollableFrame, Gridwatch
        # section 4: no per-cell scroll box). The simplest Qt equivalent is a
        # frameless QScrollArea that resizes its one child to the viewport.
        # P2 to P5 reuse this for their pages.
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        # The scroll viewport and content widget otherwise keep the default light
        # palette, not the QSS, so the dark page background does not paint behind
        # the cells. The QSS already has `QWidget#page { background: bg }`, so give
        # both the "page" objectName; a plain QWidget subclass also needs
        # WA_StyledBackground for a QSS background to paint.
        content.setObjectName("page")
        content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        area.viewport().setObjectName("page")
        area.setWidget(content)
        return area

    def _build_management_page(self) -> None:
        self.management_view = ManagementView(self)
        page = self._scroll_page(self.management_view)
        self._pages["management"] = page
        self._stack.addWidget(page)

    def _build_discovery_targets_page(self) -> None:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        title = QLabel("Discovery & Targets")   # a QLabel with no buddy shows "&" literally
        title.setProperty("class", "pageTitle")
        self._discovery_title = title
        layout.addWidget(title)

        # _discovery_handle is None until the first scan starts: that is the Idle
        # state. _discovery_paused is True only once a scan has actually stopped;
        # _discovery_stopping covers the Pause-click to DiscoveryStopped gap
        # (ADR-0010). Both stay False in Idle.
        self._discovery_handle = None
        self._discovery_paused = False
        self._discovery_stopping = False
        # Read once, here: an adapter's bands do not change while it stays bound,
        # and the dropdown only offers choices built from them (ADR-0013). May
        # come back empty if no adapter is selected yet (ADR-0017);
        # on_adapter_selected()/_refresh_band_menu() rebuild it later.
        supported = self._read_supported_bands()
        self._band_choices = self._compute_band_choices(supported)

        controls = QWidget()
        control_row = QHBoxLayout(controls)
        control_row.setContentsMargins(0, 0, 0, 0)
        control_row.setSpacing(8)
        # Primary accent-fill Start/Pause/Resume, disabled until a real band is
        # picked (the menu reads the placeholder at construction, so there is
        # nothing to start yet, ADR-0018). The QSS :disabled rule dims it, so
        # gating is setEnabled() with no hand recoloring.
        self._pause_resume_button = primary_button(
            "Start Discovery", self._on_pause_resume_discovery_clicked
        )
        self._pause_resume_button.setEnabled(False)
        self._new_session_button = outline_button("New Session", self._on_new_session_clicked)
        self._new_session_button.setEnabled(False)
        band_label = QLabel("Band:")
        band_label.setProperty("class", "fieldLabel")
        self._band_menu = QComboBox()
        self._band_menu.addItems([_BAND_UNCHOSEN, *self._band_choices])
        self._band_menu.setCurrentText(_BAND_UNCHOSEN)   # ADR-0018: nothing pre-selected
        # Enabled whenever there is at least one real choice, even a single-choice
        # adapter still needs one explicit pick off the placeholder (ADR-0018 pt 5).
        # Zero choices (no adapter selected yet) keeps it disabled.
        self._band_menu.setEnabled(len(self._band_choices) >= 1)
        # textActivated fires only on a user pick from the dropdown, not a
        # programmatic setCurrentText(), reproducing CTkOptionMenu's command=.
        self._band_menu.textActivated.connect(self._on_band_menu_changed)
        control_row.addWidget(self._pause_resume_button)
        control_row.addWidget(self._new_session_button)
        control_row.addWidget(band_label)
        control_row.addWidget(self._band_menu)
        control_row.addStretch(1)
        layout.addWidget(controls)

        self.networks_view = NetworksView(self)
        self.target_picker = TargetPicker(self)
        layout.addWidget(self.networks_view, 1)
        layout.addWidget(self.target_picker, 1)

        page = self._scroll_page(content)
        self._pages["discovery"] = page
        self._stack.addWidget(page)

        self.pump.on(NetworkDiscovered, self.networks_view.upsert_row)
        self.pump.on(NetworkSightingUpdated, self.networks_view.upsert_row)
        self.pump.on(TargetAdded, self.target_picker.upsert_row)
        self.pump.on(TargetRemoved, self.target_picker.remove_row)

    # -- Discovery band state machine (ADR-0013, ADR-0018) --------------------

    @staticmethod
    def _compute_band_choices(supported: "frozenset[Band]") -> "dict[str, frozenset[Band]]":
        """Label -> frozenset[Band] mapping for the band dropdown, built from the
        bands the adapter supports. Factored out so _refresh_band_menu() can
        recompute the same mapping after a runtime adapter switch (ADR-0017)
        without the two call sites drifting apart."""
        choices: dict[str, frozenset[Band]] = {}
        for choice in (
            frozenset({Band.GHZ_2_4}), frozenset({Band.GHZ_5}), frozenset({Band.GHZ_2_4, Band.GHZ_5}),
        ):
            if choice <= supported:
                members = [b.value.removesuffix(" GHz") for b in Band if b in choice]
                choices[" + ".join(members) + " GHz"] = choice
        return choices

    def _refresh_band_menu(self) -> None:
        """ADR-0017: re-read supported_bands() for whichever adapter is now bound
        and rebuild the dropdown from scratch, resetting it to ADR-0018's
        placeholder. A different adapter can support different bands, so the
        operator must re-pick rather than inherit the old adapter's last choice."""
        supported = self._read_supported_bands()
        self._band_choices = self._compute_band_choices(supported)
        self._band_menu.clear()
        self._band_menu.addItems([_BAND_UNCHOSEN, *self._band_choices])
        self._band_menu.setCurrentText(_BAND_UNCHOSEN)
        self._band_menu.setEnabled(len(self._band_choices) >= 1)
        self._update_discovery_gating()

    def _read_supported_bands(self) -> "frozenset[Band]":
        try:
            return self.engine.discovery.supported_bands()
        except NoAdapterSelected:
            # Nothing selected yet (ADR-0017), not a query failure: no error to
            # show, and the disabled dropdown and Start button already say
            # "nothing to choose yet".
            return frozenset()
        except BandUnavailable as e:
            # Fall back to 2.4 GHz rather than offer a band we cannot confirm;
            # core's own start() would refuse any other band anyway (ADR-0013).
            self.status_bar.show_error(
                f"Couldn't read this adapter's bands ({e}); offering 2.4 GHz only"
            )
            return frozenset({Band.GHZ_2_4})

    def _on_band_menu_changed(self, _value: str) -> None:
        """The band menu's textActivated handler (ADR-0018 point 3): the operator
        just chose a band. Delegates to _update_discovery_gating() so the band
        check stays combined with Discovery's run-state through the one shared
        code path _start_discovery and _on_discovery_stopped also use."""
        self._update_discovery_gating()

    def _update_discovery_gating(self) -> None:
        """Single source of truth for Start/Pause-Resume and New Session enabled
        state, recomputed from the band menu's current value plus Discovery's own
        run-state (ADR-0018 point 3). Start/Resume is gated on a real band only
        while Idle or Paused; the Pause action itself needs no band permission,
        and reaching a running scan already required a real pick. New Session is
        the AND of Paused and a real band being chosen. Disabled controls dim via
        the QSS :disabled rule, so this is just setEnabled()."""
        band_chosen = self._band_menu.currentText() != _BAND_UNCHOSEN
        if self._discovery_stopping:
            pause_resume_enabled = False   # Pausing
        elif self._discovery_handle is not None and not self._discovery_paused:
            pause_resume_enabled = True    # Scanning: Pause needs no band permission
        else:
            pause_resume_enabled = band_chosen   # Idle or Paused: this click starts/resumes
        self._pause_resume_button.setEnabled(pause_resume_enabled)
        self._new_session_button.setEnabled(self._discovery_paused and band_chosen)

    def _on_pause_resume_discovery_clicked(self) -> None:
        if self._discovery_stopping:
            return   # defensive: the button is already disabled while Pausing
        elif self._discovery_handle is None or self._discovery_paused:
            # Idle (first Start) or Paused (Resume): same start path, table kept.
            self._start_discovery(new_session=False)
        else:
            # Cancel is async (up to one driver tick, plus a privileged kill), and
            # the adapter is only released once the driver finishes, so this click
            # cannot flip to Resume yet. _on_discovery_stopped finishes the
            # transition. The button disables directly here, bypassing the gating
            # helper, so the QSS :disabled dim applies while it reads "Pausing...".
            self._discovery_stopping = True
            self._pause_resume_button.setText("Pausing...")
            self._pause_resume_button.setEnabled(False)
            self._discovery_handle.cancel()

    def _on_new_session_clicked(self) -> None:
        if not self._discovery_paused:
            return   # defensive: the button is only enabled while paused
        self._start_discovery(new_session=True)

    def _start_discovery(self, *, new_session: bool) -> None:
        """Shared by the first Start (from Idle), Resume (keeps the table) and New
        Session (clears it), ADR-0010. Scans whichever Band the dropdown shows;
        the dropdown is only enabled while no scan runs, so each start can pick a
        different one. The lookup never hits the placeholder: Start/Resume/New
        Session are all disabled until a real band is chosen (ADR-0018 point 3),
        so by the time any of them can be clicked the menu is on a real key."""
        options = DiscoveryOptions(bands=self._band_choices[self._band_menu.currentText()])
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
        self._pause_resume_button.setText("Pause Discovery")
        self._update_discovery_gating()   # Scanning: Pause enabled, New Session disabled
        self._band_menu.setEnabled(False)   # no Band change mid-scan

    def _on_discovery_stopped(self, event: DiscoveryStopped) -> None:
        if event.reason == StopReason.ERROR:
            message = "Discovery stopped unexpectedly, check your adapter/sudo session, then Resume"
            if event.error_detail is not None:   # best-effort hint from airodump-ng's stderr
                message += f" ({event.error_detail})"
            self.status_bar.show_error(message)
        elif not self._discovery_stopping:
            return  # CANCELLED not from the Pause button (e.g. Engine.shutdown()): nothing to do
        # The buttons wait for this event, not the click: the driver releases the
        # adapter before publishing DiscoveryStopped, so Resume/New Session are
        # safe to start from here (ADR-0010).
        self._discovery_stopping = False
        self._discovery_paused = True
        self._pause_resume_button.setText("Resume Discovery")
        self._update_discovery_gating()   # Paused: Resume/New Session both gated on a real pick
        if len(self._band_choices) >= 1:   # a single real choice is still pickable (ADR-0018 pt 5)
            self._band_menu.setEnabled(True)

    def _build_capture_attack_page(self) -> None:
        # TargetActionsView owns this page's wiring: CapturePanel registers
        # HandshakeCaptured once globally (manual capture_job_id match) in its
        # own __init__, and TargetActionsView registers TargetAdded/TargetRemoved
        # for its selector. CaptureStopped, the local DeauthFired burst counter,
        # NmapScanCompleted, and EnumerationFailed are per-Start only_job
        # registrations inside the panels (02 CapturePanel/EnumeratePanel rows).
        self.target_actions_view = TargetActionsView(self)
        page = self._scroll_page(self.target_actions_view)
        self._pages["capture"] = page
        self._stack.addWidget(page)

    def _build_cracking_page(self) -> None:
        # Composes the page's three widgets and owns the one global
        # HandshakeCaptured registration (05 "The bridge"): the event carries no
        # job_id, so only_job cannot filter it, and the picker wants every
        # captured handshake with no job filter. This is a separate handler from
        # CapturePanel's own global HandshakeCaptured registration; both firing
        # per event is correct (02 HandshakePicker and CapturePanel rows).
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        title = QLabel("Cracking")
        title.setProperty("class", "pageTitle")
        layout.addWidget(title)

        self.handshake_picker = HandshakePicker(self, on_selected=self._on_handshake_selected)
        self.wordlist_picker = WordlistPicker(on_selected=self._on_wordlist_selected)
        self.crack_panel = CrackPanel(self)
        layout.addWidget(self.handshake_picker)
        layout.addWidget(self.wordlist_picker)
        layout.addWidget(self.crack_panel)
        layout.addStretch(1)

        page = self._scroll_page(content)
        self._pages["cracking"] = page
        self._stack.addWidget(page)

        self.pump.on(HandshakeCaptured, self.handshake_picker.append)

    def _on_handshake_selected(self, handshake) -> None:
        self.crack_panel.set_handshake(handshake)

    def _on_wordlist_selected(self, path) -> None:
        self.crack_panel.set_wordlist(path)

    def _build_logs_page(self) -> None:
        # Composes AuditLogView and owns the one global DeauthFired registration
        # (05 "The bridge"): every firing is logged across every job, with no
        # job filter. This is a separate handler from CapturePanel's own
        # per-Start DeauthFired burst-counter registration; both firing per event
        # is correct (02 AuditLogView and CapturePanel rows). The embedded
        # TargetSelector is now kept live too: its TargetAdded/TargetRemoved
        # wiring sits here, beside the DeauthFired one, since AuditLogView stays
        # agnostic of the pump (unlike TargetActionsView, which wires its own
        # selector).
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        title = QLabel("Logs")
        title.setProperty("class", "pageTitle")
        layout.addWidget(title)

        self.audit_log_view = AuditLogView(self, self._interrupted_deauth_targets)
        layout.addWidget(self.audit_log_view)
        layout.addStretch(1)

        page = self._scroll_page(content)
        self._pages["logs"] = page
        self._stack.addWidget(page)

        self.pump.on(DeauthFired, self.audit_log_view.append)
        self.pump.on(TargetAdded, self.audit_log_view.target_selector.upsert_row)
        self.pump.on(TargetRemoved, self.audit_log_view.target_selector.remove_row)

    # -- Shutdown --------------------------------------------------------------

    def on_close(self) -> None:
        # Order from 05 "Shutdown": stop the pump first so a queued handler
        # cannot run against a widget about to be destroyed, show the closing
        # dialog, processEvents() so it paints before the blocking shutdown
        # (the replacement for Tk's update_idletasks), then shutdown, then close.
        if self._closing:
            return
        self._closing = True

        self.pump.stop()
        dialog = _ClosingDialog(self)
        dialog.show()
        QApplication.processEvents()   # paint the dialog before shutdown() blocks this thread

        self.engine.shutdown()
        dialog.close()
        self.close()

    def closeEvent(self, event) -> None:
        # The window close button routes to the same teardown. The _closing
        # guard makes the self.close() inside on_close() a plain accept.
        if not self._closing:
            self.on_close()
        event.accept()


class _ClosingDialog(Dialog):
    """Modal, no buttons: shutdown cannot be cancelled, and this dialog is only
    up for the shutdown's sub-second-to-single-digit-seconds duration. It just
    tells the operator closing is in progress, the same job the Tk version did."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Closing AirCommand", parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 20, 30, 20)
        label = QLabel("Closing AirCommand, please wait")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
