"""EnumeratePanel: start an nmap Enumerate scan against the Capture and Attack
page's currently selected Target, for the PySide6 port.

See docs/design/gui-structure.md "Target Actions tab",
docs/design/gui-redesign-gridwatch.md section 5 / section 6 item 5 (Enumerate is
visually and structurally its own Cell, separate from Capture/Deauth), and
docs/design/gui-port/02-reproduction-inventory.md (EnumeratePanel row) and
05-boundary-design.md "View mapping". The _radio_blocker gating,
_update_button_state, on_start_clicked error handling, both event handlers,
every engine call, and the two subscriptions port verbatim; only the widget
layer is new. The results list becomes a RowTableModel behind a CopyTableView
(set_rows / clear), and the Tk 500ms self.after poll becomes a QTimer parented
to this view.

There is no Cancel button (deliberate): nmap's -oX - output is buffered to
completion, so cancellation is not meaningfully checkable mid-scan.

Radio-state guard: Start Enumerate is disabled, with its own hint, while the
adapter is in monitor mode or no adapter is selected. Enumerate needs the
adapter associated to the Target's network through the OS, which a monitor-mode
adapter cannot be. Nothing on the bus marks a monitor-mode transition
(Management's manual Start/Stop Airmon-ng publish no event, ADR-0017), so the
guard is kept current by the QTimer, not by events.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from aircommand.core import AdapterBusy, JobHandle, NoAdapterSelected, RadioCommandFailed, Target
from aircommand.core.domain import EnumHost
from aircommand.core.events import EnumerationFailed, NmapScanCompleted
from aircommand.gui.cell import Cell
from aircommand.gui.dialog import primary_button
from aircommand.gui.table import Column, CopyTableView, RowTableModel, apply_columns
from aircommand.gui.theme import PALETTE


class EnumeratePanel(QWidget):
    _HINT_TEXT = (
        "Requires this adapter already associated to the Target's network via your "
        "OS's normal wifi settings. AirCommand does not join networks itself."
    )
    _MONITOR_MODE_HINT_TEXT = (
        "Unavailable: the adapter is in monitor mode. Stop it with Stop Airmon-ng on "
        "the Management page, then wait for your OS to rejoin the Target's network "
        "before starting Enumerate."
    )
    _NO_ADAPTER_HINT_TEXT = "Unavailable: no adapter selected. Pick one on the Management page first."
    # Pure in-memory property reads (_radio_blocker), no subprocess, so a short
    # interval costs nothing. The only timer this slice adds (05 "Threading
    # rules"), parented to the view.
    _RADIO_POLL_MS = 500
    # A full 15-character IPv4 ("255.255.255.255") in Fira Code 11px plus the
    # QTableView item padding needs more than 120px, or the last octet elides
    # (P2 widened its 17-character BSSID column to 160 for the same reason).
    # Open Ports is the stretched last section, so its width is the remainder.
    _COLUMNS = (Column("IP", 150), Column("Hostname", 240), Column("Open Ports", 220))

    def __init__(self, app, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._app = app
        self.target: Optional[Target] = None
        self.active_handle: Optional[JobHandle] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._cell = Cell("Enumerate")
        root.addWidget(self._cell)

        # Muted when the standing join requirement is the only note, warn when a
        # radio blocker the operator must fix is active. The pinned theme has no
        # selector for a label that flips between those two colors, so this takes
        # an inline style (reported as an assumption in the handoff).
        self._hint_label = QLabel(self._HINT_TEXT)
        self._hint_label.setWordWrap(True)
        self._cell.body_layout.addWidget(self._hint_label)

        start_row = QHBoxLayout()
        start_row.setContentsMargins(0, 0, 0, 0)
        self._start_button = primary_button("Start Enumerate", self.on_start_clicked)
        start_row.addWidget(self._start_button)
        start_row.addStretch(1)
        self._cell.body_layout.addLayout(start_row)

        self._status_label = QLabel("")
        self._status_label.setProperty("class", "cellSub")
        self._cell.body_layout.addWidget(self._status_label)

        self._model = RowTableModel(self._COLUMNS)
        self._view = CopyTableView()
        self._view.setModel(self._model)
        apply_columns(self._view, self._COLUMNS)
        self._cell.body_layout.addWidget(self._view)

        self._last_radio_blocker: Optional[str] = None
        self._update_button_state()

        self._radio_timer = QTimer(self)
        self._radio_timer.setInterval(self._RADIO_POLL_MS)
        self._radio_timer.timeout.connect(self._poll_radio_state)
        self._radio_timer.start()

    def set_target(self, target: Optional[Target]) -> None:
        self.target = target
        self._clear_results()
        self._update_button_state()

    def _radio_blocker(self) -> Optional[str]:
        """The hint text for whichever radio condition rules out Enumerate, or
        None if the radio is usable. Reads RadioController's in-memory state
        only, the same property the Management page's Monitor mode stat reads."""
        radio = self._app.engine.radio
        if radio.is_in_monitor_mode:
            return self._MONITOR_MODE_HINT_TEXT
        if radio.selected_adapter is None:
            return self._NO_ADAPTER_HINT_TEXT
        return None

    def _poll_radio_state(self) -> None:
        # Only reconfigures when the blocker actually changes, so the button is
        # not redrawn every tick. The QTimer repeats on its own, so this needs no
        # reschedule (unlike the Tk self.after version).
        if self._radio_blocker() != self._last_radio_blocker:
            self._update_button_state()

    def _set_hint(self, text: str, warn: bool) -> None:
        self._hint_label.setText(text)
        color = PALETTE["warn"] if warn else PALETTE["muted"]
        self._hint_label.setStyleSheet(f"color: {color}; font-size: 11px;")

    def _update_button_state(self) -> None:
        blocker = self._radio_blocker()
        self._last_radio_blocker = blocker
        can_start = self.target is not None and self.active_handle is None and blocker is None
        self._set_hint(blocker if blocker is not None else self._HINT_TEXT, blocker is not None)
        self._start_button.setEnabled(can_start)

    def _clear_results(self) -> None:
        self._model.clear()

    def _populate_results(self, hosts: tuple[EnumHost, ...]) -> None:
        rows = []
        for host in hosts:
            hostname = host.hostname if host.hostname is not None else "-"
            ports = ", ".join(str(p) for p in host.open_ports) if host.open_ports else "-"
            rows.append((host.ip, hostname, ports))
        self._model.set_rows(rows)

    def on_start_clicked(self) -> None:
        try:
            handle = self._app.engine.enumerate.start_scan(self.target)
        except AdapterBusy as e:
            self._app.status_bar.show_error(f"Radio busy: {e.holder.value} is using the adapter")
            return
        except NoAdapterSelected:
            # _update_button_state already keeps Start disabled while nothing is
            # selected; this is the backstop so a call that gets here anyway is
            # never a silent failure.
            self._app.status_bar.show_error("No adapter selected: pick one on the Management page first")
            return
        except RadioCommandFailed as e:
            # Enumerate reserves managed mode, so the only RadioCommandFailed here
            # comes from switching the adapter OUT of monitor mode.
            self._app.status_bar.show_error(
                f"Couldn't switch the adapter out of monitor mode back to normal wifi: {e}"
            )
            return
        self.active_handle = handle
        self._clear_results()
        self._status_label.setText("Enumerating...")
        self._app.pump.on(NmapScanCompleted, self._on_completed, only_job=handle.job_id)
        self._app.pump.on(EnumerationFailed, self._on_failed, only_job=handle.job_id)
        self._update_button_state()

    def _on_completed(self, event) -> None:
        self.active_handle = None
        self._status_label.setText(f"Found {len(event.hosts)} host(s)")
        self._populate_results(event.hosts)
        self._update_button_state()

    def _on_failed(self, event) -> None:
        self.active_handle = None
        self._status_label.setText(f"Enumeration failed: {event.error}")
        self._update_button_state()
