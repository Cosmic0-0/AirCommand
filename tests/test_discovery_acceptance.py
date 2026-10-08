"""FakeProcRunner -> on-disk CSV poll -> parse.py -> NetworkDiscovered -> SQLite,
wired through a real Engine with no GUI, no root, and no hardware. See
docs/design/core-gui-boundary.md 'Usage (caller's view)', headless call site.

airodump-ng writes network data to <prefix>-01.csv on disk, not to stdout --
stdout carries its live interactive display instead (see docs/roadmap.md Phase 1
item 0). _drive's loop doesn't touch handle.lines() content at all anymore
(see discovery.py's own comment on a later, deeper real-hardware finding: even
*gating ticks on stdout arriving* was unreliable) -- it's a plain wall-clock
loop now, driven by ProcHandle.poll() for liveness. These tests feed
"airodump-ng" an empty scripted line list (content is simply never read) and
control how many poll ticks happen via FakeProcRunner's running_polls= instead.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest

from aircommand.core.domain import Band, DiscoveryOptions, MacAddress, StopReason
from aircommand.core.engine import Engine
from aircommand.core.events import DiscoveryStopped, NetworkDiscovered, NetworkSightingUpdated
from aircommand.core.procutil import FakeProcRunner
from aircommand.core.rf import BandUnavailable
from tests.test_rf import IW_2_4_ONLY, IW_DUAL_BAND

BSSID_1 = "AA:BB:CC:DD:EE:01"
BSSID_2 = "AA:BB:CC:DD:EE:02"

AP_HEADER = (
    "BSSID, First time seen, Last time seen, channel, Speed, Privacy, Cipher, "
    "Authentication, Power, # beacons, # IV, LAN IP, ID-length, ESSID, Key"
)

# What airodump-ng actually writes to <prefix>-01.csv -- the on-disk artifact
# _poll_csv reads, rewritten in place each refresh cycle. Two networks, same row
# shape real airodump-ng --write output uses.
CSV_CONTENT = (
    "\n".join(
        [
            AP_HEADER,
            f"{BSSID_1}, 2024-01-01 10:00:00, 2024-01-01 10:00:05, 6, 54, WPA2, CCMP, PSK, -40, 10, 0, 0.0.0.0, 4, Net1, ",
            f"{BSSID_2}, 2024-01-01 10:00:00, 2024-01-01 10:00:06, 11, 54, OPN, , , -55, 8, 0, 0.0.0.0, 4, Net2, ",
        ]
    )
    + "\n"
)

# _drive's loop no longer iterates handle.lines() at all (see discovery.py's
# own comment -- a real-hardware finding that gating CSV polling on the
# spawned tool's own stdout chatter is unreliable). It's a plain wall-clock
# loop instead, driven by ProcHandle.poll() for liveness: FakeProcHandle.poll()
# reports "still running" for exactly this many calls before reporting
# "exited", so this number IS the poll-tick count now (with the zero poll
# interval below, each tick fires the Pacer) -- same role DISCOVERY_NOISE's
# line count used to play, enough to prove "discovered once, then updated on
# every later tick" rather than just once.
DISCOVERY_TICK_COUNT = 4

# RadioController.reserve() now really spawns "airmon-ng" on its first
# monitor-mode use (docs/roadmap.md Phase 2 item 1) -- every script below needs
# an entry for it or FakeProcRunner KeyErrors. No rename-announcement line, so
# parse_airmon_monitor_interface falls back to the original "wlan0" name.
AIRMON_NO_RENAME_OUTPUT = ["monitor mode already enabled on wlan0"]


def _write_csv_on_spawn(argv: list[str]) -> None:
    """Simulates airodump-ng's --write side effect: writes the real on-disk
    artifact _poll_csv reads, at the path airodump-ng itself would use (the
    --write prefix argument, plus airodump-ng's own "-01.csv" suffix
    convention -- see Discovery._drive). Guarded on "--write" actually being
    present since FakeProcRunner now fires on_spawn for every spawn, including
    RadioController's own "airmon-ng start <adapter>" call (docs/roadmap.md
    Phase 2 item 1), which carries no such flag."""
    if "--write" not in argv:
        return
    prefix = argv[argv.index("--write") + 1]
    Path(f"{prefix}-01.csv").write_text(CSV_CONTENT)


def test_discovery_polls_csv_file_not_stdout_for_networks(tmp_path):
    """The regression test. Networks can only appear here because the on-disk
    CSV file was read -- _drive's loop never touches handle.lines() content at
    all now, so this would discover nothing if _poll_csv itself were broken."""
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
            on_spawn=_write_csv_on_spawn,
            running_polls={"airodump-ng": DISCOVERY_TICK_COUNT},
        ),
        # Zero interval: Pacer.due() fires on every tick. Zero tick interval:
        # fast and deterministic, no real sleeping needed to exercise several
        # iterations -- DISCOVERY_TICK_COUNT alone controls how many happen.
        discovery_poll_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )
    discovered = []
    sighting_updates = []
    engine.subscribe(discovered.append, NetworkDiscovered)
    engine.subscribe(sighting_updates.append, NetworkSightingUpdated)

    handle = engine.discovery.start()
    handle.wait_for_test(timeout=2.0)

    expected_bssids = {MacAddress.parse(BSSID_1), MacAddress.parse(BSSID_2)}

    # Both networks discovered, exactly once each -- not once per poll tick.
    assert {e.network.bssid for e in discovered} == expected_bssids
    assert len(discovered) == 2

    # The CSV file is unchanged on every tick after the first read, so every
    # later tick updates the existing sighting instead of re-discovering it.
    assert len(sighting_updates) == 2 * (DISCOVERY_TICK_COUNT - 1)
    assert {u.network.bssid for u in sighting_updates} == expected_bssids

    networks = engine.discovery.list_networks()
    assert {n.bssid for n in networks} == expected_bssids


def test_discovery_survives_csv_file_never_appearing(tmp_path):
    """Robustness: if airodump-ng's CSV file never shows up (no on_spawn writes
    it here), _drive must not crash or hang -- each tick just has nothing to
    report, via _poll_csv's FileNotFoundError->return path."""
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        # no on_spawn -- csv never written
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
            running_polls={"airodump-ng": DISCOVERY_TICK_COUNT},
        ),
        discovery_poll_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )

    handle = engine.discovery.start()
    handle.wait_for_test(timeout=2.0)  # must return well inside the timeout, not hang

    assert engine.discovery.list_networks() == []


def test_discovery_publishes_stopped_with_error_reason_when_airodump_dies_unexpectedly(tmp_path):
    """The gap this closes: until now, airodump-ng dying on its own (crash,
    unplugged adapter, killed externally) published NOTHING -- the GUI had no
    way to learn Discovery had silently stopped. FakeProcHandle.poll() reports
    "exited" after DISCOVERY_TICK_COUNT calls, simulating exactly that.

    Also covers a real-hardware-driven follow-up gap found the same way: an
    ERROR reason alone carried no detail at all about WHY, anywhere in this
    codebase -- error_detail (summarize_stderr() over the dead process's own
    stderr) is what closes that. A real adapter/driver issue wouldn't
    necessarily write anything useful to stderr, so this only asserts the
    plumbing works when the process DOES -- see test_procutil.py for
    summarize_stderr()'s own behavior on empty/blank input."""
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
            running_polls={"airodump-ng": DISCOVERY_TICK_COUNT},
            stderr={"airodump-ng": ["some startup chatter", "fatal: no such device"]},
        ),
        discovery_poll_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )
    stopped = []
    engine.subscribe(stopped.append, DiscoveryStopped)

    handle = engine.discovery.start()
    handle.wait_for_test(timeout=2.0)

    assert len(stopped) == 1
    assert stopped[0].job_id == handle.job_id
    assert stopped[0].reason == StopReason.ERROR
    assert stopped[0].error_detail == "some startup chatter | fatal: no such device"


def test_discovery_publishes_stopped_with_cancelled_reason_when_cancelled(tmp_path):
    """The Pause button's path: cancelling well before airodump-ng would ever
    exit on its own must report CANCELLED, not ERROR."""
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
            # Large enough that cancel() below always wins the race.
            running_polls={"airodump-ng": 10_000},
        ),
        discovery_poll_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )
    stopped = []
    engine.subscribe(stopped.append, DiscoveryStopped)

    handle = engine.discovery.start()
    handle.cancel()
    handle.wait_for_test(timeout=2.0)

    assert len(stopped) == 1
    assert stopped[0].job_id == handle.job_id
    assert stopped[0].reason == StopReason.CANCELLED
    assert stopped[0].error_detail is None  # nothing to explain on the normal Pause-button path


def test_discovery_releases_rf_reservation_even_if_new_connection_scope_raises(tmp_path):
    """ADR-0009 regression: _drive used to call self._new_connection_scope()
    BEFORE its own try:, so a raise there skipped `finally` (the RF release
    inside it included) entirely -- leaking the reservation for the rest of
    the live session, with every later Discovery/Capture/Enumerate start()
    raising AdapterBusy. Proves both halves of the fix: the first job's
    thread still terminates (wait_for_test doesn't hang) and the reservation
    it held is genuinely released -- a second start() right after succeeds
    instead of raising AdapterBusy.
    """
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
            on_spawn=_write_csv_on_spawn,
            running_polls={"airodump-ng": DISCOVERY_TICK_COUNT},
        ),
        discovery_poll_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )
    real_new_connection_scope = engine.discovery._new_connection_scope
    calls = {"n": 0}

    def raise_on_first_call(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection scope failed")
        return real_new_connection_scope(*args, **kwargs)

    engine.discovery._new_connection_scope = raise_on_first_call

    # No except clause in Discovery._drive -- the raise above propagates out of
    # the (daemon) thread uncaught, same as any other unexpected exception
    # there. Suppress the default excepthook's traceback spam for this
    # expected-and-scripted case, same pattern test_enumerate_acceptance.py's
    # own failure test already uses.
    original_hook = threading.excepthook
    threading.excepthook = lambda args: None
    try:
        first_handle = engine.discovery.start()
        first_handle.wait_for_test(timeout=2.0)  # must not hang
    finally:
        threading.excepthook = original_hook

    # The real assertion: RadioController's reservation from the first (failed)
    # job was released -- a second start() right after succeeds rather than
    # raising AdapterBusy.
    second_handle = engine.discovery.start()
    second_handle.cancel()
    second_handle.wait_for_test(timeout=2.0)


# --- Band selection (ADR-0013) --------------------------------------------------------

BOTH_BANDS = frozenset({Band.GHZ_2_4, Band.GHZ_5})


def _band_engine(tmp_path, monkeypatch, iw_lines, spawned):
    """An Engine whose adapter's phy resolves to phy3 and whose `iw` output is
    `iw_lines`; every spawned argv is appended to `spawned`."""
    monkeypatch.setattr("aircommand.core.rf._phy_name", lambda adapter: "phy3")
    return Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": [], "iw": iw_lines},
            on_spawn=spawned.append,
            running_polls={"airodump-ng": 10_000},
        ),
        discovery_poll_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )


def _airodump_argv(spawned):
    (argv,) = [a for a in spawned if a[0] == "airodump-ng"]
    return argv


def _run_to_completion(engine, options=None):
    handle = engine.discovery.start(options) if options is not None else engine.discovery.start()
    handle.cancel()
    handle.wait_for_test(timeout=2.0)


@pytest.mark.parametrize(
    "bands, expected_flag",
    [
        (frozenset({Band.GHZ_2_4}), "bg"),
        (frozenset({Band.GHZ_5}), "a"),
        (BOTH_BANDS, "abg"),
    ],
    ids=["2.4GHz", "5GHz", "both"],
)
def test_airodump_is_started_with_the_band_flag_for_the_chosen_bands(tmp_path, monkeypatch, bands, expected_flag):
    spawned = []
    engine = _band_engine(tmp_path, monkeypatch, IW_DUAL_BAND, spawned)

    _run_to_completion(engine, DiscoveryOptions(bands=bands))

    argv = _airodump_argv(spawned)
    assert argv[argv.index("--band") + 1] == expected_flag
    assert argv[-1] == "wlan0"   # the adapter is still the last argument
    assert argv.index("--band") < argv.index("--write")


def test_default_options_scan_2_4ghz_exactly_as_before_band_selection_existed(tmp_path, monkeypatch):
    spawned = []
    engine = _band_engine(tmp_path, monkeypatch, IW_DUAL_BAND, spawned)

    _run_to_completion(engine)   # no options at all

    argv = _airodump_argv(spawned)
    assert argv[argv.index("--band") + 1] == "bg"   # airodump-ng's own default band
    assert not any(a[0] == "iw" for a in spawned)   # 2.4GHz is never capability-checked


def test_start_refuses_5ghz_on_a_2_4ghz_only_adapter_before_touching_the_radio(tmp_path, monkeypatch):
    spawned = []
    engine = _band_engine(tmp_path, monkeypatch, IW_2_4_ONLY, spawned)

    with pytest.raises(BandUnavailable, match="5 GHz"):
        engine.discovery.start(DiscoveryOptions(bands=frozenset({Band.GHZ_5})))

    assert [a[0] for a in spawned] == ["iw"]   # no airmon-ng, no airodump-ng: nothing reserved

    # ...and nothing leaked: a plain 2.4GHz start right after still gets the radio.
    _run_to_completion(engine)
    assert _airodump_argv(spawned)[2] == "bg"


def test_start_refuses_both_bands_on_a_2_4ghz_only_adapter(tmp_path, monkeypatch):
    spawned = []
    engine = _band_engine(tmp_path, monkeypatch, IW_2_4_ONLY, spawned)
    with pytest.raises(BandUnavailable, match="5 GHz"):
        engine.discovery.start(DiscoveryOptions(bands=BOTH_BANDS))


def test_start_refuses_5ghz_rather_than_guessing_when_capabilities_cant_be_read(tmp_path, monkeypatch):
    spawned = []
    engine = _band_engine(tmp_path, monkeypatch, ["unparseable"], spawned)
    with pytest.raises(BandUnavailable):
        engine.discovery.start(DiscoveryOptions(bands=frozenset({Band.GHZ_5})))
    assert not any(a[0] == "airmon-ng" for a in spawned)


def test_2_4ghz_still_starts_when_capabilities_cant_be_read(tmp_path, monkeypatch):
    """A broken `iw` must never take away the scan Discovery has always done."""
    spawned = []
    engine = _band_engine(tmp_path, monkeypatch, ["unparseable"], spawned)
    _run_to_completion(engine, DiscoveryOptions(bands=frozenset({Band.GHZ_2_4})))
    assert _airodump_argv(spawned)[2] == "bg"


def test_discovery_exposes_the_adapters_supported_bands(tmp_path, monkeypatch):
    engine = _band_engine(tmp_path, monkeypatch, IW_DUAL_BAND, [])
    assert engine.discovery.supported_bands() == BOTH_BANDS
