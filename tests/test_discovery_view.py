"""NetworksView, TargetPicker for the PySide6 port: real views under qtbot
against a real Engine + FakeProcRunner, the same idiom as test_app.py (real
widgets, .click() on real buttons, qtbot owns the QApplication). The two add
dialogs open with exec() in production; here an autouse fixture makes exec() a
no-op so the test can fill the fields and click Add without blocking (the plan's
"Test strategy for Qt": monkeypatch the call that opens the dialog).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from aircommand.core.domain import EncryptionType, MacAddress, Network
from aircommand.core.engine import Engine
from aircommand.core.events import NetworkDiscovered, NetworkSightingUpdated, TargetAdded, TargetRemoved
from aircommand.core.procutil import FakeProcRunner
from aircommand.gui.cell import Cell
from aircommand.gui.discovery_view import (
    NetworksView,
    TargetPicker,
    _AddAsTargetDialog,
    _AddManuallyDialog,
)
from aircommand.gui.dialog import Dialog
from aircommand.gui.table import CopyTableView

BSSID_1 = "AA:BB:CC:DD:EE:01"
BSSID_2 = "AA:BB:CC:DD:EE:02"

# RadioController.reserve() spawns "airmon-ng" on its first monitor-mode use.
AIRMON_NO_RENAME_OUTPUT = ["monitor mode already enabled on wlan0"]

AP_HEADER = (
    "BSSID, First time seen, Last time seen, channel, Speed, Privacy, Cipher, "
    "Authentication, Power, # beacons, # IV, LAN IP, ID-length, ESSID, Key"
)

# What airodump-ng writes to <prefix>-01.csv (see test_discovery_acceptance.py).
CSV_CONTENT = (
    f"{AP_HEADER}\n"
    f"{BSSID_1}, 2024-01-01 10:00:00, 2024-01-01 10:00:05, 6, 54, WPA2, CCMP, PSK, -40, 10, 0, 0.0.0.0, 4, Net1, \n"
)

DISCOVERY_TICK_COUNT = 1


@pytest.fixture(autouse=True)
def _non_modal_dialogs(monkeypatch):
    """Both add dialogs call exec() in production. exec() blocks a test, so make
    it a no-op: the view still stores the dialog in self._dialog, and the test
    drives the widgets directly (the plan's dialog strategy)."""
    monkeypatch.setattr(_AddAsTargetDialog, "exec", lambda self: None)
    monkeypatch.setattr(_AddManuallyDialog, "exec", lambda self: None)


def _write_csv_on_spawn(argv: list[str]) -> None:
    if "--write" not in argv:
        return
    prefix = argv[argv.index("--write") + 1]
    Path(f"{prefix}-01.csv").write_text(CSV_CONTENT)


class _FakeApp:
    def __init__(self, engine):
        self.engine = engine


def _make_engine(tmp_path, **kwargs) -> Engine:
    proc = kwargs.pop("proc", FakeProcRunner(script={}))
    return Engine(db_path=":memory:", work_dir=tmp_path, adapter="wlan0", proc=proc, **kwargs)


def make_network(bssid: str = BSSID_1, ssid: str = "Net1", channel: int = 6, signal: int = -40) -> Network:
    now = datetime.now()
    return Network(
        bssid=MacAddress.parse(bssid),
        ssid=ssid,
        channel=channel,
        encryption=EncryptionType.WPA2,
        last_signal_dbm=signal,
        first_seen=now,
        last_seen=now,
    )


def make_network_discovered(network: Network) -> NetworkDiscovered:
    return NetworkDiscovered(event_id=uuid.uuid4(), occurred_at=datetime.now(), network=network)


def make_sighting_updated(network: Network) -> NetworkSightingUpdated:
    return NetworkSightingUpdated(event_id=uuid.uuid4(), occurred_at=datetime.now(), network=network)


def make_target_removed(bssid) -> TargetRemoved:
    return TargetRemoved(event_id=uuid.uuid4(), occurred_at=datetime.now(), bssid=bssid)


def _model_cell(view, bssid, title: str) -> str:
    col = [c.title for c in view._COLUMNS].index(title)
    row = view._model._index[bssid]
    return view._model.data(view._model.index(row, col))


# --- NetworksView ------------------------------------------------------------------


def test_networks_view_wraps_its_table_in_a_titled_cell(qtbot, tmp_path):
    """Design spec section 5: "Networks seen" is a titled Cell over one
    CopyTableView, and the page (not the table) scrolls as one unit."""
    view = NetworksView(_FakeApp(_make_engine(tmp_path)))
    qtbot.addWidget(view)
    assert isinstance(view._cell, Cell)
    assert view._cell._title_label.text() == "Networks seen"
    assert isinstance(view._view, CopyTableView)


def test_does_not_seed_from_networks_persisted_by_an_earlier_discovery_job(qtbot, tmp_path):
    """ADR-0010: the table is session-scoped and starts empty, even though the
    database already holds a Network a previous Discovery job persisted."""
    proc = FakeProcRunner(
        script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
        on_spawn=_write_csv_on_spawn,
        running_polls={"airodump-ng": DISCOVERY_TICK_COUNT},
    )
    engine = _make_engine(
        tmp_path, proc=proc, discovery_poll_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )
    handle = engine.discovery.start()
    handle.wait_for_test(timeout=2.0)
    assert engine.discovery.list_networks() != []   # really persisted, the premise of this test

    view = NetworksView(_FakeApp(engine))
    qtbot.addWidget(view)
    assert view._rows == {}


def test_clear_removes_every_row(qtbot, tmp_path):
    view = NetworksView(_FakeApp(_make_engine(tmp_path)))
    qtbot.addWidget(view)
    view.upsert_row(make_network_discovered(make_network(bssid=BSSID_1)))
    view.upsert_row(make_network_discovered(make_network(bssid=BSSID_2, ssid="Net2")))
    assert view._model.rowCount() == 2

    view.clear()

    assert view._rows == {}
    assert view._model.rowCount() == 0


def test_upsert_after_clear_starts_again_at_model_row_zero(qtbot, tmp_path):
    view = NetworksView(_FakeApp(_make_engine(tmp_path)))
    qtbot.addWidget(view)
    view.upsert_row(make_network_discovered(make_network(bssid=BSSID_1)))
    view.upsert_row(make_network_discovered(make_network(bssid=BSSID_2, ssid="Net2")))

    view.clear()
    view.upsert_row(make_network_discovered(make_network(bssid=BSSID_2, ssid="Net2")))

    assert view._model._index[MacAddress.parse(BSSID_2)] == 0
    assert view._model.rowCount() == 1


def test_upsert_row_with_network_discovered_creates_a_new_row(qtbot, tmp_path):
    view = NetworksView(_FakeApp(_make_engine(tmp_path)))
    qtbot.addWidget(view)

    view.upsert_row(make_network_discovered(make_network(bssid=BSSID_2, ssid="Net2")))

    assert MacAddress.parse(BSSID_2) in view._rows
    assert len(view._rows) == 1


def test_upsert_row_with_sighting_updated_updates_existing_row_in_place(qtbot, tmp_path):
    view = NetworksView(_FakeApp(_make_engine(tmp_path)))
    qtbot.addWidget(view)
    view.upsert_row(make_network_discovered(make_network(signal=-40)))

    view.upsert_row(make_sighting_updated(make_network(signal=-70)))

    assert len(view._rows) == 1
    assert _model_cell(view, MacAddress.parse(BSSID_1), "Signal (dBm)") == "-70 dBm"


def test_band_cell_shows_2_4_ghz_5_ghz_or_question_mark_by_channel(qtbot, tmp_path):
    bssid_2_4 = "AA:BB:CC:DD:EE:01"
    bssid_5 = "AA:BB:CC:DD:EE:02"
    bssid_unknown = "AA:BB:CC:DD:EE:03"
    view = NetworksView(_FakeApp(_make_engine(tmp_path)))
    qtbot.addWidget(view)
    view.upsert_row(make_network_discovered(make_network(bssid=bssid_2_4, channel=6)))
    view.upsert_row(make_network_discovered(make_network(bssid=bssid_5, channel=36)))
    view.upsert_row(make_network_discovered(make_network(bssid=bssid_unknown, channel=-1)))

    assert _model_cell(view, MacAddress.parse(bssid_2_4), "Band") == "2.4 GHz"
    assert _model_cell(view, MacAddress.parse(bssid_5), "Band") == "5 GHz"
    assert _model_cell(view, MacAddress.parse(bssid_unknown), "Band") == "?"


def test_band_column_sits_directly_after_channel_column():
    titles = [c.title for c in NetworksView._COLUMNS]
    assert titles.index("Band") == titles.index("Channel") + 1


def test_band_cell_updates_in_place_when_channel_changes_on_sighting(qtbot, tmp_path):
    view = NetworksView(_FakeApp(_make_engine(tmp_path)))
    qtbot.addWidget(view)
    view.upsert_row(make_network_discovered(make_network(channel=6)))
    assert _model_cell(view, MacAddress.parse(BSSID_1), "Band") == "2.4 GHz"

    view.upsert_row(make_sighting_updated(make_network(channel=36)))

    assert len(view._rows) == 1
    assert _model_cell(view, MacAddress.parse(BSSID_1), "Band") == "5 GHz"


def test_add_as_target_button_opens_dialog_and_valid_label_adds_target(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    view = NetworksView(_FakeApp(engine))
    qtbot.addWidget(view)
    view.upsert_row(make_network_discovered(make_network()))

    view._rows[MacAddress.parse(BSSID_1)]["button"].click()

    dialog = view._dialog
    assert dialog is not None
    dialog._label_entry.setText("My house")
    dialog._add_button.click()

    targets = engine.targets.list()
    assert len(targets) == 1
    assert targets[0].bssid == MacAddress.parse(BSSID_1)
    assert targets[0].ssid == "Net1"
    assert targets[0].channel == 6
    assert targets[0].label == "My house"


def test_add_as_target_dialog_empty_label_shows_error_and_does_not_add(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    view = NetworksView(_FakeApp(engine))
    qtbot.addWidget(view)
    view.upsert_row(make_network_discovered(make_network()))

    view._rows[MacAddress.parse(BSSID_1)]["button"].click()
    dialog = view._dialog
    dialog._add_button.click()

    assert dialog._error_label.text() != ""
    assert engine.targets.list() == []


def test_add_as_target_dialog_uses_grid_watch_classes(qtbot, tmp_path):
    """Themed through the app QSS, not CTk light defaults: accent-fill primary
    Add, outline Cancel, and a themed Dialog base (the Qt port styles by class
    property rather than per-widget color kwargs)."""
    engine = _make_engine(tmp_path)
    view = NetworksView(_FakeApp(engine))
    qtbot.addWidget(view)
    view.upsert_row(make_network_discovered(make_network()))
    view._rows[MacAddress.parse(BSSID_1)]["button"].click()

    dialog = view._dialog
    assert isinstance(dialog, Dialog)
    assert dialog._add_button.property("class") == "primary"
    assert dialog._cancel_button.property("class") == "btn"


# --- TargetPicker ------------------------------------------------------------------


def test_target_picker_wraps_its_table_in_a_titled_cell(qtbot, tmp_path):
    picker = TargetPicker(_FakeApp(_make_engine(tmp_path)))
    qtbot.addWidget(picker)
    assert isinstance(picker._cell, Cell)
    assert picker._cell._title_label.text() == "Target allowlist"
    assert isinstance(picker._view, CopyTableView)


def test_target_picker_seeds_from_existing_targets(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "My house")

    picker = TargetPicker(_FakeApp(engine))
    qtbot.addWidget(picker)
    assert MacAddress.parse(BSSID_1) in picker._rows


def test_target_picker_upsert_row_with_target_added_adds_a_new_row(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    picker = TargetPicker(_FakeApp(engine))
    qtbot.addWidget(picker)
    target = engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "My house")
    event = TargetAdded(event_id=uuid.uuid4(), occurred_at=datetime.now(), target=target)

    picker.upsert_row(event)

    assert target.bssid in picker._rows


def test_target_picker_remove_row_removes_row_and_regrids_the_remaining_one(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    target_1 = engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "House1")
    target_2 = engine.targets.add(MacAddress.parse(BSSID_2), "Net2", 11, "House2")

    picker = TargetPicker(_FakeApp(engine))
    qtbot.addWidget(picker)
    assert len(picker._rows) == 2

    picker.remove_row(make_target_removed(target_1.bssid))

    assert target_1.bssid not in picker._rows
    assert target_2.bssid in picker._rows
    # Re-placed at model row 0, no permanent gap where the removed row was.
    assert picker._model._index[target_2.bssid] == 0
    assert _model_cell(picker, target_2.bssid, "SSID") == "Net2"
    assert picker._rows[target_2.bssid]["button"] is not None


def test_target_picker_remove_button_calls_engine_remove_for_real(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    target = engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "House1")

    picker = TargetPicker(_FakeApp(engine))
    qtbot.addWidget(picker)
    picker._rows[target.bssid]["button"].click()

    assert engine.targets.list() == []


def test_add_manually_dialog_valid_input_adds_target(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    picker = TargetPicker(_FakeApp(engine))
    qtbot.addWidget(picker)
    picker._open_add_manually_dialog()
    dialog = picker._dialog

    dialog._bssid_entry.setText(BSSID_1)
    dialog._ssid_entry.setText("Net1")
    dialog._channel_entry.setText("6")
    dialog._label_entry.setText("House1")
    dialog._add_button.click()

    targets = engine.targets.list()
    assert len(targets) == 1
    assert targets[0].bssid == MacAddress.parse(BSSID_1)
    assert targets[0].ssid == "Net1"
    assert targets[0].channel == 6
    assert targets[0].label == "House1"


def test_add_manually_dialog_invalid_bssid_shows_error_and_does_not_add(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    picker = TargetPicker(_FakeApp(engine))
    qtbot.addWidget(picker)
    picker._open_add_manually_dialog()
    dialog = picker._dialog

    dialog._bssid_entry.setText("not-a-bssid")
    dialog._ssid_entry.setText("Net1")
    dialog._channel_entry.setText("6")
    dialog._label_entry.setText("House1")
    dialog._add_button.click()

    assert dialog._error_label.text() != ""
    assert engine.targets.list() == []


def test_add_manually_dialog_invalid_channel_shows_error_and_does_not_add(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    picker = TargetPicker(_FakeApp(engine))
    qtbot.addWidget(picker)
    picker._open_add_manually_dialog()
    dialog = picker._dialog

    dialog._bssid_entry.setText(BSSID_1)
    dialog._ssid_entry.setText("Net1")
    dialog._channel_entry.setText("not-a-number")
    dialog._label_entry.setText("House1")
    dialog._add_button.click()

    assert dialog._error_label.text() != ""
    assert engine.targets.list() == []


def test_add_manually_dialog_uses_grid_watch_classes(qtbot, tmp_path):
    """Themed through the app QSS: accent-fill Add, outline Cancel, a themed
    Dialog base, QLineEdit fields (the Qt port styles by class property)."""
    engine = _make_engine(tmp_path)
    picker = TargetPicker(_FakeApp(engine))
    qtbot.addWidget(picker)
    picker._open_add_manually_dialog()
    dialog = picker._dialog

    assert isinstance(dialog, Dialog)
    assert dialog._add_button.property("class") == "primary"
    assert dialog._cancel_button.property("class") == "btn"
