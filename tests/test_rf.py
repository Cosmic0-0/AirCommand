import pytest

from aircommand.core.domain import JobKind
from aircommand.core.procutil import FakeProcRunner
from aircommand.core.rf import AdapterBusy, AdapterMode, RadioController

# Realistic (researched, not hardware-captured) airmon-ng rename-announcement
# line -- see parse.py's parse_airmon_monitor_interface, already tested against
# this exact shape plus a no-rename/fallback case. Used here as the default
# script response so reserve()'s first monitor-mode call has something to parse
# instead of KeyError-ing on an empty script (see note below).
AIRMON_START_OUTPUT_RENAMES = ["(mac80211 monitor mode vif enabled for [phy0]wlan0 on [phy0]wlan0mon)"]


def make_controller(adapter: str = "wlan0", script: dict[str, list[str]] | None = None) -> RadioController:
    # Before RadioController's airmon-ng calls were implemented, reserve() never
    # touched self._proc at all, so an empty script was fine. Now that reserve()
    # really spawns "airmon-ng" on its first monitor-mode use, FakeProcRunner
    # needs a script entry for it (a KeyError otherwise) -- default matches
    # `adapter` renaming to "<adapter>mon", overridable per-test.
    if script is None:
        script = {"airmon-ng": [f"(mac80211 monitor mode vif enabled for [phy0]{adapter} on [phy0]{adapter}mon)"]}
    return RadioController(adapter, FakeProcRunner(script=script))


def test_reserve_then_conflicting_reserve_raises_adapter_busy():
    rf = make_controller()
    rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)

    with pytest.raises(AdapterBusy) as excinfo:
        rf.reserve(AdapterMode.MONITOR_LOCKED, JobKind.CAPTURE_PASSIVE)

    assert excinfo.value.requested == AdapterMode.MONITOR_LOCKED
    assert excinfo.value.holder == JobKind.DISCOVERY


def test_release_then_reserve_again_succeeds():
    rf = make_controller()
    reservation = rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)

    rf.release(reservation)

    second = rf.reserve(AdapterMode.MONITOR_LOCKED, JobKind.CAPTURE_PASSIVE)
    assert second.mode == AdapterMode.MONITOR_LOCKED
    assert second.holder == JobKind.CAPTURE_PASSIVE


def test_reservation_adapter_matches_the_constructed_interface_name():
    # No rename in this script (no "monitor mode vif enabled for ... on ..."
    # line) -- parse_airmon_monitor_interface's fallback path, so the
    # reservation's adapter stays the constructed interface name unchanged.
    rf = make_controller(adapter="wlan1mon", script={"airmon-ng": ["some other unrelated airmon-ng output"]})

    reservation = rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)

    assert reservation.adapter == "wlan1mon"


def test_first_monitor_reserve_spawns_airmon_start_and_uses_parsed_interface():
    spawned_argvs = []
    proc = FakeProcRunner(script={"airmon-ng": AIRMON_START_OUTPUT_RENAMES}, on_spawn=spawned_argvs.append)
    rf = RadioController("wlan0", proc)

    reservation = rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)

    assert spawned_argvs == [["airmon-ng", "start", "wlan0"]]
    assert reservation.adapter == "wlan0mon"  # parsed from the scripted rename-announcement line


def test_second_monitor_reserve_after_release_does_not_respawn_airmon_ng():
    spawned_argvs = []
    proc = FakeProcRunner(script={"airmon-ng": AIRMON_START_OUTPUT_RENAMES}, on_spawn=spawned_argvs.append)
    rf = RadioController("wlan0", proc)

    first = rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)
    rf.release(first)
    second = rf.reserve(AdapterMode.MONITOR_LOCKED, JobKind.CAPTURE_PASSIVE)

    # Only ONE airmon-ng spawn total: the adapter was already in monitor mode
    # from the first reservation (release() deliberately doesn't revert mode --
    # see rf.py), so the second reserve() has nothing to switch.
    assert spawned_argvs == [["airmon-ng", "start", "wlan0"]]
    assert second.adapter == "wlan0mon"


def test_switching_from_monitor_to_managed_spawns_airmon_stop_with_monitor_interface():
    spawned_argvs = []
    proc = FakeProcRunner(
        script={"airmon-ng": AIRMON_START_OUTPUT_RENAMES + ["some stop-mode output, content unused"]},
        on_spawn=spawned_argvs.append,
    )
    rf = RadioController("wlan0", proc)

    monitor_reservation = rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)
    rf.release(monitor_reservation)
    managed_reservation = rf.reserve(AdapterMode.MANAGED, JobKind.NMAP_SCAN)

    assert spawned_argvs == [
        ["airmon-ng", "start", "wlan0"],
        ["airmon-ng", "stop", "wlan0mon"],  # stops using the MONITOR interface name, not the original
    ]
    assert managed_reservation.adapter == "wlan0"  # back to the original managed-mode interface


def test_release_to_managed_is_a_noop_when_never_switched_to_monitor_mode():
    spawned_argvs = []
    proc = FakeProcRunner(script={"airmon-ng": AIRMON_START_OUTPUT_RENAMES}, on_spawn=spawned_argvs.append)
    rf = RadioController("wlan0", proc)

    rf.release_to_managed()

    assert spawned_argvs == []  # no airmon-ng call at all -- nothing to revert


def test_release_to_managed_stops_monitor_mode_and_clears_state():
    spawned_argvs = []
    proc = FakeProcRunner(
        script={"airmon-ng": AIRMON_START_OUTPUT_RENAMES + ["some stop-mode output, content unused"]},
        on_spawn=spawned_argvs.append,
    )
    rf = RadioController("wlan0", proc)
    reservation = rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)
    rf.release(reservation)

    rf.release_to_managed()

    assert spawned_argvs == [["airmon-ng", "start", "wlan0"], ["airmon-ng", "stop", "wlan0mon"]]

    # State was actually cleared (not just spawned-and-ignored): a subsequent
    # monitor-mode reserve() has to switch again, proving self._monitor_adapter
    # really went back to None.
    rf.reserve(AdapterMode.MONITOR_LOCKED, JobKind.CAPTURE_PASSIVE)
    assert spawned_argvs == [
        ["airmon-ng", "start", "wlan0"],
        ["airmon-ng", "stop", "wlan0mon"],
        ["airmon-ng", "start", "wlan0"],
    ]
