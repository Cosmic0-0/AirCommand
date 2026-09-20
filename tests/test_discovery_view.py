"""NetworksView, TargetPicker against a real ctk.CTk()/CTkToplevel and a real
Engine + FakeProcRunner -- same idiom as tests/test_discovery_acceptance.py
(engine construction, CSV scripting) and tests/test_sudo_dialog.py (real Tk
widgets, .invoke() on real CTkButtons, destroy everything in a finally).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path

import customtkinter as ctk

from aircommand.core.domain import EncryptionType, MacAddress, Network
from aircommand.core.engine import Engine
from aircommand.core.events import NetworkDiscovered, NetworkSightingUpdated, TargetAdded, TargetRemoved
from aircommand.core.procutil import FakeProcRunner
from aircommand.gui.discovery_view import NetworksView, TargetPicker

BSSID_1 = "AA:BB:CC:DD:EE:01"
BSSID_2 = "AA:BB:CC:DD:EE:02"

# RadioController.reserve() spawns "airmon-ng" on its first monitor-mode use --
# see test_discovery_acceptance.py's identical constant/comment.
AIRMON_NO_RENAME_OUTPUT = ["monitor mode already enabled on wlan0"]

AP_HEADER = (
    "BSSID, First time seen, Last time seen, channel, Speed, Privacy, Cipher, "
    "Authentication, Power, # beacons, # IV, LAN IP, ID-length, ESSID, Key"
)

# What airodump-ng actually writes to <prefix>-01.csv -- see
# test_discovery_acceptance.py for the full explanation of this shape.
CSV_CONTENT = (
    f"{AP_HEADER}\n"
    f"{BSSID_1}, 2024-01-01 10:00:00, 2024-01-01 10:00:05, 6, 54, WPA2, CCMP, PSK, -40, 10, 0, 0.0.0.0, 4, Net1, \n"
)

# A finite scripted stdout stream: handle.lines() exhausting naturally is what
# drives Discovery._drive's finally block (mark_terminal), so wait_for_test()
# below returns instead of hanging -- same reasoning as test_discovery_acceptance.py.
DISCOVERY_NOISE = [
    "CH  6 ][ Elapsed: 4 s ][ 2024-01-01 10:00",
    " BSSID              PWR RXQ  Beacons  #Data  CH  MB   ENC  CIPHER AUTH ESSID",
]


def _write_csv_on_spawn(argv: list[str]) -> None:
    if "--write-csv" not in argv:
        return
    prefix = argv[argv.index("--write-csv") + 1]
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


# --- NetworksView ------------------------------------------------------------------


def test_seeds_from_networks_discovered_by_a_real_discovery_job(tmp_path):
    proc = FakeProcRunner(
        script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": DISCOVERY_NOISE},
        on_spawn=_write_csv_on_spawn,
    )
    engine = _make_engine(tmp_path, proc=proc, discovery_poll_interval=timedelta(seconds=0))
    handle = engine.discovery.start()
    handle.wait_for_test(timeout=2.0)

    root = ctk.CTk()
    try:
        view = NetworksView(root, _FakeApp(engine))
        bssid = MacAddress.parse(BSSID_1)
        assert bssid in view._rows
        ssid_label = view._rows[bssid]["labels"][0]
        assert ssid_label.cget("text") == "Net1"
    finally:
        root.destroy()


def test_upsert_row_with_network_discovered_creates_a_new_row(tmp_path):
    engine = _make_engine(tmp_path)
    root = ctk.CTk()
    try:
        view = NetworksView(root, _FakeApp(engine))
        network = make_network(bssid=BSSID_2, ssid="Net2")

        view.upsert_row(make_network_discovered(network))

        assert MacAddress.parse(BSSID_2) in view._rows
        assert len(view._rows) == 1
    finally:
        root.destroy()


def test_upsert_row_with_sighting_updated_updates_existing_row_in_place(tmp_path):
    engine = _make_engine(tmp_path)
    root = ctk.CTk()
    try:
        view = NetworksView(root, _FakeApp(engine))
        view.upsert_row(make_network_discovered(make_network(signal=-40)))

        view.upsert_row(make_sighting_updated(make_network(signal=-70)))

        assert len(view._rows) == 1
        signal_label = view._rows[MacAddress.parse(BSSID_1)]["labels"][4]
        assert signal_label.cget("text") == "-70 dBm"
    finally:
        root.destroy()


def test_add_as_target_button_opens_dialog_and_valid_label_adds_target(tmp_path):
    engine = _make_engine(tmp_path)
    root = ctk.CTk()
    try:
        view = NetworksView(root, _FakeApp(engine))
        view.upsert_row(make_network_discovered(make_network()))

        button = view._rows[MacAddress.parse(BSSID_1)]["button"]
        button.invoke()

        dialog = view._dialog
        assert dialog is not None
        dialog._label_entry.insert(0, "My house")
        dialog._add_button.invoke()

        targets = engine.targets.list()
        assert len(targets) == 1
        assert targets[0].bssid == MacAddress.parse(BSSID_1)
        assert targets[0].ssid == "Net1"
        assert targets[0].channel == 6
        assert targets[0].label == "My house"
    finally:
        root.destroy()


def test_add_as_target_dialog_empty_label_shows_error_and_does_not_add(tmp_path):
    engine = _make_engine(tmp_path)
    root = ctk.CTk()
    try:
        view = NetworksView(root, _FakeApp(engine))
        view.upsert_row(make_network_discovered(make_network()))

        button = view._rows[MacAddress.parse(BSSID_1)]["button"]
        button.invoke()

        dialog = view._dialog
        dialog._add_button.invoke()

        assert dialog._error_label.cget("text") != ""
        assert engine.targets.list() == []
    finally:
        root.destroy()


# --- TargetPicker ------------------------------------------------------------------


def test_target_picker_seeds_from_existing_targets(tmp_path):
    engine = _make_engine(tmp_path)
    engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "My house")

    root = ctk.CTk()
    try:
        picker = TargetPicker(root, _FakeApp(engine))
        assert MacAddress.parse(BSSID_1) in picker._rows
    finally:
        root.destroy()


def test_target_picker_upsert_row_with_target_added_adds_a_new_row(tmp_path):
    engine = _make_engine(tmp_path)
    root = ctk.CTk()
    try:
        picker = TargetPicker(root, _FakeApp(engine))
        target = engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "My house")
        event = TargetAdded(event_id=uuid.uuid4(), occurred_at=datetime.now(), target=target)

        picker.upsert_row(event)

        assert target.bssid in picker._rows
    finally:
        root.destroy()


def test_target_picker_remove_row_removes_row_and_regrids_the_remaining_one(tmp_path):
    engine = _make_engine(tmp_path)
    target_1 = engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "House1")
    target_2 = engine.targets.add(MacAddress.parse(BSSID_2), "Net2", 11, "House2")

    root = ctk.CTk()
    try:
        picker = TargetPicker(root, _FakeApp(engine))
        assert len(picker._rows) == 2

        picker.remove_row(make_target_removed(target_1.bssid))

        assert target_1.bssid not in picker._rows
        assert target_2.bssid in picker._rows
        remaining = picker._rows[target_2.bssid]
        assert remaining["labels"][0].cget("text") == "Net2"
        # Re-gridded at row 0 -- no permanent gap left where the removed row was.
        assert remaining["labels"][0].grid_info()["row"] == 0
        assert remaining["button"].grid_info()["row"] == 0
    finally:
        root.destroy()


def test_target_picker_remove_button_calls_engine_remove_for_real(tmp_path):
    engine = _make_engine(tmp_path)
    target = engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "House1")

    root = ctk.CTk()
    try:
        picker = TargetPicker(root, _FakeApp(engine))
        button = picker._rows[target.bssid]["button"]

        button.invoke()

        assert engine.targets.list() == []
    finally:
        root.destroy()


def test_add_manually_dialog_valid_input_adds_target(tmp_path):
    engine = _make_engine(tmp_path)
    root = ctk.CTk()
    try:
        picker = TargetPicker(root, _FakeApp(engine))
        picker._open_add_manually_dialog()
        dialog = picker._dialog

        dialog._bssid_entry.insert(0, BSSID_1)
        dialog._ssid_entry.insert(0, "Net1")
        dialog._channel_entry.insert(0, "6")
        dialog._label_entry.insert(0, "House1")
        dialog._add_button.invoke()

        targets = engine.targets.list()
        assert len(targets) == 1
        assert targets[0].bssid == MacAddress.parse(BSSID_1)
        assert targets[0].ssid == "Net1"
        assert targets[0].channel == 6
        assert targets[0].label == "House1"
    finally:
        root.destroy()


def test_add_manually_dialog_invalid_bssid_shows_error_and_does_not_add(tmp_path):
    engine = _make_engine(tmp_path)
    root = ctk.CTk()
    try:
        picker = TargetPicker(root, _FakeApp(engine))
        picker._open_add_manually_dialog()
        dialog = picker._dialog

        dialog._bssid_entry.insert(0, "not-a-bssid")
        dialog._ssid_entry.insert(0, "Net1")
        dialog._channel_entry.insert(0, "6")
        dialog._label_entry.insert(0, "House1")
        dialog._add_button.invoke()

        assert dialog._error_label.cget("text") != ""
        assert engine.targets.list() == []
    finally:
        root.destroy()


def test_add_manually_dialog_invalid_channel_shows_error_and_does_not_add(tmp_path):
    engine = _make_engine(tmp_path)
    root = ctk.CTk()
    try:
        picker = TargetPicker(root, _FakeApp(engine))
        picker._open_add_manually_dialog()
        dialog = picker._dialog

        dialog._bssid_entry.insert(0, BSSID_1)
        dialog._ssid_entry.insert(0, "Net1")
        dialog._channel_entry.insert(0, "not-a-number")
        dialog._label_entry.insert(0, "House1")
        dialog._add_button.invoke()

        assert dialog._error_label.cget("text") != ""
        assert engine.targets.list() == []
    finally:
        root.destroy()
