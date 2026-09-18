from aircommand.core.allowlist import Allowlist, NotATargetError
from aircommand.core.domain import MacAddress
from aircommand.core.events import EventBus, TargetAdded, TargetRemoved
from aircommand.core.persistence.db import Database

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")
BSSID_2 = MacAddress(value="AA:BB:CC:DD:EE:02")


def make_allowlist() -> tuple[Allowlist, EventBus]:
    bus = EventBus()
    return Allowlist(Database(":memory:").targets, bus), bus


def test_add_new_bssid_publishes_one_target_added_and_returns_the_target():
    allowlist, bus = make_allowlist()
    received = []
    bus.subscribe(received.append, TargetAdded)

    target = allowlist.add(BSSID_1, "Home-WiFi", 6, "My house")

    assert len(received) == 1
    assert received[0].target == target
    assert target.bssid == BSSID_1
    assert target.ssid == "Home-WiFi"
    assert target.channel == 6
    assert target.label == "My house"


def test_add_again_on_same_bssid_publishes_another_target_added_and_preserves_date_added():
    allowlist, bus = make_allowlist()
    first = allowlist.add(BSSID_1, "Home-WiFi", 6, "My house")
    received = []
    bus.subscribe(received.append, TargetAdded)

    renamed = allowlist.add(BSSID_1, "Home-WiFi", 11, "Renamed label")

    assert len(received) == 1
    assert received[0].target == renamed
    assert renamed.label == "Renamed label"
    assert renamed.channel == 11
    assert renamed.date_added == first.date_added


def test_remove_existing_target_publishes_target_removed_and_drops_from_list():
    allowlist, bus = make_allowlist()
    allowlist.add(BSSID_1, "Home-WiFi", 6, "My house")
    received = []
    bus.subscribe(received.append, TargetRemoved)

    allowlist.remove(BSSID_1)

    assert len(received) == 1
    assert received[0].bssid == BSSID_1
    assert BSSID_1 not in {t.bssid for t in allowlist.list()}


def test_remove_never_a_target_publishes_no_event_and_does_not_raise():
    allowlist, bus = make_allowlist()
    received = []
    bus.subscribe(received.append, TargetRemoved)

    allowlist.remove(BSSID_1)  # must not raise

    assert received == []


def test_require_target_on_existing_bssid_returns_it():
    allowlist, _bus = make_allowlist()
    target = allowlist.add(BSSID_1, "Home-WiFi", 6, "My house")

    assert allowlist.require_target(BSSID_1) == target


def test_require_target_on_absent_bssid_raises_not_a_target_error_with_bssid():
    allowlist, _bus = make_allowlist()

    try:
        allowlist.require_target(BSSID_1)
        assert False, "expected NotATargetError"
    except NotATargetError as error:
        assert error.bssid == BSSID_1


def test_get_and_list_round_trip():
    allowlist, _bus = make_allowlist()
    target_1 = allowlist.add(BSSID_1, "Home-WiFi", 6, "My house")
    target_2 = allowlist.add(BSSID_2, "Office-WiFi", 11, "Work")

    assert allowlist.get(BSSID_1) == target_1
    assert allowlist.get(BSSID_2) == target_2
    assert {t.id for t in allowlist.list()} == {target_1.id, target_2.id}


def test_get_on_absent_bssid_returns_none():
    allowlist, _bus = make_allowlist()

    assert allowlist.get(BSSID_1) is None
