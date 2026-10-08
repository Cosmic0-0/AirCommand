import pytest

from aircommand.core.domain import Band, JobKind
from aircommand.core.procutil import FakeProcRunner
from aircommand.core.rf import AdapterBusy, AdapterMode, BandUnavailable, RadioCommandFailed, RadioController

# Realistic (researched, not hardware-captured) airmon-ng rename-announcement
# line -- see parse.py's parse_airmon_monitor_interface, already tested against
# this exact shape plus a no-rename/fallback case. Used here as the default
# script response so reserve()'s first monitor-mode call has something to parse
# instead of KeyError-ing on an empty script (see note below).
AIRMON_START_OUTPUT_RENAMES = ["(mac80211 monitor mode vif enabled for [phy0]wlan0 on [phy0]wlan0mon)"]

# Real `iw dev <adapter> info` output, hardware-confirmed (ADR-0016): captured
# directly from a real USB adapter genuinely left in monitor mode by a prior
# AirCommand process that never reached Engine.shutdown() (a force-kill --
# see ADR-0014's own hang this exact adapter was stuck from). The managed-mode
# shape was confirmed the same way, from a real managed-mode interface.
IW_DEV_INFO_MONITOR = [
    "Interface wlan0",
    "\tifindex 4",
    "\twdev 0x200000001",
    "\taddr 5c:62:8b:9f:aa:9d",
    "\ttype monitor",
    "\tchannel 108 (5540 MHz), width: 20 MHz (no HT), center1: 5540 MHz",
]
IW_DEV_INFO_MANAGED = [
    "Interface wlan0",
    "\tifindex 3",
    "\twdev 0x1",
    "\taddr e0:0a:f6:b0:7d:7b",
    "\ttype managed",
    "\tchannel 6 (2437 MHz), width: 20 MHz, center1: 2437 MHz",
]


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


def test_hard_blocked_adapter_raises_before_any_spawn(monkeypatch):
    spawned_argvs = []
    monkeypatch.setattr("aircommand.core.rf._is_hard_blocked", lambda adapter: True)
    rc = RadioController("wlan0", FakeProcRunner(script={}, on_spawn=spawned_argvs.append))
    with pytest.raises(RadioCommandFailed, match="hard-blocked"):
        rc.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)
    assert spawned_argvs == []


# --- supported_bands() (ADR-0013) -----------------------------------------------------

# Trimmed from a real `iw phy phy3 info` run on 2026-10-08 (a dual-band USB adapter);
# parse_iw_phy_bands has its own, fuller tests in test_parse.py.
IW_DUAL_BAND = [
    "Wiphy phy3",
    "\tBand 1:",
    "\t\tFrequencies:",
    "\t\t\t* 2412.0 MHz [1] (20.0 dBm)",
    "\t\t\t* 2484.0 MHz [14] (disabled)",
    "\tBand 2:",
    "\t\tFrequencies:",
    "\t\t\t* 5180.0 MHz [36] (24.0 dBm)",
    "\t\t\t* 5260.0 MHz [52] (24.0 dBm) (radar detection)",
]
IW_2_4_ONLY = IW_DUAL_BAND[:5]


class _RecordingRunner:
    """Delegates to a FakeProcRunner but records (argv, privileged) for every
    spawn -- FakeProcRunner's own on_spawn hook doesn't see `privileged`, and
    supported_bands() must be unprivileged. Optionally raises on spawn, to
    simulate a binary that isn't installed."""

    def __init__(self, script, raises=None):
        self.calls = []
        self._raises = raises
        self._inner = FakeProcRunner(script=script)

    def spawn(self, argv, *, privileged):
        self.calls.append((list(argv), privileged))
        if self._raises is not None and argv[0] == "iw":
            raise self._raises
        return self._inner.spawn(argv, privileged=privileged)


@pytest.fixture
def phy3(monkeypatch):
    """Records which interface name _phy_name was asked about, and resolves any to phy3."""
    asked = []
    monkeypatch.setattr("aircommand.core.rf._phy_name", lambda adapter: asked.append(adapter) or "phy3")
    return asked


def test_supported_bands_runs_iw_phy_info_unprivileged_and_parses_both_bands(phy3):
    runner = _RecordingRunner({"iw": IW_DUAL_BAND})
    rf = RadioController("wlan0", runner)

    assert rf.supported_bands() == frozenset({Band.GHZ_2_4, Band.GHZ_5})

    # `iw phy <name> info`, NOT `iw list`: that prints every phy on the machine, including
    # the laptop's own card, and would offer bands the adapter doesn't have.
    assert runner.calls == [(["iw", "phy", "phy3", "info"], False)]


def test_supported_bands_reports_a_2_4ghz_only_adapter_honestly(phy3):
    rf = RadioController("wlan0", _RecordingRunner({"iw": IW_2_4_ONLY}))
    assert rf.supported_bands() == frozenset({Band.GHZ_2_4})


def test_supported_bands_needs_no_reservation_and_touches_no_radio_mode(phy3):
    runner = _RecordingRunner({"iw": IW_DUAL_BAND})
    rf = RadioController("wlan0", runner)
    rf.supported_bands()
    assert [argv[0] for argv, _ in runner.calls] == ["iw"]   # no airmon-ng, no systemctl
    assert phy3 == ["wlan0"]   # asked about the original (managed-mode) name


def test_supported_bands_asks_about_the_monitor_interface_once_airmon_renamed_it(phy3):
    """Once airmon-ng has renamed the adapter, the original name no longer exists
    (sysfs has nothing under it), so the phy must be looked up by the new one."""
    runner = _RecordingRunner({"airmon-ng": AIRMON_START_OUTPUT_RENAMES, "iw": IW_DUAL_BAND})
    rf = RadioController("wlan0", runner)
    rf.reserve(AdapterMode.MONITOR_HOPPING, JobKind.DISCOVERY)

    rf.supported_bands()

    assert phy3 == ["wlan0mon"]


def test_supported_bands_is_cached_after_a_success(phy3):
    runner = _RecordingRunner({"iw": IW_DUAL_BAND})
    rf = RadioController("wlan0", runner)
    rf.supported_bands()
    rf.supported_bands()
    assert len(runner.calls) == 1


def test_supported_bands_raises_when_the_phy_cant_be_found_and_spawns_nothing(monkeypatch):
    monkeypatch.setattr("aircommand.core.rf._phy_name", lambda adapter: None)
    runner = _RecordingRunner({"iw": IW_DUAL_BAND})
    rf = RadioController("wlan0", runner)
    with pytest.raises(BandUnavailable, match="wlan0"):
        rf.supported_bands()
    assert runner.calls == []


def test_supported_bands_raises_when_iw_is_not_installed(phy3):
    rf = RadioController("wlan0", _RecordingRunner({}, raises=FileNotFoundError("iw")))
    with pytest.raises(BandUnavailable, match="iw"):
        rf.supported_bands()


def test_supported_bands_raises_with_stderr_when_iw_fails(phy3):
    runner = FakeProcRunner(
        script={"iw": []}, returncodes={"iw": 237}, stderr={"iw": ["command failed: No such device (-19)"]}
    )
    rf = RadioController("wlan0", runner)
    with pytest.raises(BandUnavailable, match="No such device"):
        rf.supported_bands()


def test_supported_bands_raises_rather_than_guessing_when_nothing_parses(phy3):
    rf = RadioController("wlan0", FakeProcRunner(script={"iw": ["some unrelated output"]}))
    with pytest.raises(BandUnavailable, match="no usable"):
        rf.supported_bands()


def test_a_failed_query_is_not_cached(phy3):
    script = {"iw": []}
    runner = FakeProcRunner(script=script)
    rf = RadioController("wlan0", runner)
    with pytest.raises(BandUnavailable):
        rf.supported_bands()

    script["iw"] = IW_DUAL_BAND   # e.g. the operator replugged the adapter
    assert rf.supported_bands() == frozenset({Band.GHZ_2_4, Band.GHZ_5})


# --- ADR-0016: syncing with the REAL adapter mode -------------------------------
# The actual bug this fixes: self._monitor_adapter is purely in-memory, so a
# fresh RadioController (e.g. a new AirCommand process after a prior one was
# force-killed while the real adapter was in monitor mode) defaults to "assume
# managed" with nothing ever checking that against reality. Confirmed directly
# on real hardware, not just reasoned about: `iw dev` genuinely showed the
# adapter still in monitor mode with no AirCommand process running at all.
# Enumerate (reserve(MANAGED, ...)) would then skip the real mode switch
# entirely and fail reading an IP off an interface that was never going to
# have one -- the exact OSError[Errno 99] a genuinely-not-yet-joined network
# would also produce, for a completely different reason. The check is scoped
# to this ONE combination (MANAGED requested, self._monitor_adapter already
# reads None) -- every other reserve()/supported_bands()/release_to_managed()
# call is completely unaffected, confirmed by every test above this section
# passing unmodified.

def test_reserve_managed_detects_and_corrects_a_real_adapter_stuck_in_monitor_mode():
    """The core fix: self._monitor_adapter starts as None (the old, blind
    "assume managed" default) but the REAL adapter is actually in monitor
    mode -- reserve(MANAGED, ...) must perform the real stop+restart
    sequence, not silently skip it the way it did before ADR-0016."""
    spawned_argvs = []
    proc = FakeProcRunner(
        script={"iw": IW_DEV_INFO_MONITOR, "airmon-ng": ["some stop-mode output, content unused"],
                "systemctl": ["Synchronizing state..."]},
        on_spawn=spawned_argvs.append,
    )
    rf = RadioController("wlan0", proc)

    managed_reservation = rf.reserve(AdapterMode.MANAGED, JobKind.NMAP_SCAN)

    assert spawned_argvs == [
        ["iw", "dev", "wlan0", "info"],       # the new check finds "type monitor"
        ["airmon-ng", "stop", "wlan0"],       # ...and for real switches it back
        ["systemctl", "restart", "NetworkManager"],
    ]
    assert managed_reservation.adapter == "wlan0"


def test_reserve_managed_on_a_genuinely_managed_adapter_is_unaffected():
    """Baseline: when the real adapter actually IS in managed mode (the common
    case), the check finds nothing to correct and reserve(MANAGED) behaves
    exactly as it always did beyond the one extra read -- no airmon-ng/
    systemctl call, since there was never anything to switch."""
    spawned_argvs = []
    proc = FakeProcRunner(script={"iw": IW_DEV_INFO_MANAGED}, on_spawn=spawned_argvs.append)
    rf = RadioController("wlan0", proc)

    reservation = rf.reserve(AdapterMode.MANAGED, JobKind.NMAP_SCAN)

    assert spawned_argvs == [["iw", "dev", "wlan0", "info"]]
    assert reservation.adapter == "wlan0"


def test_reserve_managed_rechecks_reality_every_time_self_monitor_adapter_reads_none():
    """Deliberately NOT cached/run-once (see rf.py's own comment on
    _real_adapter_is_in_monitor_mode): two separate reserve(MANAGED, ...)
    calls that both see self._monitor_adapter as None each re-check reality --
    self-healing even if something external changes the adapter's mode again
    between them, not just on the very first call."""
    spawned_argvs = []
    proc = FakeProcRunner(script={"iw": IW_DEV_INFO_MANAGED}, on_spawn=spawned_argvs.append)
    rf = RadioController("wlan0", proc)

    first = rf.reserve(AdapterMode.MANAGED, JobKind.NMAP_SCAN)
    rf.release(first)
    second = rf.reserve(AdapterMode.MANAGED, JobKind.NMAP_SCAN)

    assert spawned_argvs == [["iw", "dev", "wlan0", "info"], ["iw", "dev", "wlan0", "info"]]
    assert second.adapter == "wlan0"


def test_reserve_managed_tolerates_iw_not_installed():
    """Detection failing must never introduce a NEW way for reserve() to
    raise -- it's purely corrective; "can't tell" just preserves the
    pre-ADR-0016 default (assume managed) rather than blocking anything."""

    class _RaisingRunner:
        def spawn(self, argv, *, privileged):
            if argv[0] == "iw":
                raise FileNotFoundError("iw")
            return FakeProcRunner(script={}).spawn(argv, privileged=privileged)

    rf = RadioController("wlan0", _RaisingRunner())

    reservation = rf.reserve(AdapterMode.MANAGED, JobKind.NMAP_SCAN)   # must not raise

    assert reservation.adapter == "wlan0"   # unchanged pre-ADR-0016 default


def test_reserve_managed_tolerates_a_failed_iw_dev_call():
    """Same tolerance, the other failure shape: `iw dev` runs but exits
    non-zero (e.g. "no such device", a renamed/missing interface this check
    doesn't attempt to solve -- see rf.py's own comment) -- still just
    preserves the pre-ADR-0016 default rather than raising."""
    proc = FakeProcRunner(
        script={"iw": []}, returncodes={"iw": 237}, stderr={"iw": ["command failed: No such device (-19)"]}
    )
    rf = RadioController("wlan0", proc)

    reservation = rf.reserve(AdapterMode.MANAGED, JobKind.NMAP_SCAN)   # must not raise

    assert reservation.adapter == "wlan0"
