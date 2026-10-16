"""TargetSelector for the PySide6 port: a real view under qtbot against a real
Engine + FakeProcRunner, the same idiom as tests/test_discovery_view.py (real
widgets, qtbot owns the QApplication). The selector makes no bus subscriptions
of its own, so these tests drive upsert_row / remove_row and _on_menu_selected
directly, exactly as the Tk suite did.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QComboBox

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


def _menu_items(selector: TargetSelector) -> list[str]:
    return [selector._menu.itemText(i) for i in range(selector._menu.count())]


def test_seeds_from_existing_target_and_calls_on_change_once(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    target = engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "House1")

    seen = []
    selector = TargetSelector(_FakeApp(engine), on_change=seen.append)
    qtbot.addWidget(selector)

    assert seen == [target]


def test_upsert_row_for_new_target_does_not_change_selection_or_call_on_change(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    target_1 = engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "House1")

    seen = []
    selector = TargetSelector(_FakeApp(engine), on_change=seen.append)
    qtbot.addWidget(selector)
    assert len(seen) == 1

    target_2 = engine.targets.add(MacAddress.parse(BSSID_2), "Net2", 11, "House2")
    selector.upsert_row(make_target_added(target_2))

    assert len(seen) == 1  # unchanged: no selection change happened
    assert selector._selected_label == selector._label_for(target_1)
    assert selector._label_for(target_2) in selector._targets_by_label


def test_remove_row_for_selected_target_falls_back_and_calls_on_change(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    target_1 = engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "House1")
    target_2 = engine.targets.add(MacAddress.parse(BSSID_2), "Net2", 11, "House2")

    seen = []
    selector = TargetSelector(_FakeApp(engine), on_change=seen.append)
    qtbot.addWidget(selector)
    assert seen == [target_1]  # initial selection is the first target

    selector.remove_row(make_target_removed(target_1.bssid))

    assert seen == [target_1, target_2]


def test_remove_row_for_the_last_remaining_target_falls_back_to_none(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    target = engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "House1")

    seen = []
    selector = TargetSelector(_FakeApp(engine), on_change=seen.append)
    qtbot.addWidget(selector)

    selector.remove_row(make_target_removed(target.bssid))

    assert seen == [target, None]


def test_remove_row_for_a_non_selected_target_does_not_call_on_change(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "House1")
    target_2 = engine.targets.add(MacAddress.parse(BSSID_2), "Net2", 11, "House2")

    seen = []
    selector = TargetSelector(_FakeApp(engine), on_change=seen.append)
    qtbot.addWidget(selector)
    assert len(seen) == 1  # selected target_1

    selector.remove_row(make_target_removed(target_2.bssid))

    assert len(seen) == 1  # unchanged


def test_manual_menu_selection_resolves_target_and_calls_on_change(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    target_1 = engine.targets.add(MacAddress.parse(BSSID_1), "Net1", 6, "House1")
    target_2 = engine.targets.add(MacAddress.parse(BSSID_2), "Net2", 11, "House2")

    seen = []
    selector = TargetSelector(_FakeApp(engine), on_change=seen.append)
    qtbot.addWidget(selector)
    assert seen == [target_1]

    # textActivated fires only on a real dropdown pick, not setCurrentText(), so
    # drive the selector's own handler directly, exactly as the Tk suite did.
    selector._on_menu_selected(selector._label_for(target_2))

    assert seen == [target_1, target_2]


def test_zero_targets_at_construction_shows_placeholder_and_calls_on_change_with_none(qtbot, tmp_path):
    engine = _make_engine(tmp_path)

    seen = []
    selector = TargetSelector(_FakeApp(engine), on_change=seen.append)
    qtbot.addWidget(selector)

    assert seen == [None]
    assert _menu_items(selector) == [TargetSelector._NO_TARGETS_LABEL]


def test_adding_the_first_target_via_pump_keeps_the_placeholder_until_the_user_picks(qtbot, tmp_path):
    """Regression (DEFECT 1): starting from zero Targets, a pump upsert_row of
    the first Target must not leave the combo showing that Target while the
    bound state is still the placeholder. Displayed text and bound state agree,
    and on_change does not fire until a real user pick."""
    engine = _make_engine(tmp_path)
    seen = []
    selector = TargetSelector(_FakeApp(engine), on_change=seen.append)
    qtbot.addWidget(selector)
    assert seen == [None]
    assert selector._menu.currentText() == TargetSelector._NO_TARGETS_LABEL

    target = engine.targets.add(MacAddress.parse(BSSID_1), "HomeNetwork-5G-Upstairs", 36, "Upstairs")
    selector.upsert_row(make_target_added(target))

    assert selector._menu.currentText() == TargetSelector._NO_TARGETS_LABEL
    assert selector._selected_label == TargetSelector._NO_TARGETS_LABEL
    assert selector._targets_by_label.get(selector._selected_label) is None
    assert seen == [None]   # no auto-select, no on_change on a pump event

    selector._on_menu_selected(selector._label_for(target))   # a real user pick binds it
    assert seen == [None, target]
    assert selector._selected_label == selector._label_for(target)


def test_combo_grows_to_show_a_long_target_label_not_the_placeholder_width(qtbot, tmp_path):
    """Regression (DEFECT 2): the combo resizes to its contents, so a long
    Target label added after the narrow "No targets" placeholder is not clipped
    to the placeholder width (the BSSID must stay readable in the closed state)."""
    engine = _make_engine(tmp_path)
    selector = TargetSelector(_FakeApp(engine), on_change=lambda t: None)
    qtbot.addWidget(selector)
    assert selector._menu.sizeAdjustPolicy() == QComboBox.SizeAdjustPolicy.AdjustToContents

    placeholder_width = selector._menu.sizeHint().width()
    target = engine.targets.add(MacAddress.parse(BSSID_1), "HomeNetwork-5G-Upstairs", 36, "Upstairs")
    selector.upsert_row(make_target_added(target))
    selector._on_menu_selected(selector._label_for(target))
    selector._menu.setCurrentText(selector._label_for(target))

    label = selector._label_for(target)
    fm = QFontMetrics(selector._menu.font())
    assert selector._menu.sizeHint().width() >= fm.horizontalAdvance(label)
    assert selector._menu.sizeHint().width() > placeholder_width
