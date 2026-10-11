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
from aircommand.core.events import CaptureStarted, CaptureStopped, DeauthFired, HandshakeCaptured
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
    # airodump-ng's OWN naming convention (confirmed against the real binary's
    # format string, see capture.py's ThingsToChange-item-3 comment): it
    # appends "-01.cap" to the prefix it's given -- it does NOT write the
    # prefix verbatim. Matching that here, not the prefix, is what makes this
    # fixture catch the real-hardware FileNotFoundError that masqueraded as
    # passing for as long as this fixture wrote to the bare prefix instead.
    cap_prefix = argv[argv.index("-w") + 1]
    cap_path = Path(f"{cap_prefix}-01.cap")
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


def test_passive_capture_publishes_error_detail_when_airodump_dies_unexpectedly(tmp_path):
    """Mirrors test_discovery_acceptance.py's own equivalent test -- same real
    gap (found tonight, on real hardware, debugging an unrelated Discovery
    death): StopReason.ERROR alone carried no detail anywhere in this
    codebase. Constructs its own Engine rather than reusing _make_engine --
    this is the one test in this file that needs a SHORT running_polls budget
    (so airodump-ng "dies" quickly) and a scripted stderr, neither of which
    _make_engine's shared helper takes a param for; not worth widening it for
    a single one-off scenario (same call this file's own module docstring
    already makes for AIRODUMP_RUNNING_POLLS's one shared value)."""
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
            running_polls={"airodump-ng": 2},
            stderr={"airodump-ng": ["ioctl(SIOCSIWMODE) failed: Device or resource busy"]},
        ),
        drive_tick_interval=DRIVE_TICK_INTERVAL,
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    stopped = []
    engine.subscribe(stopped.append, CaptureStopped)

    handle = engine.capture.start_passive(target)
    handle.wait_for_test(timeout=2.0)

    assert len(stopped) == 1
    assert stopped[0].reason == StopReason.ERROR
    assert stopped[0].error_detail == "ioctl(SIOCSIWMODE) failed: Device or resource busy"


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


def test_deauth_burst_failure_is_distinguished_from_a_real_burst(tmp_path):
    """docs/adr/0012: aireplay-ng's exit code/stderr used to be discarded
    entirely, so a failed injection attempt looked identical -- in both the
    audit log and DeauthFired -- to a real burst. Scripts a nonzero exit
    code + stderr for "aireplay-ng" and asserts both the published event and
    the durably-written AuditLogEntry honestly reflect the failure, rather
    than reporting it as an unremarkable success."""
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={
                "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
                "airodump-ng": [],  # content unused -- see module docstring point 1
                "aircrack-ng": NO_HANDSHAKE_OUTPUT,
                "aireplay-ng": [],
            },
            returncodes={"aireplay-ng": 1},
            stderr={"aireplay-ng": ["aireplay-ng: Unable to inject frames: Network is down"]},
            running_polls={"airodump-ng": AIRODUMP_RUNNING_POLLS},
        ),
        capture_handshake_check_interval=timedelta(seconds=0),
        drive_tick_interval=DRIVE_TICK_INTERVAL,
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    deauths_fired = []
    engine.subscribe(deauths_fired.append, DeauthFired)

    handle = engine.capture.start_deauth_assisted(
        target, DeauthOptions(interval=timedelta(seconds=0), burst_size=5, max_bursts=1)
    )
    handle.wait_for_test(timeout=2.0)

    assert len(deauths_fired) == 1
    assert deauths_fired[0].succeeded is False
    assert deauths_fired[0].error_detail == "aireplay-ng: Unable to inject frames: Network is down"

    # Still logged -- ADR-0001's "every firing, no exceptions" covers the
    # attempt, not only a confirmed-successful one -- just honestly flagged.
    audit_entries = engine.capture.list_audit_log(target)
    assert len(audit_entries) == 1
    assert audit_entries[0].succeeded is False
    assert audit_entries[0].error_detail == "aireplay-ng: Unable to inject frames: Network is down"


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
    # there. Collector + poll, not a bare `lambda args: None`: wait_for_test()
    # unblocks on mark_terminal() inside _drive's finally, BEFORE the RuntimeError
    # reaches threading.excepthook, so restoring the hook on that signal alone
    # raced and could leak the exception into an unrelated later test's
    # PytestUnhandledThreadExceptionWarning -- identical race and identical fix
    # to this file's own
    # test_handshake_not_reported_completed_if_insert_raises_after_detection, and
    # to test_crack_acceptance.py / test_enumerate_acceptance.py's stress tests.
    thread_exceptions = []
    original_hook = threading.excepthook
    threading.excepthook = thread_exceptions.append
    try:
        first_handle = engine.capture.start_passive(target)
        first_handle.wait_for_test(timeout=2.0)  # must not hang
        deadline = time.monotonic() + 2.0
        while not thread_exceptions and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        threading.excepthook = original_hook

    # The scripted raise really propagated out of the (daemon) thread, now
    # contained here rather than leaking into an unrelated test's warning output.
    assert len(thread_exceptions) == 1
    assert thread_exceptions[0].exc_type is RuntimeError

    # The real assertion: RadioController's reservation from the first (failed)
    # job was released -- a second start_passive() right after succeeds rather
    # than raising AdapterBusy.
    second_handle = engine.capture.start_passive(target)
    second_handle.cancel()
    second_handle.wait_for_test(timeout=2.0)


def test_capture_releases_rf_reservation_and_job_row_if_new_job_raises(tmp_path):
    """Same gap discovery.py's start()/enumerate.py's start_scan() were fixed
    for in this same hardening pass, originally missed here: _start() used to
    reserve the radio and call self._jobs.new_job() with nothing guarding it --
    a raise there (new_job already re-raises after its own internal rollback,
    see jobs.py) skipped releasing the reservation entirely, leaking it for the
    rest of the live session. Exception fires SYNCHRONOUSLY inside _start()
    itself (new_job() runs before the driver thread is ever spawned) -- no
    thread/excepthook dance needed."""
    engine = _make_engine(
        tmp_path,
        script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": [], "aircrack-ng": NO_HANDSHAKE_OUTPUT},
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    real_new_job = engine.capture._jobs.new_job
    calls = {"n": 0}

    def raise_on_first_call(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("new_job failed")
        return real_new_job(*args, **kwargs)

    engine.capture._jobs.new_job = raise_on_first_call

    started = []
    engine.subscribe(started.append, CaptureStarted)

    with pytest.raises(RuntimeError, match="new_job failed"):
        engine.capture.start_passive(target)

    # CaptureStarted must not fire for a capture whose driver thread never ran.
    assert started == []

    # The real assertion: the RF reservation from the failed start was
    # released -- a second start_passive() right after succeeds rather than
    # raising AdapterBusy.
    second_handle = engine.capture.start_passive(target)
    second_handle.cancel()
    second_handle.wait_for_test(timeout=2.0)


def test_handshake_check_skipped_until_cap_file_has_a_full_header(tmp_path):
    """docs/adr/0011: real aircrack-ng hangs indefinitely when given a missing
    or under-24-byte (pcap global header size) capture file. The very first
    handshake check can easily race airodump-ng's own startup/flush timing on
    real hardware -- Capture._drive must never spawn aircrack-ng at all until
    cap_path exists and has a full header, regardless of what aircrack-ng
    itself would do with it. No on_spawn cap-file writer here (deliberately,
    unlike _make_engine's shared one) -- cap_path never exists, so this proves
    the guard, not just that aircrack-ng was never scripted to run."""
    spawned = []
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
            on_spawn=spawned.append,
            running_polls={"airodump-ng": AIRODUMP_RUNNING_POLLS},
        ),
        capture_handshake_check_interval=timedelta(seconds=0),  # Pacer.due() fires every tick
        drive_tick_interval=DRIVE_TICK_INTERVAL,
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    handle = engine.capture.start_passive(target)
    time.sleep(PRE_CANCEL_SETTLE_S)  # several loop iterations for real, each one a
    # chance to (wrongly) spawn aircrack-ng against the still-missing cap file
    handle.cancel()
    handle.wait_for_test(timeout=2.0)

    assert all(argv[0] != "aircrack-ng" for argv in spawned), (
        "aircrack-ng must not be spawned against a cap file that doesn't exist yet"
    )


def test_cancel_is_responsive_while_handshake_check_is_in_flight(tmp_path):
    """docs/adr/0011's actual Cancel-bug fix: the one-shot aircrack-ng check
    must never block this loop's own cancellation check. Gives "aircrack-ng" a
    large running_polls budget (FakeProcHandle.poll() keeps reporting "still
    running") to simulate a slow-or-stuck check, then cancels while it's
    in flight and asserts the job still reaches terminal state promptly --
    not after waiting out that budget. Before this fix (plain
    "\\n".join(check_handle.lines())), FakeProcHandle.lines() would still
    return instantly regardless of poll() (it doesn't gate on poll() at all),
    so this specific scenario couldn't have caught the bug under the OLD code
    -- it exists to pin the NEW _collect_bounded behavior going forward, not
    to reproduce the original hang (only a real aircrack-ng process does
    that -- see tests/test_capture_real_subprocess.py)."""
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={
                "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
                "airodump-ng": [],
                "aircrack-ng": NO_HANDSHAKE_OUTPUT,
            },
            on_spawn=_write_cap_file_on_spawn,
            # Large budgets for BOTH -- aircrack-ng's is the one under test
            # (simulates a slow-or-stuck check); airodump-ng's just needs to
            # outlast the test the same way AIRODUMP_RUNNING_POLLS always does.
            running_polls={"airodump-ng": AIRODUMP_RUNNING_POLLS, "aircrack-ng": AIRODUMP_RUNNING_POLLS},
        ),
        capture_handshake_check_interval=timedelta(seconds=0),  # Pacer.due() fires every tick
        drive_tick_interval=DRIVE_TICK_INTERVAL,
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    handle = engine.capture.start_passive(target)
    time.sleep(PRE_CANCEL_SETTLE_S * 3)  # let the loop reach and start the handshake check
    handle.cancel()
    # If _collect_bounded still blocked on the check, this would time out --
    # AIRODUMP_RUNNING_POLLS * DRIVE_TICK_INTERVAL is ~1s, far longer than the
    # couple of ticks cancellation should actually take.
    handle.wait_for_test(timeout=2.0)


def test_cancel_escalates_to_kill_when_airodump_ignores_sigterm(tmp_path):
    """docs/adr/0011: ProcHandle.kill()'s own docstring already warned the
    aircrack-ng suite doesn't always honor SIGTERM -- Capture._drive used to
    call terminate() exactly once on Cancel and never check whether it
    actually worked. Simulates that real-world case via ignore_terminate=
    (poll() keeps reporting "still running" until kill() is called) and
    asserts the job still reaches terminal state -- proving kill() really
    gets called, not just that terminate() was attempted."""
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
            ignore_terminate={"airodump-ng"},
        ),
        drive_tick_interval=DRIVE_TICK_INTERVAL,
    )
    # Short grace period -- the default (3s) would make this test slow for no
    # reason; only the escalation actually happening is under test here.
    engine.capture._cancel_grace_period = timedelta(seconds=0.05)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    handle = engine.capture.start_passive(target)
    time.sleep(PRE_CANCEL_SETTLE_S)
    handle.cancel()
    # Would hang past this timeout if _terminate_with_escalation never
    # escalated to kill() -- FakeProcHandle.poll() would report "still
    # running" forever under ignore_terminate= otherwise.
    handle.wait_for_test(timeout=2.0)


def test_not_a_target_error_when_target_removed_after_fetch(tmp_path):
    # require_target() raises before Capture ever touches the adapter or
    # self._proc, so no tool needs to be scripted at all here.
    engine = _make_engine(tmp_path, script={})
    stale_target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    engine.targets.remove(BSSID_1)

    with pytest.raises(NotATargetError):
        engine.capture.start_passive(stale_target)


# --- Wave 2, fix group G4 -----------------------------------------------------------
#
# Fix 1: _terminate_with_escalation now delegates to procutil.terminate_with_
# escalation and RETURNS whether the process was actually confirmed dead, so a
# caller (_drive's own finally) can report ERROR instead of silently assuming a
# kill succeeded. The two tests below pin both outcomes of that return value.


class _NeverDiesHandle:
    """ProcHandle-shaped stand-in for the one case _FakeProcHandle's own
    ignore_terminate= can't express: a process that survives even SIGKILL
    (e.g. a lapsed cached sudo credential between spawn and kill -- see
    procutil.py's _RealProcHandle._signal's own logged-WARNING note).
    ignore_terminate= only withholds "exited" until kill() is actually
    called; this withholds it forever. Same idiom test_procutil.py's own
    _NeverDiesHandle already established for terminate_with_escalation's
    False-returning case, reproduced here (not imported -- that one is
    test_procutil.py-local) because Capture._drive needs the FULL ProcHandle
    shape (pid/pgid for record_process, stderr_tail for the ERROR-path hint),
    not just terminate/kill/poll."""

    def __init__(self) -> None:
        self.pid = 424242
        self.pgid = 424242

    def lines(self, idle_timeout=None):
        return iter(())

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def wait(self) -> int:
        return 0

    def poll(self):
        return None

    def stderr_tail(self) -> list[str]:
        return []


class _NeverDiesAirodumpRunner:
    """Wraps a real FakeProcRunner, substituting _NeverDiesHandle for
    airodump-ng's own spawn only -- every other spawn (airmon-ng) is
    delegated to the real fake unchanged."""

    def __init__(self, fake: FakeProcRunner) -> None:
        self._fake = fake

    def spawn(self, argv: list[str], *, privileged: bool):
        if argv[0] == "airodump-ng":
            return _NeverDiesHandle()
        return self._fake.spawn(argv, privileged=privileged)


class _NeverDiesAireplayRunner:
    """Same substitution as _NeverDiesAirodumpRunner above, for aireplay-ng's
    own spawn instead -- airodump-ng/airmon-ng/aircrack-ng all run as the real
    fake unchanged, so a deauth burst actually gets attempted against a
    genuinely RUNNING capture, not one that's already failed for an
    unrelated reason."""

    def __init__(self, fake: FakeProcRunner) -> None:
        self._fake = fake

    def spawn(self, argv: list[str], *, privileged: bool):
        if argv[0] == "aireplay-ng":
            return _NeverDiesHandle()
        return self._fake.spawn(argv, privileged=privileged)


def test_cancel_still_reports_cancelled_when_airodump_only_dies_via_kill_escalation(tmp_path):
    """Proves the TRUE branch of _terminate_with_escalation's new return value
    flows through correctly: a Cancel that only succeeds via SIGKILL
    escalation (airodump-ng ignoring SIGTERM, same ignore_terminate= idiom
    test_cancel_escalates_to_kill_when_airodump_ignores_sigterm above already
    uses) must still report StopReason.CANCELLED -- not accidentally ERROR
    just because the kill needed escalation to land."""
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
            ignore_terminate={"airodump-ng"},
        ),
        drive_tick_interval=DRIVE_TICK_INTERVAL,
    )
    engine.capture._cancel_grace_period = timedelta(seconds=0.05)  # see
    # test_cancel_escalates_to_kill_when_airodump_ignores_sigterm's identical note
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    stopped = []
    engine.subscribe(stopped.append, CaptureStopped)

    handle = engine.capture.start_passive(target)
    time.sleep(PRE_CANCEL_SETTLE_S)
    handle.cancel()
    handle.wait_for_test(timeout=2.0)

    assert len(stopped) == 1
    assert stopped[0].reason == StopReason.CANCELLED
    assert stopped[0].error_detail is None


def test_cancel_reports_error_when_airodump_cannot_be_stopped_even_by_kill(tmp_path):
    """The actual Fix 1 bug: before this fix, _terminate_with_escalation's
    result was discarded entirely, so a Cancel against a process that
    survives even SIGKILL still reported StopReason.CANCELLED, as if the
    kill had worked. Uses _NeverDiesHandle (see above) since _FakeProcHandle
    itself can't express "never dies at all"."""
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=_NeverDiesAirodumpRunner(FakeProcRunner(script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT})),
        drive_tick_interval=DRIVE_TICK_INTERVAL,
    )
    engine.capture._cancel_grace_period = timedelta(seconds=0.02)  # keep both
    # escalation windows (SIGTERM grace + SIGKILL grace) short -- this test
    # doesn't care how long they are, only that both are exhausted and the
    # process is STILL reported alive at the end.
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    stopped = []
    engine.subscribe(stopped.append, CaptureStopped)

    handle = engine.capture.start_passive(target)
    time.sleep(PRE_CANCEL_SETTLE_S)
    handle.cancel()
    handle.wait_for_test(timeout=2.0)

    assert len(stopped) == 1
    assert stopped[0].reason == StopReason.ERROR
    assert stopped[0].error_detail is not None and "could not be stopped" in stopped[0].error_detail


# --- Fix 2: the aireplay-ng deauth burst must be bounded and cancel-aware ----------


def test_deauth_burst_times_out_when_aireplay_hangs(tmp_path):
    """Fix 2's actual bug: a hung aireplay-ng used to block this loop
    indefinitely (a blind .wait()). ignore_terminate= simulates that hang
    (poll() reports "still running" until kill() lands); a short
    deauth_burst_timeout proves the bound actually fires rather than waiting
    out the hang for real."""
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={
                "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
                "airodump-ng": [],  # content unused -- see module docstring point 1
                "aircrack-ng": NO_HANDSHAKE_OUTPUT,
                "aireplay-ng": [],
            },
            ignore_terminate={"aireplay-ng"},
            running_polls={"airodump-ng": AIRODUMP_RUNNING_POLLS},
        ),
        capture_handshake_check_interval=timedelta(seconds=0),
        drive_tick_interval=DRIVE_TICK_INTERVAL,
    )
    engine.capture._deauth_burst_timeout = timedelta(seconds=0.02)  # short,
    # deliberately -- the bound firing is what's under test, not realism
    # _await_deauth_burst's own termination of the hung handle (ignore_terminate=
    # withholds "exited" until kill() lands) escalates through SIGTERM-grace +
    # SIGKILL-grace using this SAME knob -- short it too, or that escalation
    # alone would take up to 2 * DEFAULT_CANCEL_GRACE_PERIOD (6s), blowing past
    # this test's own wait_for_test timeout below.
    engine.capture._cancel_grace_period = timedelta(seconds=0.02)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    deauths_fired = []
    engine.subscribe(deauths_fired.append, DeauthFired)

    handle = engine.capture.start_deauth_assisted(
        target, DeauthOptions(interval=timedelta(seconds=0), burst_size=5, max_bursts=1)
    )
    # Not an unbounded block -- see module docstring point 1 for why
    # airodump-ng's own running_polls budget is what ends this job's loop
    # (same shape test_deauth_burst_failure_is_distinguished_from_a_real_burst
    # above already relies on with max_bursts=1 and no explicit cancel).
    handle.wait_for_test(timeout=2.0)

    assert len(deauths_fired) == 1
    assert deauths_fired[0].succeeded is False
    assert deauths_fired[0].error_detail == "timed out"

    audit_entries = engine.capture.list_audit_log(target)
    assert len(audit_entries) == 1
    assert audit_entries[0].succeeded is False
    assert audit_entries[0].error_detail == "timed out"


def test_deauth_burst_timeout_notes_when_aireplay_survives_even_kill(tmp_path):
    """_await_deauth_burst's own escalation can itself fail to confirm the
    kill (e.g. a lapsed cached sudo credential between spawn and kill -- see
    procutil.py's own logged-WARNING note) -- that must be recorded on the
    audit row/DeauthFired, same as the main airodump-ng handle's equivalent
    "could not be stopped" note in _drive's own finally block, not silently
    discarded. Uses _NeverDiesHandle (via _NeverDiesAireplayRunner) since
    _FakeProcHandle itself can't express "never dies even after kill()"."""
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=_NeverDiesAireplayRunner(
            FakeProcRunner(
                script={
                    "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
                    "airodump-ng": [],
                    "aircrack-ng": NO_HANDSHAKE_OUTPUT,
                },
                running_polls={"airodump-ng": AIRODUMP_RUNNING_POLLS},
            )
        ),
        capture_handshake_check_interval=timedelta(seconds=0),
        drive_tick_interval=DRIVE_TICK_INTERVAL,
    )
    engine.capture._deauth_burst_timeout = timedelta(seconds=0.02)  # short --
    # see test_deauth_burst_times_out_when_aireplay_hangs's identical note.
    engine.capture._cancel_grace_period = timedelta(seconds=0.02)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    deauths_fired = []
    engine.subscribe(deauths_fired.append, DeauthFired)

    handle = engine.capture.start_deauth_assisted(
        target, DeauthOptions(interval=timedelta(seconds=0), burst_size=5, max_bursts=1)
    )
    handle.wait_for_test(timeout=2.0)

    assert len(deauths_fired) == 1
    assert deauths_fired[0].succeeded is False
    assert deauths_fired[0].error_detail == "timed out; aireplay-ng could not be stopped"

    audit_entries = engine.capture.list_audit_log(target)
    assert len(audit_entries) == 1
    assert audit_entries[0].error_detail == "timed out; aireplay-ng could not be stopped"


def test_deauth_burst_cancelled_mid_flight_reports_cancelled(tmp_path):
    """Same hang, but Cancel (not the timeout) is what ends it -- proving
    _await_deauth_burst's token.is_cancelled() check, not just its deadline,
    actually bounds this wait."""
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={
                "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
                "airodump-ng": [],  # content unused -- see module docstring point 1
                "aircrack-ng": NO_HANDSHAKE_OUTPUT,
                "aireplay-ng": [],
            },
            ignore_terminate={"aireplay-ng"},
            running_polls={"airodump-ng": AIRODUMP_RUNNING_POLLS},
        ),
        capture_handshake_check_interval=timedelta(seconds=0),
        drive_tick_interval=DRIVE_TICK_INTERVAL,
    )
    engine.capture._cancel_grace_period = timedelta(seconds=0.02)  # see the
    # identical note in test_deauth_burst_times_out_when_aireplay_hangs above --
    # _await_deauth_burst's own termination of the hung handle escalates using
    # this same knob, not deauth_burst_timeout
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    deauths_fired = []
    engine.subscribe(deauths_fired.append, DeauthFired)

    handle = engine.capture.start_deauth_assisted(
        target, DeauthOptions(interval=timedelta(seconds=0), burst_size=5, max_bursts=None)
    )
    time.sleep(PRE_CANCEL_SETTLE_S)  # let the burst actually start hanging first
    handle.cancel()
    handle.wait_for_test(timeout=2.0)

    assert len(deauths_fired) == 1
    assert deauths_fired[0].succeeded is False
    assert deauths_fired[0].error_detail == "cancelled"

    audit_entries = engine.capture.list_audit_log(target)
    assert len(audit_entries) == 1
    assert audit_entries[0].error_detail == "cancelled"


# --- Fix 3: the audit log's bssid snapshot must carry the real Target bssid -------


def test_audit_log_entry_records_the_target_bssid(tmp_path):
    engine = _make_engine(
        tmp_path,
        script={
            "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
            "airodump-ng": [],  # content unused -- see module docstring point 1
            "aircrack-ng": NO_HANDSHAKE_OUTPUT,
            "aireplay-ng": [],
        },
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    handle = engine.capture.start_deauth_assisted(
        target, DeauthOptions(interval=timedelta(seconds=0), burst_size=5, max_bursts=1)
    )
    handle.wait_for_test(timeout=2.0)

    audit_entries = engine.capture.list_audit_log(target)
    assert len(audit_entries) == 1
    assert audit_entries[0].bssid == target.bssid


# --- Fix 4: handshake_seen must not flip True until persistence actually succeeds -


def test_handshake_not_reported_completed_if_insert_raises_after_detection(tmp_path):
    """Before this fix, handshake_seen = True was set BEFORE
    db_scope.handshakes.insert(...) -- a raise there (or from the publish
    right after) still left handshake_seen True, so finally's reason
    computation reported CaptureStopped(COMPLETED) even though no Handshake
    was ever actually persisted and HandshakeCaptured never fired."""
    engine = _make_engine(
        tmp_path,
        script={
            "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
            "airodump-ng": [],  # content unused -- see module docstring point 1
            "aircrack-ng": ["   1  AA:BB:CC:DD:EE:01  Test-SSID              WPA (1 handshake)"],
        },
    )
    engine.capture._cancel_grace_period = timedelta(seconds=0.02)  # keep Fix 1's
    # own safety net (which this exception path also exercises, since the
    # airodump-ng handle is still alive when the exception unwinds) fast.
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    real_new_connection_scope = engine.capture._new_connection_scope

    def scope_with_raising_insert(*args, **kwargs):
        scope = real_new_connection_scope(*args, **kwargs)
        scope.handshakes.insert = lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("simulated insert failure")
        )
        return scope

    engine.capture._new_connection_scope = scope_with_raising_insert

    handshakes_captured = []
    stopped = []
    engine.subscribe(handshakes_captured.append, HandshakeCaptured)
    engine.subscribe(stopped.append, CaptureStopped)

    # No except clause in Capture._drive -- the raise above propagates out of
    # the (daemon) thread uncaught, same pattern
    # test_capture_releases_rf_reservation_even_if_new_connection_scope_raises
    # above already uses for an expected-and-scripted uncaught raise. Swap
    # threading.excepthook to a COLLECTOR and poll until it actually fires
    # before restoring the outer hook -- NOT a bare `lambda args: None` with a
    # plain try/finally. wait_for_test() unblocks the instant mark_terminal()
    # sets its event inside _drive's own finally, which completes BEFORE the
    # RuntimeError finishes unwinding out of the thread and reaches
    # threading.excepthook (the exact ordering test_crack_acceptance.py's and
    # test_enumerate_acceptance.py's own stress tests already document). The old
    # `lambda args: None` + plain try/finally here restored the outer hook on
    # the mark_terminal signal alone, so it raced: whenever the thread lost that
    # race the re-exposed outer hook (pytest's own _pytest/threadexception
    # collector) caught the leak and reported it as a
    # PytestUnhandledThreadExceptionWarning against whatever UNRELATED later test
    # was in its runtest phase then -- reproduced landing on
    # test_capture_real_subprocess.py here, ~1 run in 6. Collector + poll keeps
    # the by-design raise contained to this test and lets us positively assert
    # it propagated rather than merely swallowing it.
    thread_exceptions = []
    original_hook = threading.excepthook
    threading.excepthook = thread_exceptions.append
    try:
        handle = engine.capture.start_passive(target)
        handle.wait_for_test(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while not thread_exceptions and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        threading.excepthook = original_hook

    assert handshakes_captured == []
    assert stopped[0].reason != StopReason.COMPLETED
    assert len(stopped) == 1
    assert stopped[0].reason == StopReason.ERROR
    assert engine.capture.list_handshakes(target) == []
    # The raise really propagated out of the (daemon) thread uncaught -- the
    # by-design outcome (Capture._drive has no except clause), now contained to
    # THIS test instead of leaking into an unrelated one's warning output.
    assert len(thread_exceptions) == 1
    assert thread_exceptions[0].exc_type is RuntimeError
