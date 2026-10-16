"""NetworksView, TargetPicker: the Discovery and Targets page's two tables for
the PySide6 port. See docs/design/gui-structure.md "Discovery & Targets tab",
docs/design/gui-redesign-gridwatch.md section 5 ("Discovery & Targets"), and
docs/design/gui-port/05-boundary-design.md "Tables" for the shape this
implements.

Each table is one RowTableModel behind a CopyTableView, wrapped in a titled
Cell ("Networks seen" / "Target allowlist"). The bssid-keyed row model, the
upsert_row / remove_row handlers, every engine.targets call, and the two add
dialogs' validation port as is from the CustomTkinter original; only the widget
layer is new. The per-row action button (NetworksView's "Add as Target",
TargetPicker's "Remove") sits in a trailing column placed with
QTableView.setIndexWidget, the smaller choice than a custom delegate for these
short tables.

NetworksView is session-scoped (ADR-0010): it starts empty and shows only what
the current Discovery session has heard, so it is never seeded from the
database. clear() is how App's "New Session" button ends one session and begins
the next.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from aircommand.core import BSSID, MacAddress, Network, Target
from aircommand.core.events import TargetAdded, TargetRemoved
from aircommand.gui.cell import Cell
from aircommand.gui.dialog import (
    Dialog,
    error_label,
    field_label,
    outline_button,
    primary_button,
    themed_entry,
)
from aircommand.gui.table import Column, CopyTableView, RowTableModel, apply_columns


# A full 17-character BSSID in Fira Code 11px plus the QTableView cell padding
# needs more than the 130px the Tk layout used, or the last octet elides.
_BSSID_WIDTH = 160
# The per-row action button (class "btn"/"primary") has 7px vertical padding, so
# the default QTableView row is too short and clips the glyphs. This row height
# fits the whole button in both tables.
_ROW_HEIGHT = 36


class NetworksView(QWidget):
    # Widths trimmed from the Tk values (except the widened BSSID) so all seven
    # data columns plus the action button fit at the 1040px minimum window with
    # no horizontal scroll. The action column stretches, so its slack goes there
    # at the wider default window; its initial width is the button's own minimum.
    _COLUMNS = (
        Column("SSID", 130), Column("BSSID", _BSSID_WIDTH), Column("Channel", 64), Column("Band", 68),
        Column("Encryption", 80), Column("Signal (dBm)", 86), Column("Last Seen", 84),
    )
    _ACTION_COLUMN = Column("", 110)   # trailing column holding the per-row "Add as Target" button

    def __init__(self, app, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._app = app
        # bssid -> {"values": tuple[str, ...], "button": QPushButton}. The model
        # renders the rows; this mirror keeps the keyed logic the handlers need
        # (is-this-new, the current cell values) and the per-row button handle.
        self._rows: dict[BSSID, dict] = {}
        self._dialog: "_AddAsTargetDialog | None" = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._cell = Cell("Networks seen")
        layout.addWidget(self._cell)

        model_columns = (*self._COLUMNS, self._ACTION_COLUMN)
        self._model = RowTableModel(model_columns)
        self._view = CopyTableView()
        self._view.setModel(self._model)
        apply_columns(self._view, model_columns)
        self._view.verticalHeader().setDefaultSectionSize(_ROW_HEIGHT)
        self._cell.body_layout.addWidget(self._view)

    def upsert_row(self, event) -> None:
        self._upsert(event.network)

    def clear(self) -> None:
        for row in self._rows.values():
            if row["button"] is not None:
                row["button"].deleteLater()
        self._rows.clear()
        self._model.clear()   # a fresh upsert then starts again at row 0

    def _upsert(self, network: Network) -> None:
        values = (
            network.ssid or "(hidden)",
            str(network.bssid),
            str(network.channel),
            network.band.value if network.band is not None else "?",
            network.encryption.value.upper(),
            f"{network.last_signal_dbm} dBm",
            network.last_seen.strftime("%H:%M:%S"),
        )
        if network.bssid not in self._rows:
            self._model.upsert(network.bssid, (*values, ""))
            row = self._model.rowCount() - 1   # upsert appends a new key at the end
            button = outline_button(
                "Add as Target", lambda n=network: self._open_add_target_dialog(n)
            )
            self._view.setIndexWidget(self._model.index(row, len(self._COLUMNS)), button)
            self._rows[network.bssid] = {"values": values, "button": button}
        else:
            self._model.upsert(network.bssid, (*values, ""))
            self._rows[network.bssid]["values"] = values

    def _open_add_target_dialog(self, network: Network) -> None:
        self._dialog = _AddAsTargetDialog(self._app, network, self)
        self._dialog.exec()


class _AddAsTargetDialog(Dialog):
    """Modal, pre-filled bssid/ssid/channel (read-only), asks only for a label.
    Add validates, calls engine.targets.add(...), and accepts; Cancel rejects."""

    def __init__(self, app, network: Network, parent: Optional[QWidget] = None) -> None:
        super().__init__("Add as Target", parent)
        self._app = app
        self._network = network

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(8)

        layout.addWidget(field_label(f"SSID: {network.ssid or '(hidden)'}"))
        layout.addWidget(field_label(f"BSSID: {network.bssid}"))
        layout.addWidget(field_label(f"Channel: {network.channel}"))

        layout.addWidget(field_label("Label:"))
        self._label_entry = themed_entry()
        self._label_entry.returnPressed.connect(self._on_add)
        layout.addWidget(self._label_entry)

        self._error_label = error_label()
        layout.addWidget(self._error_label)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self._cancel_button = outline_button("Cancel", self.reject)
        self._add_button = primary_button("Add", self._on_add)
        button_row.addWidget(self._cancel_button)
        button_row.addWidget(self._add_button)
        layout.addLayout(button_row)

        self._label_entry.setFocus()

    def _on_add(self) -> None:
        label = self._label_entry.text().strip()
        if not label:
            self._error_label.setText("Label can't be empty")
            return
        self._app.engine.targets.add(self._network.bssid, self._network.ssid, self._network.channel, label)
        self.accept()


class TargetPicker(QWidget):
    _COLUMNS = (
        Column("SSID", 150), Column("BSSID", _BSSID_WIDTH), Column("Channel", 70),
        Column("Label", 140), Column("Date Added", 130),
    )
    _ACTION_COLUMN = Column("", 120)   # trailing column holding the per-row "Remove" button

    def __init__(self, app, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._app = app
        self._rows: dict[BSSID, dict] = {}
        self._dialog: "_AddManuallyDialog | None" = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._cell = Cell("Target allowlist")
        layout.addWidget(self._cell)

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.addStretch(1)
        self._add_manually_button = outline_button("Add manually", self._open_add_manually_dialog)
        header_layout.addWidget(self._add_manually_button)
        self._cell.body_layout.addWidget(header)

        model_columns = (*self._COLUMNS, self._ACTION_COLUMN)
        self._model = RowTableModel(model_columns)
        self._view = CopyTableView()
        self._view.setModel(self._model)
        apply_columns(self._view, model_columns)
        self._view.verticalHeader().setDefaultSectionSize(_ROW_HEIGHT)
        self._cell.body_layout.addWidget(self._view)

        for target in app.engine.targets.list():
            self._upsert(target)

    def upsert_row(self, event: TargetAdded) -> None:
        self._upsert(event.target)

    def remove_row(self, event: TargetRemoved) -> None:
        self._remove(event.bssid)

    def _upsert(self, target: Target) -> None:
        values = (
            target.ssid,
            str(target.bssid),
            str(target.channel),
            target.label,
            target.date_added.strftime("%Y-%m-%d %H:%M"),
        )
        if target.bssid not in self._rows:
            self._model.upsert(target.bssid, (*values, ""))
            self._rows[target.bssid] = {"values": values, "button": None}
            self._place_remove_button(target.bssid)
        else:
            self._model.upsert(target.bssid, (*values, ""))
            self._rows[target.bssid]["values"] = values

    def _place_remove_button(self, bssid: BSSID) -> None:
        row = list(self._rows).index(bssid)
        button = outline_button("Remove", lambda b=bssid: self._app.engine.targets.remove(b))
        self._view.setIndexWidget(self._model.index(row, len(self._COLUMNS)), button)
        self._rows[bssid]["button"] = button

    def _remove(self, bssid: BSSID) -> None:
        if bssid not in self._rows:
            return
        # Removing the row shifts every later row up by one, so the trailing
        # buttons must be rebuilt at the new indices. Drop them all, remove the
        # row from the model, then re-place one per remaining row in order.
        for row in self._rows.values():
            if row["button"] is not None:
                row["button"].deleteLater()
                row["button"] = None
        self._model.remove(bssid)
        del self._rows[bssid]
        for remaining in self._rows:
            self._place_remove_button(remaining)

    def _open_add_manually_dialog(self) -> None:
        self._dialog = _AddManuallyDialog(self._app, self)
        self._dialog.exec()


class _AddManuallyDialog(Dialog):
    """Modal, all four fields editable, for a Target not currently in
    NetworksView (e.g. temporarily out of range). Add validates (MacAddress.parse,
    non-empty SSID, int channel, non-empty label), calls engine.targets.add(...),
    and accepts; Cancel rejects."""

    def __init__(self, app, parent: Optional[QWidget] = None) -> None:
        super().__init__("Add Target manually", parent)
        self._app = app

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(8)

        layout.addWidget(field_label("BSSID:"))
        self._bssid_entry = themed_entry()
        self._bssid_entry.returnPressed.connect(self._on_add)
        layout.addWidget(self._bssid_entry)

        layout.addWidget(field_label("SSID:"))
        self._ssid_entry = themed_entry()
        self._ssid_entry.returnPressed.connect(self._on_add)
        layout.addWidget(self._ssid_entry)

        layout.addWidget(field_label("Channel:"))
        self._channel_entry = themed_entry()
        self._channel_entry.returnPressed.connect(self._on_add)
        layout.addWidget(self._channel_entry)

        layout.addWidget(field_label("Label:"))
        self._label_entry = themed_entry()
        self._label_entry.returnPressed.connect(self._on_add)
        layout.addWidget(self._label_entry)

        self._error_label = error_label()
        layout.addWidget(self._error_label)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self._cancel_button = outline_button("Cancel", self.reject)
        self._add_button = primary_button("Add", self._on_add)
        button_row.addWidget(self._cancel_button)
        button_row.addWidget(self._add_button)
        layout.addLayout(button_row)

        self._bssid_entry.setFocus()

    def _on_add(self) -> None:
        try:
            bssid = MacAddress.parse(self._bssid_entry.text().strip())
        except ValueError:
            self._error_label.setText("Invalid BSSID")
            return

        ssid = self._ssid_entry.text().strip()
        if not ssid:
            self._error_label.setText("SSID can't be empty")
            return

        try:
            channel = int(self._channel_entry.text().strip())
        except ValueError:
            self._error_label.setText("Invalid channel")
            return

        label = self._label_entry.text().strip()
        if not label:
            self._error_label.setText("Label can't be empty")
            return

        self._app.engine.targets.add(bssid, ssid, channel, label)
        self.accept()
