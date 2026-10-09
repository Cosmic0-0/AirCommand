"""CapturePanel — start/cancel a Capture (passive or deauth-assisted) against
the Target Actions tab's currently selected Target. See
docs/design/gui-structure.md 'Target Actions tab' and
docs/design/gui-redesign-gridwatch.md §5/§6 item 5 for the Grid Watch layout
this renders into: one controller, two Cells (Capture, then danger-bordered
Deauth) stacked top to bottom -- see this module's own class docstring.

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

from aircommand.core import AdapterBusy, JobHandle, RadioCommandFailed, Target
from aircommand.core.events import CaptureStopped, DeauthFired, HandshakeCaptured
from aircommand.gui.cell import Cell
from aircommand.gui.theme import CORNER_RADIUS, PALETTE, mono_font, ui_font

# Adapted from TargetActionsView._confirm_deauth_dialog's click-time confirm
# text -- this copy is an always-visible warning inside the Deauth cell, not
# a replacement for that dialog, so it drops the target-specific/"Continue?"
# phrasing and keeps only the always-true warning itself.
_DEAUTH_WARNING_TEXT = (
    "This will actively transmit deauthentication frames to force a handshake. "
    "Every firing is logged."
)


class CapturePanel(ctk.CTkFrame):
    """Same one controller as before -- same shared state, same event wiring,
    same `_try_start`/`_update_button_states`/`set_target` behavior -- it just
    renders its UI across two Cells instead of one flat Frame: a plain
    "Capture" cell (Start Passive Capture, Cancel, the shared status label,
    the handshake list) and a danger-bordered "Deauth" cell (the warning copy,
    "Fire Deauth", and its own burst-count status label). Both are built and
    packed top-to-bottom here in __init__; the constructor signature callers
    rely on (`CapturePanel(master, app, confirm_deauth)`) is unchanged.
    """

    def __init__(self, master, app, confirm_deauth: Callable[[Target], bool]) -> None:
        super().__init__(master, fg_color="transparent")
        self._app = app
        self._confirm_deauth = confirm_deauth
        self.target: Optional[Target] = None
        self.active_handle: Optional[JobHandle] = None
        self._burst_count = 0
        self._failed_burst_count = 0

        capture_cell = Cell(self, "Capture")
        capture_button_row = ctk.CTkFrame(capture_cell.body, fg_color="transparent")
        capture_button_row.pack(side="top", fill="x", pady=(0, 8))
        self._start_passive_button = ctk.CTkButton(
            capture_button_row, text="Start Passive Capture", command=self.on_start_passive_clicked,
            fg_color=PALETTE["accent"], hover_color=PALETTE["accent"], border_color=PALETTE["accent"],
            text_color=PALETTE["bg"], corner_radius=CORNER_RADIUS, font=ui_font(12, "bold"),
        )
        self._start_passive_button.pack(side="left", padx=(0, 6))
        self._cancel_button = self._make_outline_button(capture_button_row, "Cancel", self.on_cancel_clicked)
        self._cancel_button.pack(side="left")

        self._status_label = ctk.CTkLabel(
            capture_cell.body, text="", font=ui_font(11), text_color=PALETTE["muted"], anchor="w",
        )
        self._status_label.pack(side="top", anchor="w", pady=(0, 8))

        # Plain CTkFrame, not CTkScrollableFrame -- design spec §6 item 3: no
        # page puts its own fixed-height scroll box inside an individual
        # cell any more (app.py's page is already a CTkScrollableFrame, the
        # whole page scrolls as one unit). A plain label replaces what the
        # scrollable frame's own label_text="Handshakes" used to provide.
        ctk.CTkLabel(
            capture_cell.body, text="Handshakes", font=ui_font(11), text_color=PALETTE["muted"], anchor="w",
        ).pack(side="top", anchor="w", pady=(0, 4))
        self._handshake_list = ctk.CTkFrame(capture_cell.body, fg_color=PALETTE["bg"])
        self._handshake_list.pack(side="top", fill="both", expand=True)
        capture_cell.pack(side="top", fill="both", expand=True, pady=(0, 8))
        self._capture_cell = capture_cell   # exposed for tests asserting on cell structure/styling

        # Danger-bordered/red-titled per the design spec (Deauth section) --
        # Cell's own border_color/title_color kwargs, no parallel styling helper.
        deauth_cell = Cell(self, "Deauth", title_color=PALETTE["danger"], border_color=PALETTE["danger"])
        ctk.CTkLabel(
            deauth_cell.body, text=_DEAUTH_WARNING_TEXT, font=ui_font(11), text_color=PALETTE["muted"],
            anchor="w", wraplength=480, justify="left",
        ).pack(side="top", anchor="w", pady=(0, 8))
        # Filled with danger (not just danger-outlined) -- mirrors the primary-
        # action accent-fill recipe used elsewhere (Start Discovery/Airmon-ng/
        # Crack), substituting danger for accent since Fire Deauth is this
        # cell's own primary action, just a destructive one.
        self._start_deauth_button = ctk.CTkButton(
            deauth_cell.body, text="Fire Deauth", command=self.on_start_deauth_clicked,
            fg_color=PALETTE["danger"], hover_color=PALETTE["danger"], border_color=PALETTE["danger"],
            text_color=PALETTE["bg"], corner_radius=CORNER_RADIUS, font=ui_font(12, "bold"),
        )
        self._start_deauth_button.pack(side="top", anchor="w", pady=(0, 8))
        self._deauth_status_label = ctk.CTkLabel(
            deauth_cell.body, text="", font=ui_font(11), text_color=PALETTE["muted"], anchor="w",
        )
        self._deauth_status_label.pack(side="top", anchor="w")
        deauth_cell.pack(side="top", fill="both", expand=True, pady=(0, 8))
        self._deauth_cell = deauth_cell   # exposed for tests asserting on cell structure/styling

        # Registered once, unfiltered -- see module docstring.
        self._app.pump.on(HandshakeCaptured, self._on_handshake)

        self._update_button_states()

    @staticmethod
    def _make_outline_button(master, text: str, command) -> ctk.CTkButton:
        # Matches management_view.py's _make_outline_button recipe exactly,
        # for consistency across pages -- not a parallel styling helper.
        return ctk.CTkButton(
            master, text=text, command=command, fg_color="transparent", border_width=1,
            border_color=PALETTE["border"], text_color=PALETTE["text"], corner_radius=CORNER_RADIUS,
            font=ui_font(12, "bold"),
        )

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
            ctk.CTkLabel(
                self._handshake_list, text=text, font=mono_font(11), text_color=PALETTE["text"], anchor="w",
            ).pack(side="top", fill="x")

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
        except RadioCommandFailed as e:
            self._app.status_bar.show_error(f"Couldn't switch the adapter into monitor mode: {e}")
            return
        self.active_handle = handle
        self._burst_count = 0
        self._failed_burst_count = 0
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
        text = f"Stopped ({event.reason.value})"
        if event.error_detail is not None:   # best-effort hint from airodump-ng's own
            text += f" — {event.error_detail}"   # stderr -- see events.py's own docstring
        self._status_label.configure(text=text)
        self._update_button_states()

    def _on_burst(self, event) -> None:
        # docs/adr/0012: DeauthFired now fires on every ATTEMPT, success or
        # not -- distinguish them rather than reporting a failed injection as
        # an indistinguishable "burst fired" the way this used to. Written to
        # the Deauth cell's own status label, not the shared Capture one --
        # this message is deauth-specific burst/fail tracking, not general
        # capture status.
        if event.succeeded:
            self._burst_count += 1
            text = f"Capturing… {self._burst_count} deauth burst(s) fired"
            if self._failed_burst_count:
                text += f" ({self._failed_burst_count} failed)"
        else:
            self._failed_burst_count += 1
            text = (f"Capturing… deauth burst FAILED ({self._failed_burst_count} so far)"
                    f" — {event.error_detail or 'no further detail'}")
        self._deauth_status_label.configure(text=text)
