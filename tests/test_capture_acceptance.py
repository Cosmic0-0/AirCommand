"""FakeProcRunner -> airodump-ng/aireplay-ng/aircrack-ng -> HandshakeCaptured /
DeauthFired / CaptureStopped -> SQLite, wired through a real Engine with no GUI,
no root, and no hardware. Same style as test_discovery_acceptance.py: a real
Engine, no mocking of internals, assertions on published events plus repository
reads via engine.capture.list_handshakes()/list_audit_log().

Fixture trap: aircrack-ng's own argv also carries "-w /dev/null" (its wordlist
argument), unrelated to airodump-ng's "-w <cap_path>" capture-file flag -- and
Discovery's own airodump-ng invocation uses "--write-csv" instead of "-w" at
all. _write_cap_file_on_spawn below is guarded on both argv[0] == "airodump-ng"
and "-w" actually being present, so it only ever fires for Capture's own
airodump-ng spawn -- never aircrack-ng's, and never Discovery's (test 4 starts
Discovery first, to hold the RF reservation).

One real-thread-timing subtlety, verified empirically against this repo's
actual threading.Thread/Event behavior (see tests/test_jobs.py for this
codebase's existing comfort with real-thread-timing tests) rather than assumed:

1. Calling handle.cancel() immediately (zero delay) after start_passive()/
   start_deauth_assisted() reliably wins the race against the driver thread's
   very first loop iteration on this system (threading.Thread.start() blocks
   the calling thread until the new thread has begun bootstrapping, which in
   practice hands the new thread the CPU for its whole run -- and with a
   plain, instantly-iterable list of scripted lines, "whole run" reliably
   means the driver thread exhausts every line and reaches its own finally
   block before this thread's next line ever executes, regardless of how many
   lines are scripted). Since the cancellation check comes before the deauth/
   handshake-check logic in the loop body, an immediate cancel would make the
   scripted aircrack-ng/aireplay-ng behavior below never actually get
   exercised -- or, if delayed naively, risks the loop exhausting on its own
   first (reason=ERROR instead of CANCELLED). _slow_lines() below sidesteps
   both failure modes with real (but tiny) per-line delays, so a short real
   sleep before cancel() reliably lands mid-loop: enough real iterations
   happen first to exercise what each scenario is actually meant to prove,
   while the scripted stream is far from exhausted when cancellation lands.
"""

from __future__ import annotations

import hashlib
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

# Content is unused by _drive (see capture.py's `for _line in handle.lines()`) --
# only the count matters. Used only for the handshake-found-on-first-check
# scenario, which breaks out on its very first iteration regardless -- no real
# delay needed there, so a plain, instantly-iterable list is enough.
CAPTURE_NOISE = [f"CH 6 ][ Elapsed: {i} s ][ 2024-01-01 10:00" for i in range(150)]

NO_HANDSHAKE_OUTPUT = ["No valid WPA handshakes found"]
CAP_FILE_BYTES = b"fake-cap-file-bytes-for-sha256-hashing"

# RadioController.reserve() now really spawns "airmon-ng" on its first
# monitor-mode use (docs/roadmap.md Phase 2 item 1) -- every script below needs
# an entry for it or FakeProcRunner KeyErrors. No rename-announcement line, so
# parse_airmon_monitor_interface falls back to the original "wlan0" name,
# matching what every existing assertion in this file already assumes.
AIRMON_NO_RENAME_OUTPUT = ["monitor mode already enabled on wlan0"]

# See module docstring, point 1. Real but tiny -- comfortably shorter than any
# human-perceptible delay, long enough (given _slow_lines' own per-line delay
# below) to let several loop iterations run for real before a same-process
# .cancel() call lands, and comfortably shorter than _slow_lines' own total
# runtime so cancellation always lands well before the scripted stream would
# ever exhaust naturally.
PRE_CANCEL_SETTLE_S = 0.02


def _write_cap_file_on_spawn(argv: list[str]) -> None:
    """Simulates airodump-ng's -w <cap_path> side effect: writes a real file so
    hashlib.sha256(cap_path.read_bytes()) in Capture._drive has something real
    to hash. Guarded on argv[0] == "airodump-ng" first (aircrack-ng's own argv
    also contains "-w", for its unrelated /dev/null wordlist argument) and on
    "-w" actually being present (Discovery's airodump-ng invocation uses
    "--write-csv" instead, and this callback is reused for the AdapterBusy
    test, which starts Discovery before Capture)."""
    if argv[0] != "airodump-ng" or "-w" not in argv:
        return
    cap_path = Path(argv[argv.index("-w") + 1])
    cap_path.write_bytes(CAP_FILE_BYTES)


def _slow_lines(count: int, delay_s: float):
    """A scripted line stream that sleeps for real before each line -- used
    only for Discovery's airodump-ng in the AdapterBusy test (module docstring
    point 1 applies to Discovery's own driver thread too: with a plain list of
    noise lines, Discovery's loop -- nothing here ever cancels it -- reliably
    runs to completion and releases the RF reservation before this test's very
    next line executes). FakeProcRunner only ever iterates whatever's under a
    script key via `yield from`, so any iterable works, not just a list."""
    for i in range(count):
        time.sleep(delay_s)
        yield f"CH 6 ][ Elapsed: {i} s ][ 2024-01-01 10:00"


def _make_engine(tmp_path, script: dict[str, list[str]]) -> Engine:
    return Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(script=script, on_spawn=_write_cap_file_on_spawn),
        # Zero interval: Pacer.due() fires on every check, same trick
        # discovery_poll_interval already uses -- see test_discovery_acceptance.py.
        capture_handshake_check_interval=timedelta(seconds=0),
    )


def test_passive_capture_finds_handshake_on_first_check(tmp_path):
    engine = _make_engine(
        tmp_path,
        script={
            "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
            "airodump-ng": CAPTURE_NOISE[:3],
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
            "airodump-ng": _slow_lines(1000, 0.001),
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
            "airodump-ng": _slow_lines(1000, 0.001),
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
        script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": _slow_lines(30, 0.01)},
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    # Reserves the adapter synchronously inside .start() itself, before
    # Discovery's driver thread even runs -- deterministic. The slow scripted
    # stdout (module docstring point 1) is what keeps Discovery's thread
    # actually still holding that reservation by the time the next line runs.
    engine.discovery.start()

    with pytest.raises(AdapterBusy):
        engine.capture.start_passive(target)


def test_not_a_target_error_when_target_removed_after_fetch(tmp_path):
    # require_target() raises before Capture ever touches the adapter or
    # self._proc, so no tool needs to be scripted at all here.
    engine = _make_engine(tmp_path, script={})
    stale_target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    engine.targets.remove(BSSID_1)

    with pytest.raises(NotATargetError):
        engine.capture.start_passive(stale_target)
