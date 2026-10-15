"""ManagementView: the Management page for the PySide6 port. Adapter select
plus the manual Check / Kill / Start / Stop monitor-mode controls, wired to the
public RadioController API ADR-0017 added.

See docs/design/gui-redesign-gridwatch.md section 5 ("Management") for the
approved page spec and docs/design/gui-port/02-reproduction-inventory.md
(ManagementView row) and 05-boundary-design.md "View mapping" for what ports as
is. The gating, every engine.radio call, and the on_adapter_selected fan-out
port verbatim; only the widget code is new. The Tk tk.Canvas signal box becomes
a small custom-paint QWidget, the session timer becomes a QTimer parented to the
view, and the rebuilt adapter list deletes its old child rows with deleteLater.

RadioController has no EventBus (ADR-0017), so this page subscribes to no bus
events: it refreshes on page show (App._show_page), after each of its own button
clicks, and via the session timer.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from aircommand.core import AdapterBusy, AdapterInfo, NoAdapterSelected, RadioCommandFailed
from aircommand.gui import theme
from aircommand.gui.cell import Cell, DashGrid, StatusPill
from aircommand.gui.dialog import outline_button, primary_button
from aircommand.gui.theme import PALETTE

_PAGE_TITLE = "Management"
# The mockup's page subtitle was dropped at the user's request (see the Tk
# original's own note); the two hints below are unaffected.
_ADAPTER_SELECT_HINT = "Switching here stops monitor mode if it was running."
_CONTROLS_HINT = (
    "Buttons stay disabled until an adapter is selected. Stop only enables "
    "once monitor mode is actually running."
)


class _SignalBox(QWidget):
    """The 14x14 adapter-row selection indicator (the mockup's .sig-bar with its
    .dot). A custom-paint widget in place of the Tk canvas: a hairline box with
    an accent-filled 6px dot when this is the bound adapter, empty otherwise. It
    is a selection marker, not a live signal-strength meter."""

    def __init__(self, bound: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._bound = bound
        self.setFixedSize(14, 14)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor(PALETTE["border"]))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(0.5, 0.5, 13, 13))
        if self._bound:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(PALETTE["accent"]))
            painter.drawEllipse(QRectF(4, 4, 6, 6))
        painter.end()


class _AdapterRow(QFrame):
    """One clickable adapter row (the mockup's .adapter-row). The whole row is
    the click target, reproducing the Tk version's bind on the row and every
    child. The accent border on the bound row comes from the QSS adapterRow
    selector reading the `selected` property."""

    def __init__(self, info: AdapterInfo, on_click: Callable[[str], None], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._name = info.name
        self._on_click = on_click
        self.setProperty("class", "adapterRow")
        self.setProperty("selected", "true" if info.is_bound else "false")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)
        layout.addWidget(_SignalBox(info.is_bound))

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(0)
        name = QLabel(info.name)
        # The adapter name is Fira Code (the mockup's .adapter-name). The pinned
        # theme has no selector for it, and theme.py is not in P1's cell, so this
        # one widget takes an inline style built from PALETTE (reported as an
        # assumption in the handoff).
        name.setStyleSheet(f"font-family: 'Fira Code'; font-weight: 600; font-size: 12px; color: {PALETTE['text']};")
        description = QLabel(info.description)
        description.setProperty("class", "cellSub")   # muted 11px, the mockup's .adapter-desc
        text.addWidget(name)
        text.addWidget(description)
        layout.addLayout(text)
        layout.addStretch(1)
        layout.addWidget(StatusPill("LIVE" if info.live else "OFF", active=info.live))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_click(self._name)
        super().mousePressEvent(event)


class ManagementView(QWidget):
    def __init__(self, app, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._app = app
        self._session_start: Optional[float] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(8)

        title = QLabel(_PAGE_TITLE)
        title.setProperty("class", "pageTitle")
        root.addWidget(title)

        stats = DashGrid()
        adapter_cell = Cell("Adapter")
        self._adapter_stat_label = QLabel("--")
        self._adapter_stat_label.setProperty("class", "statValue")
        adapter_cell.body_layout.addWidget(self._adapter_stat_label)
        stats.place_cell(adapter_cell, 4)

        monitor_cell = Cell("Monitor mode")
        self._monitor_stat_label = QLabel("OFF")
        self._monitor_stat_label.setProperty("class", "statValue")
        self._monitor_stat_label.setProperty("fill", "off")
        monitor_cell.body_layout.addWidget(self._monitor_stat_label)
        stats.place_cell(monitor_cell, 4)

        session_cell = Cell("Session time")
        self._session_stat_label = QLabel("00:00")
        self._session_stat_label.setProperty("class", "statValue")
        session_cell.body_layout.addWidget(self._session_stat_label)
        stats.place_cell(session_cell, 4)
        root.addWidget(stats)

        controls = DashGrid()

        select_cell = Cell("Adapter select")
        select_hint = QLabel(_ADAPTER_SELECT_HINT)
        select_hint.setProperty("class", "cellSub")
        select_hint.setWordWrap(True)
        select_cell.body_layout.addWidget(select_hint)
        self._adapter_list = QWidget()
        self._adapter_list_layout = QVBoxLayout(self._adapter_list)
        self._adapter_list_layout.setContentsMargins(0, 0, 0, 0)
        self._adapter_list_layout.setSpacing(6)
        select_cell.body_layout.addWidget(self._adapter_list)
        select_cell.body_layout.addStretch(1)
        controls.place_cell(select_cell, 6)

        controls_cell = Cell("Monitor-mode controls")
        controls_hint = QLabel(_CONTROLS_HINT)
        controls_hint.setProperty("class", "cellSub")
        controls_hint.setWordWrap(True)
        controls_cell.body_layout.addWidget(controls_hint)

        button_row = QWidget()
        button_layout = QHBoxLayout(button_row)
        button_layout.setContentsMargins(0, 8, 0, 0)
        button_layout.setSpacing(6)
        self._check_button = outline_button("Check", self._on_check_clicked)
        self._kill_button = outline_button("Kill Conflicting Process", self._on_kill_clicked)
        self._start_button = primary_button("Start Airmon-ng", self._on_start_clicked)
        self._stop_button = outline_button("Stop Airmon-ng", self._on_stop_clicked)
        for button in (self._check_button, self._kill_button, self._start_button, self._stop_button):
            button_layout.addWidget(button)
        button_layout.addStretch(1)
        controls_cell.body_layout.addWidget(button_row)

        self._last_action_label = QLabel("Last action: --")
        # Fira Code muted, the mockup's .last-action line. No pinned selector for
        # it; same inline-style note as the adapter name above.
        self._last_action_label.setStyleSheet(f"font-family: 'Fira Code'; font-size: 11px; color: {PALETTE['muted']};")
        self._last_action_label.setWordWrap(True)
        controls_cell.body_layout.addWidget(self._last_action_label)
        controls_cell.body_layout.addStretch(1)
        controls.place_cell(controls_cell, 6)
        root.addWidget(controls)
        root.addStretch(1)

        # The Tk self.after(1000, ...) loop becomes a QTimer parented to the view
        # (05 "Threading rules"). It reads in-memory RadioController state only,
        # which has no bus event (ADR-0017). refresh() is deliberately NOT called
        # here: App._show_page() calls it when this page is shown, which for the
        # landing page happens at once.
        self._session_timer = QTimer(self)
        self._session_timer.setInterval(1000)
        self._session_timer.timeout.connect(self._tick_session_time)
        self._session_timer.start()
        self._tick_session_time()

    def refresh(self) -> None:
        """Re-read engine.radio's current state and rebuild the adapter list.
        Called on construction's first page show, after every one of this page's
        own button clicks, and whenever App shows this page (ADR-0017's "populate
        a page the user just opened", no bus event)."""
        self._rebuild_adapter_list()
        self._refresh_stats()
        self._update_button_states()

    def _rebuild_adapter_list(self) -> None:
        while self._adapter_list_layout.count():
            item = self._adapter_list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        adapters = self._app.engine.radio.list_adapters()
        if not adapters:
            empty = QLabel("No wifi-capable adapters detected.")
            empty.setProperty("class", "cellSub")
            self._adapter_list_layout.addWidget(empty)
            return
        for info in adapters:
            self._adapter_list_layout.addWidget(_AdapterRow(info, self._on_adapter_row_clicked))

    def _on_adapter_row_clicked(self, name: str) -> None:
        try:
            self._app.engine.radio.select_adapter(name)
        except AdapterBusy as e:
            self._set_last_action(self._describe_error(e))
            return
        self._app.on_adapter_selected()   # fans out to the status bar now, Discovery's band menu in P2 (ADR-0017)
        self._set_last_action(f"selected {name}")
        self.refresh()

    def _refresh_stats(self) -> None:
        selected = self._app.engine.radio.selected_adapter
        self._adapter_stat_label.setText(selected or "--")
        is_live = self._app.engine.radio.is_in_monitor_mode
        self._monitor_stat_label.setText("ON" if is_live else "OFF")
        self._monitor_stat_label.setProperty("fill", "ok" if is_live else "off")
        theme.repolish(self._monitor_stat_label)
        if is_live and self._session_start is None:
            self._session_start = time.monotonic()
        elif not is_live:
            self._session_start = None

    def _tick_session_time(self) -> None:
        if self._session_start is not None:
            elapsed = int(time.monotonic() - self._session_start)
            self._session_stat_label.setText(f"{elapsed // 60:02d}:{elapsed % 60:02d}")
        else:
            self._session_stat_label.setText("00:00")

    def _update_button_states(self) -> None:
        has_adapter = self._app.engine.radio.selected_adapter is not None
        is_live = has_adapter and self._app.engine.radio.is_in_monitor_mode
        # Not pre-emptively disabled while an automatic job holds the radio
        # (AdapterBusy); a caught, visible error is the accepted tradeoff, the
        # same posture CapturePanel/EnumeratePanel's Start buttons take. Disabled
        # controls dim via the QSS :disabled rule, so this is just setEnabled.
        self._check_button.setEnabled(has_adapter)
        self._kill_button.setEnabled(has_adapter)
        self._start_button.setEnabled(has_adapter and not is_live)
        self._start_button.setText("Airmon-ng Running" if is_live else "Start Airmon-ng")
        self._stop_button.setEnabled(is_live)

    def _set_last_action(self, text: str) -> None:
        self._last_action_label.setText(f"Last action: {text}")

    @staticmethod
    def _describe_error(e: Exception) -> str:
        if isinstance(e, NoAdapterSelected):
            return "select an adapter first"
        if isinstance(e, AdapterBusy):
            return f"radio busy: {e.holder.value} is using the adapter"
        return str(e)

    def _on_check_clicked(self) -> None:
        try:
            output = self._app.engine.radio.check_conflicting_processes()
        except (NoAdapterSelected, AdapterBusy) as e:
            self._set_last_action(self._describe_error(e))
            return
        lines = [line for line in output.strip().splitlines() if line.strip()]
        self._set_last_action(f"airmon-ng check: {lines[-1] if lines else 'no output'}")
        self.refresh()

    def _on_kill_clicked(self) -> None:
        try:
            self._app.engine.radio.kill_conflicting_processes()
        except (NoAdapterSelected, AdapterBusy, RadioCommandFailed) as e:
            self._set_last_action(self._describe_error(e))
            return
        self._set_last_action("killed conflicting processes")
        self.refresh()

    def _on_start_clicked(self) -> None:
        try:
            iface = self._app.engine.radio.start_monitor_mode()
        except (NoAdapterSelected, AdapterBusy, RadioCommandFailed) as e:   # RadioBlocked subclasses
            # RadioCommandFailed, so it is already covered here.
            self._set_last_action(self._describe_error(e))
            return
        self._set_last_action(f"started monitor mode on {iface}")
        self.refresh()

    def _on_stop_clicked(self) -> None:
        try:
            self._app.engine.radio.stop_monitor_mode()
        except (NoAdapterSelected, AdapterBusy, RadioCommandFailed) as e:
            self._set_last_action(self._describe_error(e))
            return
        self._set_last_action("stopped monitor mode")
        self.refresh()
