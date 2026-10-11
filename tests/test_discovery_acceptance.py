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

import logging
import threading
import time
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
STATION_HEADER = "Station MAC, First time seen, Last time seen, Power, # packets, BSSID, Probed ESSIDs"

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


def test_discovery_releases_rf_reservation_if_new_job_raises(tmp_path):
    """Fix 4 regression: start() used to reserve the radio, then call
    self._jobs.new_job() with nothing guarding it -- a raise there (new_job
    already re-raises after its own internal rollback, see jobs.py) skipped
    releasing the reservation entirely, leaking it for the rest of the live
    session: every later start()/start_scan()/start_passive() call would
    then raise AdapterBusy against a reservation nothing is actually using
    anymore. Unlike the _new_connection_scope regression test above, this
    exception fires SYNCHRONOUSLY inside start() itself (new_job() runs
    before the driver thread is ever spawned) -- no thread/excepthook dance
    needed; pytest.raises around the call itself is enough."""
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
    real_new_job = engine.discovery._jobs.new_job
    calls = {"n": 0}

    def raise_on_first_call(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("new_job failed")
        return real_new_job(*args, **kwargs)

    engine.discovery._jobs.new_job = raise_on_first_call

    with pytest.raises(RuntimeError, match="new_job failed"):
        engine.discovery.start()

    # The real assertion: the RF reservation from the failed start() was
    # released -- a second start() right after succeeds rather than raising
    # AdapterBusy.
    second_handle = engine.discovery.start()
    second_handle.cancel()
    second_handle.wait_for_test(timeout=2.0)


# --- Fix 1: airodump-ng must be pinned to --output-format csv ------------------------
# With no --output-format at all, airodump-ng's own default writes far more than
# the one .csv file _poll_csv ever reads (a .cap -- every raw frame heard, from
# ANY network, not just one a Target gates -- plus .kismet.csv/.kismet.netxml/
# .log.csv). Confirmed directly on this machine: a work dir with 41 Discovery
# sessions held 209 files, only the 41 .csv ones ever read.

def test_airodump_is_started_with_output_format_csv_and_nothing_else(tmp_path):
    spawned = []
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
            on_spawn=spawned.append,
            running_polls={"airodump-ng": DISCOVERY_TICK_COUNT},
        ),
        discovery_poll_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )

    handle = engine.discovery.start()
    handle.wait_for_test(timeout=2.0)

    (argv,) = [a for a in spawned if a[0] == "airodump-ng"]
    # "--output-format" and "csv" must be ADJACENT argv tokens, not just both
    # present somewhere.
    assert argv[argv.index("--output-format") + 1] == "csv"
    # No other real airodump-ng output-format name appears anywhere in argv --
    # these are exact-token checks (argv is a list), not substring checks, so
    # an unrelated token (the adapter name, a work-dir-derived path) that
    # happens to CONTAIN one of these strings would not false-positive here.
    assert not any(fmt in argv for fmt in ("cap", "kismet", "pcap", "netxml"))


# --- Fix 2: one bad CSV row must not end the whole Discovery job ---------------------

def _write_bytes_on_spawn(content: bytes):
    """on_spawn callback factory: writes raw bytes (not a str) to the on-disk
    CSV file airodump-ng's --write would produce -- needed for the non-UTF-8
    regression test below, which Path.write_text() (str-only) can't express.
    Guarded on "--write" being present, same reason _write_csv_on_spawn is
    (FakeProcRunner fires on_spawn for every spawn, airmon-ng's own included)."""
    def on_spawn(argv: list[str]) -> None:
        if "--write" not in argv:
            return
        prefix = argv[argv.index("--write") + 1]
        Path(f"{prefix}-01.csv").write_bytes(content)
    return on_spawn


def _write_text_on_spawn(content: str):
    """Same as _write_bytes_on_spawn, for plain str content -- a thin wrapper
    around Path.write_text so each test below doesn't redefine this guard."""
    def on_spawn(argv: list[str]) -> None:
        if "--write" not in argv:
            return
        prefix = argv[argv.index("--write") + 1]
        Path(f"{prefix}-01.csv").write_text(content)
    return on_spawn


def test_poll_csv_survives_a_non_utf8_byte_in_the_ssid_field(tmp_path):
    """Fix 2a regression: csv_path.read_text()'s default strict UTF-8 decode
    raises UnicodeDecodeError on one bad byte anywhere in the file -- a real
    SSID is an arbitrary byte string, not guaranteed valid UTF-8. Uncaught,
    that propagated out of _poll_csv into _drive's own loop, ending the WHOLE
    Discovery job over a single unreadable byte. Confirms the fix
    (errors="replace") doesn't just avoid a crash: BOTH rows in this poll
    tick -- including the one with the bad byte itself, in its SSID field --
    still get discovered."""
    row_with_bad_byte = (
        f"{BSSID_1}, 2024-01-01 10:00:00, 2024-01-01 10:00:05, 6, 54, WPA2, CCMP, PSK, "
        "-40, 10, 0, 0.0.0.0, 4, "
    ).encode() + b"\xff\xfeBadSSID, "
    clean_row = (
        f"{BSSID_2}, 2024-01-01 10:00:00, 2024-01-01 10:00:06, 11, 54, OPN, , , "
        "-55, 8, 0, 0.0.0.0, 4, Net2, "
    ).encode()
    raw_csv = AP_HEADER.encode() + b"\n" + row_with_bad_byte + b"\n" + clean_row + b"\n"

    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
            on_spawn=_write_bytes_on_spawn(raw_csv),
            running_polls={"airodump-ng": DISCOVERY_TICK_COUNT},
        ),
        discovery_poll_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )
    discovered = []
    engine.subscribe(discovered.append, NetworkDiscovered)

    handle = engine.discovery.start()
    handle.wait_for_test(timeout=2.0)  # must not hang/crash over the bad byte

    expected_bssids = {MacAddress.parse(BSSID_1), MacAddress.parse(BSSID_2)}
    assert {e.network.bssid for e in discovered} == expected_bssids


MALFORMED_STATION_LIKE_ROW = (
    "11:22:33:44:55:66, 2024-01-01 10:00:01, 2024-01-01 10:00:02, not_a_channel, 5, "
    "AA:BB:CC:DD:EE:01, ssid1, ssid2, ssid3, ssid4, ssid5, ssid6, ssid7, ssid8"
)
# 14 comma-separated fields (station MAC, first/last seen, power, #packets,
# BSSID, then 8 "probed SSIDs" naively comma-split into separate fields) --
# field 0 is a syntactically-valid-looking MAC (station MACs use the identical
# 6-octet-hex shape as a BSSID), so this passes parse_airodump_csv_line's own
# field-count/MAC-shape filters and then raises ValueError from
# int(fields[3]) ("not_a_channel") rather than returning None.

CSV_WITH_MALFORMED_ROW = (
    "\n".join(
        [
            AP_HEADER,
            f"{BSSID_1}, 2024-01-01 10:00:00, 2024-01-01 10:00:05, 6, 54, WPA2, CCMP, PSK, -40, 10, 0, 0.0.0.0, 4, Net1, ",
            MALFORMED_STATION_LIKE_ROW,
            f"{BSSID_2}, 2024-01-01 10:00:00, 2024-01-01 10:00:06, 11, 54, OPN, , , -55, 8, 0, 0.0.0.0, 4, Net2, ",
        ]
    )
    + "\n"
)


def test_poll_csv_skips_a_malformed_14_plus_field_row_and_logs_a_warning(tmp_path, caplog):
    """Fix 2b regression: a row shaped like MALFORMED_STATION_LIKE_ROW (see its
    own comment) reaches parse_airodump_csv_line's int()/strptime() calls and
    raises ValueError there instead of being filtered out by field count.
    Uncaught, that propagated out of _poll_csv the same way the
    UnicodeDecodeError fix above did -- ending the whole job over one bad row.
    Confirms the fix: the job keeps running (the two real AP rows around the
    bad one both still get discovered) and a WARNING is logged naming the bad
    row."""
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
            on_spawn=_write_text_on_spawn(CSV_WITH_MALFORMED_ROW),
            running_polls={"airodump-ng": DISCOVERY_TICK_COUNT},
        ),
        discovery_poll_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )
    discovered = []
    engine.subscribe(discovered.append, NetworkDiscovered)

    with caplog.at_level(logging.WARNING, logger="aircommand.core.discovery"):
        handle = engine.discovery.start()
        handle.wait_for_test(timeout=2.0)  # must not hang/crash over the bad row

    expected_bssids = {MacAddress.parse(BSSID_1), MacAddress.parse(BSSID_2)}
    assert {e.network.bssid for e in discovered} == expected_bssids

    assert any(
        "unparsable" in record.getMessage() and "11:22:33:44:55:66" in record.getMessage()
        for record in caplog.records
    ), caplog.records


CSV_WITH_STATION_SECTION = (
    "\n".join(
        [
            AP_HEADER,
            f"{BSSID_1}, 2024-01-01 10:00:00, 2024-01-01 10:00:05, 6, 54, WPA2, CCMP, PSK, -40, 10, 0, 0.0.0.0, 4, Net1, ",
            f"{BSSID_2}, 2024-01-01 10:00:00, 2024-01-01 10:00:06, 11, 54, OPN, , , -55, 8, 0, 0.0.0.0, 4, Net2, ",
            "",  # the real blank separator line airodump-ng writes between sections
            STATION_HEADER,
            "11:22:33:44:55:66, 2024-01-01 10:00:01, 2024-01-01 10:00:02, -60, 5, AA:BB:CC:DD:EE:01, ",
        ]
    )
    + "\n"
)


def test_poll_csv_ignores_the_station_section_without_crashing(tmp_path):
    """Fix 2c: confirms the new "break" at the blank separator line (added so
    a REAL station row -- not just the synthetic one fix 2b's test above
    crafts -- never reaches parse_airodump_csv_line at all) doesn't change
    behavior for a real-shaped CSV. Nothing in Discovery has ever consumed
    station rows (test_parse.py's own test_station_data_row_returns_none
    already covers parse_airodump_csv_line's own rejection of one in
    isolation); this confirms the same remains true end-to-end through
    _poll_csv, with a real station-section shape (blank separator, station
    header, one station row) present and simply never reached: exactly 2
    networks discovered, not 3."""
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
            on_spawn=_write_text_on_spawn(CSV_WITH_STATION_SECTION),
            running_polls={"airodump-ng": DISCOVERY_TICK_COUNT},
        ),
        discovery_poll_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )
    discovered = []
    engine.subscribe(discovered.append, NetworkDiscovered)

    handle = engine.discovery.start()
    handle.wait_for_test(timeout=2.0)

    expected_bssids = {MacAddress.parse(BSSID_1), MacAddress.parse(BSSID_2)}
    assert {e.network.bssid for e in discovered} == expected_bssids
    assert len(discovered) == 2  # exactly the 2 AP rows -- the station row never added a 3rd


# --- Fix 3: Cancel, and a crash in the loop, must escalate and confirm termination ----

def test_cancel_escalates_through_terminate_with_escalation_not_a_bare_terminate(tmp_path, monkeypatch):
    """Cancel used to call handle.terminate() directly -- a bare SIGTERM, no
    escalation, no confirmation it actually died. A real aircrack-ng-suite
    process that doesn't honor SIGTERM cleanly (same scenario
    test_procutil.py's own
    test_terminate_with_escalation_escalates_to_kill_when_terminate_is_ignored
    exercises) would then be left running, unattended, after an ordinary
    Pause click. ignore_terminate simulates exactly that: FakeProcHandle.
    poll() reports "still running" regardless of running_polls_remaining
    until kill() is actually called (see _FakeProcHandle's own docstring).

    terminate_with_escalation's default grace periods (3s per SIGTERM/SIGKILL
    attempt) are real wall-clock time -- patched here, at the binding
    discovery.py itself calls through, to a small-grace-period wrapper AROUND
    the real function (not a stub), so this still exercises its genuine
    SIGTERM-wait-SIGKILL logic, just fast.

    The real assertion is call COUNT, not just "it eventually finished": a
    bare, non-escalating handle.terminate() in the cancel branch (the pre-fix
    bug) would leave the process alive (ignore_terminate=True), forcing fix
    3's OTHER half -- the finally-block safety net -- to call
    terminate_with_escalation a SECOND time, which calls handle.terminate()
    again. Exactly one terminate() call proves the cancel branch itself
    already escalated all the way to kill(), with nothing left for the
    finally safety net to do."""
    import aircommand.core.procutil as procutil

    terminate_calls = []
    kill_calls = []
    original_terminate = procutil._FakeProcHandle.terminate
    original_kill = procutil._FakeProcHandle.kill

    def counting_terminate(self):
        terminate_calls.append(self)
        original_terminate(self)

    def counting_kill(self):
        kill_calls.append(self)
        original_kill(self)

    monkeypatch.setattr(procutil._FakeProcHandle, "terminate", counting_terminate)
    monkeypatch.setattr(procutil._FakeProcHandle, "kill", counting_kill)

    def fast_terminate_with_escalation(handle, **kwargs):
        return procutil.terminate_with_escalation(handle, grace_period_s=0.05, tick_s=0.01)

    monkeypatch.setattr("aircommand.core.discovery.terminate_with_escalation", fast_terminate_with_escalation)

    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
            running_polls={"airodump-ng": 10_000},
            ignore_terminate={"airodump-ng"},
        ),
        discovery_poll_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )
    stopped = []
    engine.subscribe(stopped.append, DiscoveryStopped)

    handle = engine.discovery.start()
    handle.cancel()
    handle.wait_for_test(timeout=5.0)

    assert len(terminate_calls) == 1
    assert len(kill_calls) == 1
    assert len(stopped) == 1
    assert stopped[0].reason == StopReason.CANCELLED
    assert stopped[0].error_detail is None  # termination succeeded -- nothing to report as a failure


def test_finally_block_escalates_and_confirms_termination_when_the_loop_crashes(tmp_path, monkeypatch):
    """Fix 3's other half: a crash INSIDE the loop (not a Cancel) must still
    not leave airodump-ng running unattended. Monkeypatches _poll_csv to raise
    on its first call -- the exception propagates straight past the while
    loop into `finally` WITHOUT the Cancel branch ever running (token is
    never cancelled here), so this is a pure test of the finally block's own
    safety net. ignore_terminate (same idiom as the Cancel test above) proves
    genuine escalation to kill(), not just "the fake happened to report
    exited anyway"."""
    import aircommand.core.procutil as procutil

    def fast_terminate_with_escalation(handle, **kwargs):
        return procutil.terminate_with_escalation(handle, grace_period_s=0.05, tick_s=0.01)

    monkeypatch.setattr("aircommand.core.discovery.terminate_with_escalation", fast_terminate_with_escalation)

    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
            running_polls={"airodump-ng": 10_000},
            ignore_terminate={"airodump-ng"},
        ),
        discovery_poll_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )

    def raise_once(csv_path, networks):
        raise RuntimeError("simulated poll crash")

    engine.discovery._poll_csv = raise_once

    stopped = []
    engine.subscribe(stopped.append, DiscoveryStopped)

    # wait_for_test() unblocks the instant mark_terminal() sets its internal
    # event, inside `finally` -- slightly BEFORE the RuntimeError actually
    # finishes propagating out of the thread and hits threading.excepthook
    # (same race test_enumerate_acceptance.py's own equivalent tests document
    # and poll around). Polling briefly here, instead of restoring the hook
    # immediately, keeps pytest's own hook from collecting this expected,
    # scripted exception as a late-arriving PytestUnhandledThreadExceptionWarning.
    thread_exceptions = []
    original_hook = threading.excepthook
    threading.excepthook = thread_exceptions.append
    try:
        handle = engine.discovery.start()
        handle.wait_for_test(timeout=5.0)
        deadline = time.monotonic() + 2.0
        while not thread_exceptions and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        threading.excepthook = original_hook

    assert len(stopped) == 1
    assert stopped[0].reason == StopReason.ERROR
    # Escalation succeeded here (ignore_terminate still responds to kill()) --
    # nothing to report as a stop failure.
    assert stopped[0].error_detail is None
    assert len(thread_exceptions) == 1
    assert thread_exceptions[0].exc_type is RuntimeError


class _NeverDiesHandle:
    """ProcHandle stand-in for a process that survives BOTH SIGTERM and
    SIGKILL -- mirrors test_procutil.py's own _NeverDiesHandle (same
    reasoning: no FakeProcHandle mode expresses this, since every other test
    in this suite relies on FakeProcHandle eventually reporting exited)."""

    def __init__(self) -> None:
        self.pid = 99999
        self.pgid = 99999
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return None

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    def stderr_tail(self) -> list[str]:
        return []


class _InterceptingProcRunner:
    """Delegates to a FakeProcRunner for every argv[0] except "airodump-ng",
    which gets `never_dies_handle` instead -- the seam used to inject a
    ProcHandle shape FakeProcRunner itself can't express (see
    _NeverDiesHandle)."""

    def __init__(self, fake, never_dies_handle):
        self._fake = fake
        self._handle = never_dies_handle

    def spawn(self, argv, *, privileged):
        if argv[0] == "airodump-ng":
            return self._handle
        return self._fake.spawn(argv, privileged=privileged)


def test_finally_block_reports_a_stop_failure_when_escalation_genuinely_fails(tmp_path, monkeypatch):
    """The "only if" half of fix 3's error_detail contract: when the finally
    safety net's own terminate_with_escalation call genuinely can't confirm
    termination (both SIGTERM and SIGKILL survived -- e.g. a cached sudo
    credential that lapsed between spawn and kill, so a privileged kill
    silently failed), DiscoveryStopped's error_detail must say so."""
    import aircommand.core.procutil as procutil

    def fast_terminate_with_escalation(handle, **kwargs):
        return procutil.terminate_with_escalation(handle, grace_period_s=0.05, tick_s=0.01)

    monkeypatch.setattr("aircommand.core.discovery.terminate_with_escalation", fast_terminate_with_escalation)

    never_dies = _NeverDiesHandle()
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=_InterceptingProcRunner(FakeProcRunner(script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT}), never_dies),
        discovery_poll_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )

    def raise_once(csv_path, networks):
        raise RuntimeError("simulated poll crash")

    engine.discovery._poll_csv = raise_once

    stopped = []
    engine.subscribe(stopped.append, DiscoveryStopped)

    # See the previous test's own comment for why this polls briefly rather
    # than restoring the hook immediately.
    thread_exceptions = []
    original_hook = threading.excepthook
    threading.excepthook = thread_exceptions.append
    try:
        handle = engine.discovery.start()
        handle.wait_for_test(timeout=5.0)
        deadline = time.monotonic() + 2.0
        while not thread_exceptions and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        threading.excepthook = original_hook

    assert never_dies.terminate_calls >= 1
    assert never_dies.kill_calls >= 1
    assert len(stopped) == 1
    assert stopped[0].reason == StopReason.ERROR
    assert "airodump-ng could not be stopped" in stopped[0].error_detail
    assert len(thread_exceptions) == 1
    assert thread_exceptions[0].exc_type is RuntimeError


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
