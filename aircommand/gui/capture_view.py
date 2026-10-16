"""CapturePanel: start and cancel a Capture (passive or deauth-assisted) against
the Capture and Attack page's currently selected Target, for the PySide6 port.

See docs/design/gui-structure.md "Target Actions tab",
docs/design/gui-redesign-gridwatch.md section 5 / section 6 item 5, and
docs/design/gui-port/02-reproduction-inventory.md (CapturePanel row) and
05-boundary-design.md "View mapping" for what ports as is. The gating
(_try_start / _update_button_states / set_target), every engine.capture call,
JobHandle.cancel, and the three subscriptions port verbatim; only the widget
layer is new. The panel renders two Cells stacked top to bottom: a plain
"Capture" cell (Start Passive Capture, Cancel, the shared status label, the
handshake list) and a danger-bordered "Deauth" cell (the warning copy, Fire
Deauth, its own burst-count status label).

HandshakeCaptured carries only a `handshake` field, not a `job_id` of its own,
so GuiEventPump's only_job filter (which reads getattr(event, "job_id", None))
can never match it. It is registered ONCE, unfiltered, in __init__, and
_on_handshake matches handshake.capture_job_id by hand against the active job
(05 "The bridge"). CaptureStopped and the local DeauthFired burst counter are
per-Start only_job registrations; the tab-global DeauthFired audit-log
registration is a later slice and is not registered here.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from aircommand.core import AdapterBusy, JobHandle, RadioCommandFailed, Target
from aircommand.core.events import CaptureStopped, DeauthFired, HandshakeCaptured
from aircommand.gui.cell import Cell
from aircommand.gui.dialog import outline_button, primary_button
from aircommand.gui.theme import PALETTE

# Adapted from the deauth-confirm dialog's click-time copy: an always-visible
# warning inside the Deauth cell, so it drops the target-specific "Continue?"
# phrasing and keeps only the always-true warning.
_DEAUTH_WARNING_TEXT = (
    "This will actively transmit deauthentication frames to force a handshake. "
    "Every firing is logged."
)
# The handshake list is a data (mono) list and the pinned theme has no selector
# for a plain Fira Code label, so each row takes a small inline style built from
# PALETTE, the same choice management_view.py made for its adapter name
# (reported as an assumption in the handoff).
_MONO_LABEL_STYLE = f"font-family: 'Fira Code'; font-size: 11px; color: {PALETTE['text']};"


class CapturePanel(QWidget):
    def __init__(
        self, app, confirm_deauth: Callable[[Target], bool], parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._confirm_deauth = confirm_deauth
        self.target: Optional[Target] = None
        self.active_handle: Optional[JobHandle] = None
        self._burst_count = 0
        self._failed_burst_count = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        self._capture_cell = Cell("Capture")
        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(6)
        self._start_passive_button = primary_button("Start Passive Capture", self.on_start_passive_clicked)
        self._cancel_button = outline_button("Cancel", self.on_cancel_clicked)
        button_row.addWidget(self._start_passive_button)
        button_row.addWidget(self._cancel_button)
        button_row.addStretch(1)
        self._capture_cell.body_layout.addLayout(button_row)

        self._status_label = QLabel("")
        self._status_label.setProperty("class", "cellSub")
        self._capture_cell.body_layout.addWidget(self._status_label)

        handshakes_label = QLabel("Handshakes")
        handshakes_label.setProperty("class", "cellSub")
        self._capture_cell.body_layout.addWidget(handshakes_label)

        # A plain QWidget of QLabels, not a table: the Tk original is a label
        # list, and 05 "Rebuilt lists" says label lists call deleteLater() on the
        # old children. The page (App._scroll_page) scrolls, not this list.
        self._handshake_list = QWidget()
        self._handshake_list_layout = QVBoxLayout(self._handshake_list)
        self._handshake_list_layout.setContentsMargins(0, 0, 0, 0)
        self._handshake_list_layout.setSpacing(2)
        self._capture_cell.body_layout.addWidget(self._handshake_list)
        root.addWidget(self._capture_cell)

        self._deauth_cell = Cell("Deauth", danger=True)
        warning = QLabel(_DEAUTH_WARNING_TEXT)
        warning.setProperty("class", "cellSub")
        warning.setWordWrap(True)
        self._deauth_cell.body_layout.addWidget(warning)
        deauth_button_row = QHBoxLayout()
        deauth_button_row.setContentsMargins(0, 0, 0, 0)
        # Fire Deauth is the danger button (class "danger"): the pinned theme
        # styles it as a danger-outline that fills on hover. It is this cell's
        # own primary action, a destructive one.
        self._start_deauth_button = primary_button("Fire Deauth", self.on_start_deauth_clicked, danger=True)
        deauth_button_row.addWidget(self._start_deauth_button)
        deauth_button_row.addStretch(1)
        self._deauth_cell.body_layout.addLayout(deauth_button_row)
        self._deauth_status_label = QLabel("")
        self._deauth_status_label.setProperty("class", "cellSub")
        self._deauth_cell.body_layout.addWidget(self._deauth_status_label)
        root.addWidget(self._deauth_cell)

        # Registered once, unfiltered: see the module docstring.
        self._app.pump.on(HandshakeCaptured, self._on_handshake)

        self._update_button_states()

    def set_target(self, target: Optional[Target]) -> None:
        self.target = target
        self._refresh_handshakes()
        self._update_button_states()

    def _refresh_handshakes(self) -> None:
        while self._handshake_list_layout.count():
            item = self._handshake_list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if self.target is None:
            return
        for handshake in self._app.engine.capture.list_handshakes(self.target):
            text = (
                f"{handshake.kind.value} captured "
                f"{handshake.captured_at.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            label = QLabel(text)
            label.setStyleSheet(_MONO_LABEL_STYLE)
            self._handshake_list_layout.addWidget(label)

    def _update_button_states(self) -> None:
        # Disabled controls dim through the QSS :disabled rule, so gating is just
        # setEnabled(); no hand recoloring of the accent/danger fills.
        can_start = self.target is not None and self.active_handle is None
        self._start_passive_button.setEnabled(can_start)
        self._start_deauth_button.setEnabled(can_start)
        self._cancel_button.setEnabled(self.active_handle is not None)

    def on_start_passive_clicked(self) -> None:
        self._try_start(deauth=False)

    def on_start_deauth_clicked(self) -> None:
        # ADR-0001: deauth stays in scope only with extra confirmation friction,
        # on every firing, with no way to skip it. This is that friction.
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
                f"Radio busy: {e.holder.value} is using the adapter. Free it first on the Discovery page."
            )
            return
        except RadioCommandFailed as e:
            self._app.status_bar.show_error(f"Couldn't switch the adapter into monitor mode: {e}")
            return
        self.active_handle = handle
        self._burst_count = 0
        self._failed_burst_count = 0
        self._status_label.setText("Capturing...")
        self._app.pump.on(CaptureStopped, self._on_stopped, only_job=handle.job_id)
        # Local live burst counter, a separate registration from the tab-global
        # audit-log one (a later slice, out of scope here).
        self._app.pump.on(DeauthFired, self._on_burst, only_job=handle.job_id)
        self._update_button_states()

    def _on_handshake(self, event) -> None:
        if self.active_handle is None or event.handshake.capture_job_id != self.active_handle.job_id:
            return
        self._refresh_handshakes()

    def _on_stopped(self, event) -> None:
        self.active_handle = None
        text = f"Stopped ({event.reason.value})"
        if event.error_detail is not None:   # best-effort hint from airodump-ng's stderr
            text += f": {event.error_detail}"
        self._status_label.setText(text)
        self._update_button_states()

    def _on_burst(self, event) -> None:
        # ADR-0012: DeauthFired fires on every attempt, success or not, so a
        # failed injection is distinguished from a real burst. Written to the
        # Deauth cell's own status label, not the shared Capture one.
        if event.succeeded:
            self._burst_count += 1
            text = f"Capturing... {self._burst_count} deauth burst(s) fired"
            if self._failed_burst_count:
                text += f" ({self._failed_burst_count} failed)"
        else:
            self._failed_burst_count += 1
            text = (
                f"Capturing... deauth burst FAILED ({self._failed_burst_count} so far): "
                f"{event.error_detail or 'no further detail'}"
            )
        self._deauth_status_label.setText(text)
