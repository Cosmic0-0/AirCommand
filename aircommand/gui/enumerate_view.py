"""EnumeratePanel — start an nmap Enumerate scan against the Target Actions
tab's currently selected Target. See docs/design/gui-structure.md 'Target
Actions tab' and docs/design/gui-redesign-gridwatch.md §5/§6 item 5: Enumerate
is visually and structurally its own Cell, separate from Capture/Deauth.

No Cancel button here (deliberate, not an oversight): enumerate.py's own
_drive docstring already notes cancellation isn't meaningfully checkable
mid-scan (nmap's -oX - output is buffered to completion, not iterated line by
line) -- unlike CapturePanel's Cancel button, this wasn't something the
project owner needed to weigh in on.

Column widths/alignment go through table.py's build_header()/add_row() --
see that module's docstring (ThingsToChange item 1).

Radio-state guard: Start Enumerate is disabled, with its own hint, while the
adapter is in monitor mode or no adapter is selected (_radio_blocker below).
Enumerate needs the adapter associated to the Target's network through the
OS (enumerate.py's module docstring, Option A), and a monitor-mode adapter
can't be. Without the guard, clicking Start silently ran airmon-ng stop plus a
NetworkManager restart (rf.py's _ensure_mode) the operator never asked for,
and nothing then rejoined the Target's network for them, so the scan almost
always failed anyway. Kept current by a self-rescheduling poll, not bus
events: nothing on the bus marks a monitor-mode transition. DiscoveryStopped/
CaptureStopped fire when a job ends, but RadioController.release()
deliberately leaves the radio in monitor mode, and the Management page's
manual Start/Stop Airmon-ng buttons publish no event at all (ADR-0017).
"""

from __future__ import annotations

from typing import Optional

import customtkinter as ctk

from aircommand.core import AdapterBusy, JobHandle, NoAdapterSelected, RadioCommandFailed, Target
from aircommand.core.domain import EnumHost
from aircommand.core.events import EnumerationFailed, NmapScanCompleted
from aircommand.gui.cell import Cell
from aircommand.gui.table import Column, add_row, build_header
from aircommand.gui.theme import CORNER_RADIUS, PALETTE, ui_font


class EnumeratePanel(ctk.CTkFrame):
    _HINT_TEXT = (
        "Requires this adapter already associated to the Target's network via "
        "your OS's normal wifi settings — AirCommand doesn't join networks itself."
    )
    _MONITOR_MODE_HINT_TEXT = (
        "Unavailable: the adapter is in monitor mode. Stop it with Stop Airmon-ng on "
        "the Management page, then wait for your OS to rejoin the Target's network "
        "before starting Enumerate."
    )
    _NO_ADAPTER_HINT_TEXT = "Unavailable: no adapter selected. Pick one on the Management page first."
    # Pure in-memory property reads (_radio_blocker), no subprocess, so a short
    # interval costs nothing. Same self-rescheduling after() pattern as
    # ManagementView._tick_session_time.
    _RADIO_POLL_MS = 500
    _COLUMNS = (Column("IP", 120), Column("Hostname", 160), Column("Open Ports", 220))

    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color="transparent")
        self._app = app
        self.target: Optional[Target] = None
        self.active_handle: Optional[JobHandle] = None

        cell = Cell(self, "Enumerate")
        cell.pack(side="top", fill="both", expand=True)

        self._hint_label = ctk.CTkLabel(
            cell.body, text=self._HINT_TEXT, font=ui_font(11), text_color=PALETTE["muted"],
            wraplength=500, justify="left", anchor="w",
        )
        self._hint_label.pack(side="top", anchor="w", pady=(0, 8))

        self._start_button = ctk.CTkButton(
            cell.body, text="Start Enumerate", command=self.on_start_clicked,
            fg_color=PALETTE["accent"], hover_color=PALETTE["accent"], border_color=PALETTE["accent"],
            text_color=PALETTE["bg"], text_color_disabled=PALETTE["muted"],
            corner_radius=CORNER_RADIUS, font=ui_font(12, "bold"),
        )  # disabled-state styling: see management_view.py's Start button comment --
        # text_color_disabled + the fill-dim in _update_button_state() below keep the
        # disabled accent button legible and clearly inactive.
        self._start_button.pack(side="top", anchor="w", pady=(0, 8))

        self._status_label = ctk.CTkLabel(
            cell.body, text="", font=ui_font(11), text_color=PALETTE["muted"], anchor="w",
        )
        self._status_label.pack(side="top", anchor="w", pady=(0, 8))

        header = build_header(cell.body, self._COLUMNS)
        header.pack(side="top", fill="x")

        # Plain CTkFrame, not CTkScrollableFrame: design spec §6 item 3 -- no
        # page puts its own fixed-height scroll box inside an individual
        # cell any more. The page itself (a CTkScrollableFrame, one layer up
        # in app.py) already scrolls as a single unit; nesting a second
        # scrollable frame in here would reintroduce exactly what that rule
        # removed elsewhere (NetworksView/TargetPicker got the same fix).
        self._results_body = ctk.CTkFrame(cell.body, fg_color=PALETTE["bg"])
        self._results_body.pack(side="top", fill="both", expand=True)

        self._last_radio_blocker: Optional[str] = None
        self._update_button_state()
        self._poll_radio_state()

    def set_target(self, target: Optional[Target]) -> None:
        self.target = target
        self._clear_results()
        self._update_button_state()

    def _radio_blocker(self) -> Optional[str]:
        """The hint text for whichever radio condition currently rules out
        Enumerate, or None if the radio is usable. Reads RadioController's own
        in-memory state, the same property the Management page's Monitor mode
        stat reads. It does NOT spawn `iw dev <if> info` to catch ADR-0016's
        desync (a prior crashed session left the real adapter in monitor mode
        while this process still believes it's managed): a subprocess every
        _RADIO_POLL_MS is too expensive for a poll, and _ensure_mode's own
        ADR-0016 branch still corrects that one case when Start is clicked."""
        radio = self._app.engine.radio
        if radio.is_in_monitor_mode:
            return self._MONITOR_MODE_HINT_TEXT
        if radio.selected_adapter is None:
            return self._NO_ADAPTER_HINT_TEXT
        return None

    def _poll_radio_state(self) -> None:
        # Only reconfigures when the blocker actually changes, so the button
        # isn't redrawn every tick. Tk drops a destroyed widget's pending
        # after() callbacks, so this needs no explicit stop (same as
        # ManagementView._tick_session_time).
        if self._radio_blocker() != self._last_radio_blocker:
            self._update_button_state()
        self.after(self._RADIO_POLL_MS, self._poll_radio_state)

    def _update_button_state(self) -> None:
        blocker = self._radio_blocker()
        self._last_radio_blocker = blocker
        can_start = self.target is not None and self.active_handle is None and blocker is None
        # warn, not muted, while a radio blocker is active: it is something the
        # operator has to go and fix, unlike the standing join requirement in
        # _HINT_TEXT (same muted-vs-warn split as cell.py's Tag).
        self._hint_label.configure(
            text=blocker if blocker is not None else self._HINT_TEXT,
            text_color=PALETTE["warn"] if blocker is not None else PALETTE["muted"],
        )
        # Dim the fill/border when disabled so the muted text reads and the button
        # looks inactive -- CTkButton won't dim its own fill (see management_view.py).
        self._start_button.configure(
            state="normal" if can_start else "disabled",
            fg_color=PALETTE["accent"] if can_start else PALETTE["panel"],
            hover_color=PALETTE["accent"] if can_start else PALETTE["panel"],
            border_color=PALETTE["accent"] if can_start else PALETTE["border"],
        )

    def _clear_results(self) -> None:
        for child in self._results_body.winfo_children():
            child.destroy()

    def _populate_results(self, hosts: tuple[EnumHost, ...]) -> None:
        for row_index, host in enumerate(hosts):
            hostname = host.hostname if host.hostname is not None else "—"
            ports = ", ".join(str(p) for p in host.open_ports) if host.open_ports else "—"
            add_row(self._results_body, row_index, self._COLUMNS, (host.ip, hostname, ports))

    def on_start_clicked(self) -> None:
        try:
            handle = self._app.engine.enumerate.start_scan(self.target)
        except AdapterBusy as e:
            self._app.status_bar.show_error(f"Radio busy: {e.holder.value} is using the adapter")
            return
        except NoAdapterSelected:
            # Previously uncaught: it escaped into Tk's callback handler and the
            # operator saw nothing. _update_button_state's guard already keeps
            # Start disabled while nothing is selected; this is the backstop so
            # a call that gets here anyway is never a silent failure.
            self._app.status_bar.show_error("No adapter selected: pick one on the Management page first")
            return
        except RadioCommandFailed as e:
            # Enumerate reserves AdapterMode.MANAGED, so the only RadioCommandFailed
            # reserve() can raise here comes from _stop_monitor_mode's
            # `systemctl restart NetworkManager`, i.e. switching the adapter OUT of
            # monitor mode. The old text ("into monitor mode") was copied from
            # Capture's handler and described the opposite direction.
            self._app.status_bar.show_error(
                f"Couldn't switch the adapter out of monitor mode back to normal wifi: {e}"
            )
            return
        self.active_handle = handle
        self._clear_results()
        self._status_label.configure(text="Enumerating…")
        self._app.pump.on(NmapScanCompleted, self._on_completed, only_job=handle.job_id)
        self._app.pump.on(EnumerationFailed, self._on_failed, only_job=handle.job_id)
        self._update_button_state()

    def _on_completed(self, event) -> None:
        self.active_handle = None
        self._status_label.configure(text=f"Found {len(event.hosts)} host(s)")
        self._populate_results(event.hosts)
        self._update_button_state()

    def _on_failed(self, event) -> None:
        self.active_handle = None
        self._status_label.configure(text=f"Enumeration failed: {event.error}")
        self._update_button_state()
