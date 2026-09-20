"""CapturePanel — start/cancel a Capture (passive or deauth-assisted) against
the Target Actions tab's currently selected Target. See
docs/design/gui-structure.md 'Target Actions tab'.

Gap fix (see this slice's own briefing, grounded against the actual shipped
HandshakeCaptured event): HandshakeCaptured carries only a `handshake: Handshake`
field, not a `job_id` of its own -- only `handshake.capture_job_id` does. Since
GuiEventPump.on's `only_job` filtering keys off `getattr(event, "job_id", None)`,
it would never match this event type. So HandshakeCaptured is registered here
ONCE, unfiltered, in __init__, and the job match is done manually in
_on_handshake against self.active_handle.job_id.
"""

from __future__ import annotations

from typing import Callable, Optional

import customtkinter as ctk

from aircommand.core import AdapterBusy, JobHandle, Target
from aircommand.core.events import CaptureStopped, DeauthFired, HandshakeCaptured


class CapturePanel(ctk.CTkFrame):
    def __init__(self, master, app, confirm_deauth: Callable[[Target], bool]) -> None:
        super().__init__(master)
        self._app = app
        self._confirm_deauth = confirm_deauth
        self.target: Optional[Target] = None
        self.active_handle: Optional[JobHandle] = None
        self._burst_count = 0

        ctk.CTkLabel(self, text="Capture", font=ctk.CTkFont(weight="bold")).pack(side="top", anchor="w")

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(side="top", fill="x", pady=(0, 5))
        self._start_passive_button = ctk.CTkButton(
            button_row, text="Start Passive Capture", command=self.on_start_passive_clicked
        )
        self._start_passive_button.pack(side="left", padx=(0, 5))
        self._start_deauth_button = ctk.CTkButton(
            button_row, text="Start Deauth-Assisted Capture", command=self.on_start_deauth_clicked
        )
        self._start_deauth_button.pack(side="left", padx=(0, 5))
        self._cancel_button = ctk.CTkButton(button_row, text="Cancel", command=self.on_cancel_clicked)
        self._cancel_button.pack(side="left")

        self._status_label = ctk.CTkLabel(self, text="")
        self._status_label.pack(side="top", anchor="w")

        self._handshake_list = ctk.CTkScrollableFrame(self, label_text="Handshakes")
        self._handshake_list.pack(side="top", fill="both", expand=True)

        # Registered once, unfiltered -- see module docstring.
        self._app.pump.on(HandshakeCaptured, self._on_handshake)

        self._update_button_states()

    def set_target(self, target: Optional[Target]) -> None:
        self.target = target
        self._refresh_handshakes()
        self._update_button_states()

    def _refresh_handshakes(self) -> None:
        for child in self._handshake_list.winfo_children():
            child.destroy()
        if self.target is None:
            return
        for handshake in self._app.engine.capture.list_handshakes(self.target):
            text = f"{handshake.kind.value} — captured {handshake.captured_at.strftime('%Y-%m-%d %H:%M:%S')}"
            ctk.CTkLabel(self._handshake_list, text=text, anchor="w").pack(side="top", fill="x")

    def _update_button_states(self) -> None:
        can_start = self.target is not None and self.active_handle is None
        self._start_passive_button.configure(state="normal" if can_start else "disabled")
        self._start_deauth_button.configure(state="normal" if can_start else "disabled")
        self._cancel_button.configure(state="normal" if self.active_handle is not None else "disabled")

    def on_start_passive_clicked(self) -> None:
        self._try_start(deauth=False)

    def on_start_deauth_clicked(self) -> None:
        # ADR-0001: "extra confirmation friction... still used for deauth,
        # which stayed in scope" -- this is that friction, not optional GUI polish.
        if not self._confirm_deauth(self.target):
            return
        self._try_start(deauth=True)

    def on_cancel_clicked(self) -> None:
        if self.active_handle is not None:
            self.active_handle.cancel()

    def _try_start(self, deauth: bool) -> None:
        try:
            handle = (
                self._app.engine.capture.start_deauth_assisted(self.target)
                if deauth
                else self._app.engine.capture.start_passive(self.target)
            )
        except AdapterBusy as e:
            self._app.status_bar.show_error(
                f"Radio busy: {e.holder.value} is using the adapter — free it first (see Discovery tab)"
            )
            return
        self.active_handle = handle
        self._burst_count = 0
        self._status_label.configure(text="Capturing…")
        self._app.pump.on(CaptureStopped, self._on_stopped, only_job=handle.job_id)
        self._app.pump.on(DeauthFired, self._on_burst, only_job=handle.job_id)  # local live counter --
        # separate registration from the tab-global audit-log one (a later slice, out of scope here).
        self._update_button_states()

    def _on_handshake(self, event) -> None:
        if self.active_handle is None or event.handshake.capture_job_id != self.active_handle.job_id:
            return
        self._refresh_handshakes()

    def _on_stopped(self, event) -> None:
        self.active_handle = None
        self._status_label.configure(text=f"Stopped ({event.reason.value})")
        self._update_button_states()

    def _on_burst(self, event) -> None:
        self._burst_count += 1
        self._status_label.configure(text=f"Capturing… {self._burst_count} deauth burst(s) fired")
