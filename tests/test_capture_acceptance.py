"""FakeProcRunner -> airodump-ng/aireplay-ng/aircrack-ng -> HandshakeCaptured /
DeauthFired / CaptureStopped -> SQLite, wired through a real Engine with no GUI,
no root, and no hardware. Same style as test_discovery_acceptance.py: a real
Engine, no mocking of internals, assertions on published events plus repository
reads via engine.capture.list_handshakes()/list_audit_log().

Fixture trap: aircrack-ng's own argv also carries "-w /dev/null" (its wordlist
argument), unrelated to airodump-ng's "-w <cap_path>" capture-file flag -- and
Discovery's own airodump-ng invocation uses "--write" (long form) instead of
"-w" at all. _write_cap_file_on_spawn below is guarded on both argv[0] ==
"airodump-ng" and "-w" actually being present, so it only ever fires for
Capture's own airodump-ng spawn -- never aircrack-ng's, and never Discovery's
(test 4 starts Discovery first, to hold the RF reservation).

One real-thread-timing subtlety, verified empirically against this repo's
actual threading.Thread/Event behavior (see tests/test_jobs.py for this
codebase's existing comfort with real-thread-timing tests) rather than assumed:

1. _drive's main loop no longer iterates handle.lines() at all (see capture.py's
   own comment -- a real-hardware finding that gating cancellation/deauth/
   handshake-check timing on the spawned tool's own stdout chatter is
   unreliable). It's a plain wall-clock loop instead, driven by
   ProcHandle.poll() for liveness. FakeProcHandle.poll() reports "already
   exited" immediately unless told to report "still running" for a number of
   polls first (running_polls=, see procutil.py) -- so every script below
   gives "airodump-ng" a generous running_polls budget (plenty of margin for
   whatever each test needs to exercise) and _make_engine passes a tiny
   drive_tick_interval, so that budget covers comfortably more real wall-clock
   time than any test actually waits, without making the suite slow. A test
   that wants the loop to keep going past its very first check (anything
   beyond "finds the handshake on the first check") needs running_polls of at
   least 2; this file just uses one generous shared value throughout.
"""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from aircommand.core.allowlist import NotATargetError
from aircommand.core.domain import DeauthOptions, HandshakeKind, MacAddress, StopReason
from aircommand.core.engine import Engine
from aircommand.core.events import CaptureStopped, DeauthFired, HandshakeCaptured
from aircommand.core.procutil import FakeProcRunner
from aircommand.core.rf import AdapterBusy

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")

NO_HANDSHAKE_OUTPUT = ["No valid WPA handshakes found"]
CAP_FILE_BYTES = b"fake-cap-file-bytes-for-sha256-hashing"

# RadioController.reserve() now really spawns "airmon-ng" on its first
# monitor-mode use (docs/roadmap.md Phase 2 item 1) -- every script below needs
# an entry for it or FakeProcRunner KeyErrors. No rename-announcement line, so
# parse_airmon_monitor_interface falls back to the original "wlan0" name,
# matching what every existing assertion in this file already assumes.
AIRMON_NO_RENAME_OUTPUT = ["monitor mode already enabled on wlan0"]

# See module docstring, point 1. Generous shared running_polls budget (times
# DRIVE_TICK_INTERVAL below) for every "airodump-ng" entry in this file --
# comfortably more real time than any test actually waits before cancelling,
# without making the suite slow (each tick is 1ms).
AIRODUMP_RUNNING_POLLS = 1000
DRIVE_TICK_INTERVAL = timedelta(seconds=0.001)

# Real but tiny -- comfortably shorter than any human-perceptible delay, and
# than AIRODUMP_RUNNING_POLLS * DRIVE_TICK_INTERVAL's own ~1s budget, so
# cancellation always lands well before the fake process would ever "exit" on
# its own.
PRE_CANCEL_SETTLE_S = 0.02


def _write_cap_file_on_spawn(argv: list[str]) -> None:
    """Simulates airodump-ng's -w <cap_path> side effect: writes a real file so
    hashlib.sha256(cap_path.read_bytes()) in Capture._drive has something real
    to hash. Guarded on argv[0] == "airodump-ng" first (aircrack-ng's own argv
    also contains "-w", for its unrelated /dev/null wordlist argument) and on
    "-w" actually being present (Discovery's airodump-ng invocation uses
    "--write" instead, and this callback is reused for the AdapterBusy test,
    which starts Discovery before Capture)."""
    if argv[0] != "airodump-ng" or "-w" not in argv:
        return
    cap_path = Path(argv[argv.index("-w") + 1])
    cap_path.write_bytes(CAP_FILE_BYTES)


def _make_engine(tmp_path, script: dict[str, list[str]]) -> Engine:
    return Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script=script, on_spawn=_write_cap_file_on_spawn,
            running_polls={"airodump-ng": AIRODUMP_RUNNING_POLLS},
        ),
        # Zero interval: Pacer.due() fires on every check, same trick
        # discovery_poll_interval already uses -- see test_discovery_acceptance.py.
        capture_handshake_check_interval=timedelta(seconds=0),
        drive_tick_interval=DRIVE_TICK_INTERVAL,
    )


def test_passive_capture_finds_handshake_on_first_check(tmp_path):
    engine = _make_engine(
        tmp_path,
        script={
            "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
            "airodump-ng": [],  # content unused -- see module docstring point 1
            "aircrack-ng": ["   1  AA:BB:CC:DD:EE:01  Test-SSID              WPA (1 handshake)"],
        },
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    handshakes_captured = []
    stopped = []
    engine.subscribe(handshakes_captured.append, HandshakeCaptured)
    engine.subscribe(stopped.append, CaptureStopped)

    handle = engine.capture.start_passive(target)
    handle.wait_for_test(timeout=2.0)

    assert len(handshakes_captured) == 1
    handshake = handshakes_captured[0].handshake
    assert handshake.target_id == target.id
    assert handshake.bssid == target.bssid
    assert handshake.kind == HandshakeKind.WPA2_EAPOL
    assert handshake.cap_file_sha256 == hashlib.sha256(CAP_FILE_BYTES).hexdigest()

    assert engine.capture.list_handshakes(target) == [handshake]

    assert len(stopped) == 1
    assert stopped[0].reason == StopReason.COMPLETED


def test_passive_capture_never_finds_handshake_gets_cancelled(tmp_path):
    engine = _make_engine(
        tmp_path,
        script={
            "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
            "airodump-ng": [],  # content unused -- see module docstring point 1
            "aircrack-ng": NO_HANDSHAKE_OUTPUT,
        },
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    handshakes_captured = []
    stopped = []
    engine.subscribe(handshakes_captured.append, HandshakeCaptured)
    engine.subscribe(stopped.append, CaptureStopped)

    handle = engine.capture.start_passive(target)
    time.sleep(PRE_CANCEL_SETTLE_S)  # let real iterations (and real aircrack-ng
    # checks, always scripted "not found") happen before cancelling -- see
    # module docstring point 1.
    handle.cancel()
    handle.wait_for_test(timeout=2.0)

    assert handshakes_captured == []
    assert len(stopped) == 1
    assert stopped[0].reason == StopReason.CANCELLED


def test_deauth_assisted_capture_respects_max_bursts(tmp_path):
    engine = _make_engine(
        tmp_path,
        script={
            "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
            "airodump-ng": [],  # content unused -- see module docstring point 1
            "aircrack-ng": NO_HANDSHAKE_OUTPUT,
            "aireplay-ng": [],  # only .wait()'d, never .lines()'d -- see capture.py
        },
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    deauths_fired = []
    engine.subscribe(deauths_fired.append, DeauthFired)

    handle = engine.capture.start_deauth_assisted(
        target, DeauthOptions(interval=timedelta(seconds=0), burst_size=5, max_bursts=2)
    )
    time.sleep(PRE_CANCEL_SETTLE_S)  # let several loop iterations happen for
    # real first -- see module docstring point 1 -- so the max_bursts cap (not
    # an accidentally-never-fired burst) is what this test actually exercises.
    handle.cancel()
    handle.wait_for_test(timeout=2.0)

    # DeauthFired is published mid-loop, strictly before the loop can exit --
    # by the time wait_for_test() unblocks, the loop has already exited, so
    # this count (unlike CaptureStopped's) needs no extra settling.
    assert len(deauths_fired) == 2

    audit_entries = engine.capture.list_audit_log(target)
    assert len(audit_entries) == 2
    assert all(entry.frame_count == 5 for entry in audit_entries)


def test_adapter_busy_propagates_synchronously_and_does_not_start_a_job(tmp_path):
    engine = _make_engine(
        tmp_path,
        script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    # Reserves the adapter synchronously inside .start() itself, before
    # Discovery's driver thread even runs -- deterministic. AIRODUMP_RUNNING_POLLS
    # (module docstring point 1) is what keeps Discovery's thread actually still
    # holding that reservation by the time the next line runs.
    engine.discovery.start()

    with pytest.raises(AdapterBusy):
        engine.capture.start_passive(target)


def test_capture_releases_rf_reservation_even_if_new_connection_scope_raises(tmp_path):
    """ADR-0009 regression: _drive used to call self._new_connection_scope()
    BEFORE its own try:, so a raise there skipped `finally` (the RF release
    inside it included) entirely -- leaking the reservation for the rest of
    the live session. Proves both halves of the fix: the first job's thread
    still terminates (wait_for_test doesn't hang) and the reservation it held
    is genuinely released -- a second start_passive() right after succeeds
    instead of raising AdapterBusy.
    """
    engine = _make_engine(
        tmp_path,
        script={
            "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
            "airodump-ng": [],  # content unused -- see module docstring point 1
            "aircrack-ng": NO_HANDSHAKE_OUTPUT,
        },
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    real_new_connection_scope = engine.capture._new_connection_scope
    calls = {"n": 0}

    def raise_on_first_call(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection scope failed")
        return real_new_connection_scope(*args, **kwargs)

    engine.capture._new_connection_scope = raise_on_first_call

    # No except clause in Capture._drive -- the raise above propagates out of
    # the (daemon) thread uncaught, same as any other unexpected exception
    # there. Suppress the default excepthook's traceback spam for this
    # expected-and-scripted case, same pattern test_enumerate_acceptance.py's
    # own failure test already uses.
    original_hook = threading.excepthook
    threading.excepthook = lambda args: None
    try:
        first_handle = engine.capture.start_passive(target)
        first_handle.wait_for_test(timeout=2.0)  # must not hang
    finally:
        threading.excepthook = original_hook

    # The real assertion: RadioController's reservation from the first (failed)
    # job was released -- a second start_passive() right after succeeds rather
    # than raising AdapterBusy.
    second_handle = engine.capture.start_passive(target)
    second_handle.cancel()
    second_handle.wait_for_test(timeout=2.0)


def test_not_a_target_error_when_target_removed_after_fetch(tmp_path):
    # require_target() raises before Capture ever touches the adapter or
    # self._proc, so no tool needs to be scripted at all here.
    engine = _make_engine(tmp_path, script={})
    stale_target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    engine.targets.remove(BSSID_1)

    with pytest.raises(NotATargetError):
        engine.capture.start_passive(stale_target)
