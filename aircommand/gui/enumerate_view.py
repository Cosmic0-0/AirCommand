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
"""

from __future__ import annotations

from typing import Optional

import customtkinter as ctk

from aircommand.core import AdapterBusy, JobHandle, RadioCommandFailed, Target
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
            text_color=PALETTE["bg"], corner_radius=CORNER_RADIUS, font=ui_font(12, "bold"),
        )
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

        self._update_button_state()

    def set_target(self, target: Optional[Target]) -> None:
        self.target = target
        self._clear_results()
        self._update_button_state()

    def _update_button_state(self) -> None:
        can_start = self.target is not None and self.active_handle is None
        self._start_button.configure(state="normal" if can_start else "disabled")

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
        except RadioCommandFailed as e:
            self._app.status_bar.show_error(f"Couldn't switch the adapter into monitor mode: {e}")
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
