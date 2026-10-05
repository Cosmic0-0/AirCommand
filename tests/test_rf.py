import pytest

from aircommand.core.domain import JobKind
from aircommand.core.procutil import FakeProcRunner
from aircommand.core.rf import AdapterBusy, AdapterMode, RadioCommandFailed, RadioController

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

    # check kill fires first (docs/adr/0005-networkmanager-check-kill.md), THEN
    # the actual mode switch.
    assert spawned_argvs == [["airmon-ng", "check", "kill"], ["airmon-ng", "start", "wlan0"]]
    assert reservation.adapter == "wlan0mon"  # parsed from the scripted rename-announcement line


def test_second_monitor_reserve_after_release_does_not_respawn_airmon_ng():
    spawned_argvs = []
    proc = FakeProcRunner(script={"airmon-ng": AIRMON_START_OUTPUT_RENAMES}, on_spawn=spawned_argvs.append)
    rf = RadioController("wlan0", proc)

    first = rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)
    rf.release(first)
    second = rf.reserve(AdapterMode.MONITOR_LOCKED, JobKind.CAPTURE_PASSIVE)

    # Only ONE check-kill/start PAIR total: the adapter was already in monitor
    # mode from the first reservation (release() deliberately doesn't revert
    # mode -- see rf.py), so the second reserve() has nothing to switch (and
    # therefore no reason to re-run "airmon-ng check kill" either).
    assert spawned_argvs == [["airmon-ng", "check", "kill"], ["airmon-ng", "start", "wlan0"]]
    assert second.adapter == "wlan0mon"


def test_switching_from_monitor_to_managed_spawns_airmon_stop_with_monitor_interface():
    spawned_argvs = []
    proc = FakeProcRunner(
        script={
            "airmon-ng": AIRMON_START_OUTPUT_RENAMES + ["some stop-mode output, content unused"],
            "systemctl": ["Synchronizing state..."],
        },
        on_spawn=spawned_argvs.append,
    )
    rf = RadioController("wlan0", proc)

    monitor_reservation = rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)
    rf.release(monitor_reservation)
    managed_reservation = rf.reserve(AdapterMode.MANAGED, JobKind.NMAP_SCAN)

    assert spawned_argvs == [
        ["airmon-ng", "check", "kill"],
        ["airmon-ng", "start", "wlan0"],
        ["airmon-ng", "stop", "wlan0mon"],  # stops using the MONITOR interface name, not the original
        ["systemctl", "restart", "NetworkManager"],  # NetworkManager comes back -- see rf.py, ADR-0005
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
        script={
            "airmon-ng": AIRMON_START_OUTPUT_RENAMES + ["some stop-mode output, content unused"],
            "systemctl": ["Synchronizing state..."],
        },
        on_spawn=spawned_argvs.append,
    )
    rf = RadioController("wlan0", proc)
    reservation = rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)
    rf.release(reservation)

    rf.release_to_managed()

    assert spawned_argvs == [
        ["airmon-ng", "check", "kill"],
        ["airmon-ng", "start", "wlan0"],
        ["airmon-ng", "stop", "wlan0mon"],
        ["systemctl", "restart", "NetworkManager"],
    ]

    # State was actually cleared (not just spawned-and-ignored): a subsequent
    # monitor-mode reserve() has to switch again, proving self._monitor_adapter
    # really went back to None.
    rf.reserve(AdapterMode.MONITOR_LOCKED, JobKind.CAPTURE_PASSIVE)
    assert spawned_argvs == [
        ["airmon-ng", "check", "kill"],
        ["airmon-ng", "start", "wlan0"],
        ["airmon-ng", "stop", "wlan0mon"],
        ["systemctl", "restart", "NetworkManager"],
        ["airmon-ng", "check", "kill"],
        ["airmon-ng", "start", "wlan0"],
    ]


def test_managed_transition_restarts_network_manager_then_next_monitor_reserve_rechecks_kill():
    # Proves the confirmed design end-to-end (docs/adr/0005-networkmanager-
    # check-kill.md): NetworkManager's restart is tied to the monitor->managed
    # transition ITSELF, not deferred to Engine.shutdown() -- so a mid-session
    # round trip (Discovery pausing so the operator can run Enumerate via their
    # OS's normal wifi settings, then Discovery resuming) really does restart
    # NetworkManager and then re-kill it, rather than restarting it once and
    # leaving it alone (or never restarting it) for the rest of the session.
    spawned_argvs = []
    proc = FakeProcRunner(
        script={
            "airmon-ng": AIRMON_START_OUTPUT_RENAMES + ["some stop-mode output, content unused"],
            "systemctl": ["Synchronizing state..."],
        },
        on_spawn=spawned_argvs.append,
    )
    rf = RadioController("wlan0", proc)

    # Discovery starts (monitor mode)...
    discovery_reservation = rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)
    rf.release(discovery_reservation)

    # ...operator pauses Discovery and reserves MANAGED to run Enumerate --
    # this is the monitor->managed transition, so NetworkManager restarts.
    managed_reservation = rf.reserve(AdapterMode.MANAGED, JobKind.NMAP_SCAN)
    rf.release(managed_reservation)

    assert spawned_argvs == [
        ["airmon-ng", "check", "kill"],
        ["airmon-ng", "start", "wlan0"],
        ["airmon-ng", "stop", "wlan0mon"],
        ["systemctl", "restart", "NetworkManager"],
    ]

    # ...Discovery resumes: the adapter goes back to monitor mode, and
    # "airmon-ng check kill" fires again -- NOT skipped just because it already
    # ran once earlier this session. This is the crux of the design: the
    # restart above was NOT the last one, because NetworkManager needs to be
    # killed again before every subsequent managed->monitor switch too.
    rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)

    assert spawned_argvs == [
        ["airmon-ng", "check", "kill"],
        ["airmon-ng", "start", "wlan0"],
        ["airmon-ng", "stop", "wlan0mon"],
        ["systemctl", "restart", "NetworkManager"],
        ["airmon-ng", "check", "kill"],
        ["airmon-ng", "start", "wlan0"],
    ]


def test_check_kill_failure_raises_radio_command_failed_and_skips_airmon_start():
    spawned_argvs = []
    proc = FakeProcRunner(
        script={"airmon-ng": AIRMON_START_OUTPUT_RENAMES},
        on_spawn=spawned_argvs.append,
        returncodes={"airmon-ng": 1},
        stderr={"airmon-ng": ["sudo: a password is required"]},
    )
    rf = RadioController("wlan0", proc)

    with pytest.raises(RadioCommandFailed) as excinfo:
        rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)

    assert excinfo.value.argv == ["airmon-ng", "check", "kill"]
    assert excinfo.value.returncode == 1

    # "airmon-ng start" never ran -- check-kill's failure stopped the
    # managed->monitor switch before it got that far.
    assert spawned_argvs == [["airmon-ng", "check", "kill"]]


def test_network_manager_restart_failure_raises_radio_command_failed():
    spawned_argvs = []
    proc = FakeProcRunner(
        script={
            "airmon-ng": AIRMON_START_OUTPUT_RENAMES + ["some stop-mode output, content unused"],
            "systemctl": ["Failed to restart NetworkManager.service: Access denied"],
        },
        on_spawn=spawned_argvs.append,
        returncodes={"systemctl": 1},
        stderr={"systemctl": ["Interactive authentication required."]},
    )
    rf = RadioController("wlan0", proc)
    monitor_reservation = rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)
    rf.release(monitor_reservation)

    with pytest.raises(RadioCommandFailed) as excinfo:
        rf.reserve(AdapterMode.MANAGED, JobKind.NMAP_SCAN)

    assert excinfo.value.argv == ["systemctl", "restart", "NetworkManager"]
    assert excinfo.value.returncode == 1


def test_airmon_start_nonzero_does_not_raise():
    # Regression guard: airmon-ng start/stop's own exit codes stay deliberately
    # unchecked (unconfirmed semantics, see rf.py) -- only check-kill and
    # systemctl raise. returncodes is keyed by argv[0] only ("airmon-ng" covers
    # both check-kill and start), so on_spawn mutates it in place right before
    # the "start" call specifically, leaving check-kill's own exit code at the
    # default (0) when ITS handle was built a moment earlier.
    spawned_argvs = []
    returncodes: dict[str, int] = {}

    def on_spawn(argv: list[str]) -> None:
        spawned_argvs.append(argv)
        if argv == ["airmon-ng", "start", "wlan0"]:
            returncodes["airmon-ng"] = 1

    proc = FakeProcRunner(
        script={"airmon-ng": AIRMON_START_OUTPUT_RENAMES}, on_spawn=on_spawn, returncodes=returncodes
    )
    rf = RadioController("wlan0", proc)

    reservation = rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)  # must not raise

    assert reservation.adapter == "wlan0mon"  # fallback/parse behavior unaffected
    assert spawned_argvs == [["airmon-ng", "check", "kill"], ["airmon-ng", "start", "wlan0"]]


def test_airmon_stop_nonzero_does_not_raise():
    # Same regression guard as test_airmon_start_nonzero_does_not_raise, for the
    # monitor->managed direction. on_spawn again mutates returncodes in place
    # right before the "stop" call so check-kill/start (earlier in the same
    # argv[0] bucket) keep their default 0 exit code.
    spawned_argvs = []
    returncodes: dict[str, int] = {}

    def on_spawn(argv: list[str]) -> None:
        spawned_argvs.append(argv)
        if argv[:2] == ["airmon-ng", "stop"]:
            returncodes["airmon-ng"] = 1

    proc = FakeProcRunner(
        script={
            "airmon-ng": AIRMON_START_OUTPUT_RENAMES + ["some stop-mode output, content unused"],
            "systemctl": ["Synchronizing state..."],
        },
        on_spawn=on_spawn,
        returncodes=returncodes,
    )
    rf = RadioController("wlan0", proc)
    monitor_reservation = rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)
    rf.release(monitor_reservation)

    managed_reservation = rf.reserve(AdapterMode.MANAGED, JobKind.NMAP_SCAN)  # must not raise

    assert managed_reservation.adapter == "wlan0"
    # systemctl restart still ran after the failed "stop" -- a non-raising
    # warning log doesn't short-circuit the rest of the transition.
    assert spawned_argvs == [
        ["airmon-ng", "check", "kill"],
        ["airmon-ng", "start", "wlan0"],
        ["airmon-ng", "stop", "wlan0mon"],
        ["systemctl", "restart", "NetworkManager"],
    ]
