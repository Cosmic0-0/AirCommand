"""TargetSelector against a real ctk.CTk() and a real Engine -- same idiom as
tests/test_discovery_view.py (real Tk widgets, real Engine + FakeProcRunner,
destroy roots in a finally).
"""

from __future__ import annotations

import uuid
from datetime import datetime

import customtkinter as ctk

from aircommand.core.domain import MacAddress
from aircommand.core.engine import Engine
from aircommand.core.events import TargetAdded, TargetRemoved
from aircommand.core.procutil import FakeProcRunner
from aircommand.gui.target_selector import TargetSelector

BSSID_1 = "AA:BB:CC:DD:EE:01"
BSSID_2 = "AA:BB:CC:DD:EE:02"


class _FakeApp:
    def __init__(self, engine):
        self.engine = engine


def _make_engine(tmp_path) -> Engine:
    return Engine(db_path=":memory:", work_dir=tmp_path, adapter="wlan0", proc=FakeProcRunner(script={}))


def make_target_added(target) -> TargetAdded:
    return TargetAdded(event_id=uuid.uuid4(), occurred_at=datetime.now(), target=target)


def make_target_removed(bssid) -> TargetRemoved:
    return TargetRemoved(event_id=uuid.uuid4(), occurred_at=datetime.now(), bssid=bssid)


def test_seeds_from_existing_target_and_calls_on_change_once(tmp_path):
    engine = _make_engine(tmp_path)
    target = engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "House1")

    root = ctk.CTk()
    try:
        seen = []
        TargetSelector(root, _FakeApp(engine), on_change=seen.append)

        assert seen == [target]
    finally:
        root.destroy()


def test_upsert_row_for_new_target_does_not_change_selection_or_call_on_change(tmp_path):
    engine = _make_engine(tmp_path)
    target_1 = engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "House1")

    root = ctk.CTk()
    try:
        seen = []
        selector = TargetSelector(root, _FakeApp(engine), on_change=seen.append)
        assert len(seen) == 1

        target_2 = engine.targets.add(MacAddress.parse(BSSID_2), "Net2", 11, "House2")
        selector.upsert_row(make_target_added(target_2))

        assert len(seen) == 1  # unchanged -- no selection change happened
        assert selector._selected_label == selector._label_for(target_1)
        assert selector._label_for(target_2) in selector._targets_by_label
    finally:
        root.destroy()


def test_remove_row_for_selected_target_falls_back_and_calls_on_change(tmp_path):
    engine = _make_engine(tmp_path)
    target_1 = engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "House1")
    target_2 = engine.targets.add(MacAddress.parse(BSSID_2), "Net2", 11, "House2")

    root = ctk.CTk()
    try:
        seen = []
        selector = TargetSelector(root, _FakeApp(engine), on_change=seen.append)
        assert seen == [target_1]  # initial selection is the first target

        selector.remove_row(make_target_removed(target_1.bssid))

        assert seen == [target_1, target_2]
    finally:
        root.destroy()


def test_remove_row_for_the_last_remaining_target_falls_back_to_none(tmp_path):
    engine = _make_engine(tmp_path)
    target = engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "House1")

    root = ctk.CTk()
    try:
        seen = []
        selector = TargetSelector(root, _FakeApp(engine), on_change=seen.append)

        selector.remove_row(make_target_removed(target.bssid))

        assert seen == [target, None]
    finally:
        root.destroy()


def test_remove_row_for_a_non_selected_target_does_not_call_on_change(tmp_path):
    engine = _make_engine(tmp_path)
    target_1 = engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "House1")
    target_2 = engine.targets.add(MacAddress.parse(BSSID_2), "Net2", 11, "House2")

    root = ctk.CTk()
    try:
        seen = []
        selector = TargetSelector(root, _FakeApp(engine), on_change=seen.append)
        assert len(seen) == 1  # selected target_1

        selector.remove_row(make_target_removed(target_2.bssid))

        assert len(seen) == 1  # unchanged
    finally:
        root.destroy()


def test_manual_menu_selection_resolves_target_and_calls_on_change(tmp_path):
    engine = _make_engine(tmp_path)
    target_1 = engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "House1")
    target_2 = engine.targets.add(MacAddress.parse(BSSID_2), "Net2", 11, "House2")

    root = ctk.CTk()
    try:
        seen = []
        selector = TargetSelector(root, _FakeApp(engine), on_change=seen.append)
        assert seen == [target_1]

        # Verified directly against this installed customtkinter version:
        # CTkOptionMenu.set() does NOT invoke `command` (only real dropdown-menu
        # clicks, via _dropdown_callback, do) -- so drive the selector's own
        # handler directly, exactly as this slice's own spec allows.
        selector._on_menu_selected(selector._label_for(target_2))

        assert seen == [target_1, target_2]
    finally:
        root.destroy()


def test_zero_targets_at_construction_shows_placeholder_and_calls_on_change_with_none(tmp_path):
    engine = _make_engine(tmp_path)

    root = ctk.CTk()
    try:
        seen = []
        selector = TargetSelector(root, _FakeApp(engine), on_change=seen.append)

        assert seen == [None]
        assert selector._menu._values == [TargetSelector._NO_TARGETS_LABEL]
    finally:
        root.destroy()
