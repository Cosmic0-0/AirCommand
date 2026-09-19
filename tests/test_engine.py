"""Engine.shutdown() against a real Engine + FakeProcRunner, same style as
test_capture_acceptance.py/test_discovery_acceptance.py: no mocking of core's
own internals, assertions on published events and real object state.

SudoSession.start()/its keepalive loop are exercised for real (not via
FakeProcRunner -- privilege.py talks to subprocess.run/Popen directly, its own
seam, unrelated to Engine's proc= injection), with
aircommand.core.privilege.subprocess.run patched at the module level exactly
like test_privilege.py already does, so shutdown() stopping the real keepalive
thread is genuine proof, not an assumption.
"""

from __future__ import annotations

import sqlite3
import subprocess
import time
from datetime import timedelta
from unittest.mock import patch

import pytest

from aircommand.core.domain import MacAddress, StopReason
from aircommand.core.engine import Engine
from aircommand.core.events import CaptureStopped
from aircommand.core.procutil import FakeProcRunner

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")
PASSWORD = "correct-horse-battery-staple"

# RadioController.reserve() really spawns "airmon-ng" on its first monitor-mode
# use -- see test_capture_acceptance.py's identical constant/comment.
AIRMON_NO_RENAME_OUTPUT = ["monitor mode already enabled on wlan0"]
NO_HANDSHAKE_OUTPUT = ["No valid WPA handshakes found"]

# Real but tiny per-line delay so a still-RUNNING Capture job is genuinely
# still in its loop by the time shutdown() cancels it, without the scripted
# stream ever exhausting on its own first -- same idiom (and same reasoning)
# as test_capture_acceptance.py's _slow_lines/PRE_CANCEL_SETTLE_S.
PRE_SHUTDOWN_SETTLE_S = 0.02


def _slow_lines(count: int, delay_s: float):
    for i in range(count):
        time.sleep(delay_s)
        yield f"CH 6 ][ Elapsed: {i} s ][ 2024-01-01 10:00"


def _completed(returncode: int) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode)


def test_shutdown_on_a_fresh_engine_with_nothing_running_completes_promptly(tmp_path):
    engine = Engine(db_path=":memory:", work_dir=tmp_path, adapter="wlan0", proc=FakeProcRunner(script={}))

    started = time.monotonic()
    engine.shutdown()
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert engine._jobs.active_job_ids() == []
    assert engine.privilege._keepalive_thread is None  # start() was never called
    with pytest.raises(sqlite3.ProgrammingError):
        engine._db._conn.execute("SELECT 1")


@patch("aircommand.core.privilege.subprocess.run")
def test_shutdown_cancels_running_job_stops_privilege_releases_adapter_and_closes_db(mock_run, tmp_path):
    # sudo -k, sudo -S -v, and any (unexpected, given the 75s default keepalive
    # interval) extra keepalive tick all just need to succeed.
    mock_run.return_value = _completed(0)

    airmon_calls = []

    def on_spawn(argv: list[str]) -> None:
        if argv[0] == "airmon-ng":
            airmon_calls.append(argv)

    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={
                "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
                "airodump-ng": _slow_lines(1000, 0.001),
                "aircrack-ng": NO_HANDSHAKE_OUTPUT,
            },
            on_spawn=on_spawn,
        ),
        # Long enough that the handshake-check Pacer never fires during this
        # short test, so the scripted aircrack-ng entry above is defensive only.
        capture_handshake_check_interval=timedelta(seconds=999),
    )

    engine.privilege.start(PASSWORD)
    assert engine.privilege._keepalive_thread.is_alive()

    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    stopped = []
    engine.subscribe(stopped.append, CaptureStopped)
    engine.capture.start_passive(target)
    time.sleep(PRE_SHUTDOWN_SETTLE_S)  # let the driver thread genuinely be
    # mid-loop (and holding the RF reservation) before shutdown() cancels it --
    # see module docstring / test_capture_acceptance.py's identical reasoning.

    engine.shutdown()

    # The job reached a terminal state, and shutdown() waited for it.
    assert engine._jobs.active_job_ids() == []
    assert len(stopped) == 1
    assert stopped[0].reason == StopReason.CANCELLED

    # The keepalive thread actually stopped, not just "stop() returned".
    assert not engine.privilege._keepalive_thread.is_alive()

    # The adapter was released to managed mode -- release() alone (called by
    # Capture._drive's own finally) deliberately does NOT do this, only
    # release_to_managed() does (see rf.py), so seeing "airmon-ng stop" here is
    # proof shutdown() itself made this call, not a side effect of job cleanup.
    assert any(call[:2] == ["airmon-ng", "stop"] for call in airmon_calls)

    # DB closed last.
    with pytest.raises(sqlite3.ProgrammingError):
        engine._db._conn.execute("SELECT 1")
