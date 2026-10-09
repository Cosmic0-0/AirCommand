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
from aircommand.core.rf import RadioController

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")
PASSWORD = "correct-horse-battery-staple"


# --- ADR-0017: adapter becomes optional, Engine.radio is the new public facade --

def test_engine_constructs_with_no_adapter_selected_yet_and_exposes_radio(tmp_path):
    engine = Engine(db_path=":memory:", work_dir=tmp_path, adapter=None, proc=FakeProcRunner(script={}))

    assert isinstance(engine.radio, RadioController)
    assert engine.radio.selected_adapter is None


def test_engine_still_constructs_with_an_adapter_pre_selected(tmp_path):
    # Same call shape every other test in this file already uses -- optional
    # with a real value passed is identical to the old required-arg behavior.
    engine = Engine(db_path=":memory:", work_dir=tmp_path, adapter="wlan0", proc=FakeProcRunner(script={}))

    assert isinstance(engine.radio, RadioController)
    assert engine.radio.selected_adapter == "wlan0"

# RadioController.reserve() really spawns "airmon-ng" on its first monitor-mode
# use -- see test_capture_acceptance.py's identical constant/comment.
AIRMON_NO_RENAME_OUTPUT = ["monitor mode already enabled on wlan0"]
NO_HANDSHAKE_OUTPUT = ["No valid WPA handshakes found"]

# Real but tiny tick interval, and a generous running_polls budget for
# "airodump-ng" below, so a still-RUNNING Capture job is genuinely still in its
# loop by the time shutdown() cancels it, without the fake process ever
# "exiting" on its own first -- same idiom (and same reasoning) as
# test_capture_acceptance.py's AIRODUMP_RUNNING_POLLS/DRIVE_TICK_INTERVAL/
# PRE_CANCEL_SETTLE_S (Capture's _drive loop is a plain wall-clock loop now,
# driven by ProcHandle.poll() for liveness, not handle.lines() content -- see
# that file's module docstring for the full real-hardware finding).
PRE_SHUTDOWN_SETTLE_S = 0.02
AIRODUMP_RUNNING_POLLS = 1000
DRIVE_TICK_INTERVAL = timedelta(seconds=0.001)


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
    systemctl_calls = []

    def on_spawn(argv: list[str]) -> None:
        if argv[0] == "airmon-ng":
            airmon_calls.append(argv)
        elif argv[0] == "systemctl":
            systemctl_calls.append(argv)

    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={
                "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
                "airodump-ng": [],  # content unused -- see AIRODUMP_RUNNING_POLLS's comment
                "aircrack-ng": NO_HANDSHAKE_OUTPUT,
                "systemctl": ["Synchronizing state..."],
            },
            on_spawn=on_spawn,
            running_polls={"airodump-ng": AIRODUMP_RUNNING_POLLS},
        ),
        # Long enough that the handshake-check Pacer never fires during this
        # short test, so the scripted aircrack-ng entry above is defensive only.
        capture_handshake_check_interval=timedelta(seconds=999),
        drive_tick_interval=DRIVE_TICK_INTERVAL,
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

    # NetworkManager comes back the moment the adapter returns to managed mode
    # -- this IS "when the app closes, NetworkManager needs to be reverted"
    # (docs/adr/0005-networkmanager-check-kill.md), proven end-to-end through a
    # real Engine.shutdown(), not just unit-tested on RadioController alone.
    assert any(call == ["systemctl", "restart", "NetworkManager"] for call in systemctl_calls)

    # DB closed last.
    with pytest.raises(sqlite3.ProgrammingError):
        engine._db._conn.execute("SELECT 1")


@patch("aircommand.core.privilege.subprocess.run")
def test_shutdown_still_stops_privilege_and_closes_db_when_network_manager_restart_fails(mock_run, tmp_path):
    # Same shape as test_shutdown_cancels_running_job_stops_privilege_releases_adapter_and_closes_db
    # above, except "systemctl restart NetworkManager" fails (RadioCommandFailed,
    # see rf.py) during release_to_managed() -- proving shutdown() catches that
    # (per its own new comment) and still runs every step below it, rather than
    # the exception propagating out of shutdown() and skipping privilege.stop()/
    # db.close().
    mock_run.return_value = _completed(0)

    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={
                "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
                "airodump-ng": [],  # content unused -- see AIRODUMP_RUNNING_POLLS's comment
                "aircrack-ng": NO_HANDSHAKE_OUTPUT,
                "systemctl": ["Failed to restart NetworkManager.service: Access denied"],
            },
            returncodes={"systemctl": 1},
            running_polls={"airodump-ng": AIRODUMP_RUNNING_POLLS},
        ),
        capture_handshake_check_interval=timedelta(seconds=999),
        drive_tick_interval=DRIVE_TICK_INTERVAL,
    )

    engine.privilege.start(PASSWORD)
    assert engine.privilege._keepalive_thread.is_alive()

    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    engine.capture.start_passive(target)
    time.sleep(PRE_SHUTDOWN_SETTLE_S)  # see module docstring -- let the driver
    # thread genuinely be mid-loop (and holding the RF reservation) first.

    engine.shutdown()  # must not raise despite the failing NetworkManager restart

    # Every step AFTER the failed release_to_managed() still ran.
    assert not engine.privilege._keepalive_thread.is_alive()
    with pytest.raises(sqlite3.ProgrammingError):
        engine._db._conn.execute("SELECT 1")
