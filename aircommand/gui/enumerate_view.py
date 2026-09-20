"""EnumeratePanel — start an nmap Enumerate scan against the Target Actions
tab's currently selected Target. See docs/design/gui-structure.md 'Target
Actions tab'.

No Cancel button here (deliberate, not an oversight): enumerate.py's own
_drive docstring already notes cancellation isn't meaningfully checkable
mid-scan (nmap's -oX - output is buffered to completion, not iterated line by
line) -- unlike CapturePanel's Cancel button, this wasn't something the
project owner needed to weigh in on.
"""

from __future__ import annotations

from typing import Optional

import customtkinter as ctk

from aircommand.core import AdapterBusy, JobHandle, Target
from aircommand.core.domain import EnumHost
from aircommand.core.events import EnumerationFailed, NmapScanCompleted


class EnumeratePanel(ctk.CTkFrame):
    _HINT_TEXT = (
        "Requires this adapter already associated to the Target's network via "
        "your OS's normal wifi settings — AirCommand doesn't join networks itself."
    )
    _COLUMNS = ("IP", "Hostname", "Open Ports")

    def __init__(self, master, app) -> None:
        super().__init__(master)
        self._app = app
        self.target: Optional[Target] = None
        self.active_handle: Optional[JobHandle] = None

        ctk.CTkLabel(self, text="Enumerate", font=ctk.CTkFont(weight="bold")).pack(side="top", anchor="w")

        self._hint_label = ctk.CTkLabel(self, text=self._HINT_TEXT, wraplength=500, justify="left")
        self._hint_label.pack(side="top", anchor="w", pady=(0, 5))

        self._start_button = ctk.CTkButton(self, text="Start Enumerate", command=self.on_start_clicked)
        self._start_button.pack(side="top", anchor="w", pady=(0, 5))

        self._status_label = ctk.CTkLabel(self, text="")
        self._status_label.pack(side="top", anchor="w")

        header = ctk.CTkFrame(self)
        header.pack(side="top", fill="x")
        for col, text in enumerate(self._COLUMNS):
            ctk.CTkLabel(header, text=text, font=ctk.CTkFont(weight="bold")).grid(row=0, column=col, padx=5, sticky="w")

        self._results_body = ctk.CTkScrollableFrame(self)
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
            for col, value in enumerate((host.ip, hostname, ports)):
                ctk.CTkLabel(self._results_body, text=value).grid(row=row_index, column=col, padx=5, pady=2, sticky="w")

    def on_start_clicked(self) -> None:
        try:
            handle = self._app.engine.enumerate.start_scan(self.target)
        except AdapterBusy as e:
            self._app.status_bar.show_error(f"Radio busy: {e.holder.value} is using the adapter")
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
