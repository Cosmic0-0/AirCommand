"""FakeProcRunner -> hashcat -> CrackProgress / CrackResult -> SQLite, wired
through a real Engine with no GUI, no root, and no hardware. Same style as
test_capture_acceptance.py: a real Engine, no mocking of internals, assertions
on published events plus a repository read via engine.crack.list_results().

Crack needs a real Handshake to crack (mint-restricted, only Capture can
produce one -- see domain.py), so every scenario here first drives a real
(scripted) passive Capture exactly the way test_capture_acceptance.py's own
first test does, then grabs HandshakeCaptured's .handshake before ever
touching Crack. _capture_handshake() below is that recipe, factored out since
all three Crack scenarios need it.

One real-thread-timing subtlety applies here, the same one
test_capture_acceptance.py documents, checked here against what crack.py's own
_drive actually does rather than assumed by analogy:

1. Cancellation race (identical to test_capture_acceptance.py point 1): a
   plain, instantly-iterable scripted hashcat stdout lets the driver thread's
   loop exhaust before this thread's next line (the .cancel() call) ever runs,
   for the same threading.Thread.start()-hands-off-the-CPU reason documented
   there. _slow_hashcat_lines()/PRE_CANCEL_SETTLE_S below are the same fix:
   real (tiny) per-line delays plus a short real settle sleep before cancel(),
   so cancellation reliably lands mid-loop instead of racing an already-
   exhausted stream.

(Terminal-event ordering was checked too: crack.py's _drive finally block
calls self._bus.publish(CrackResult(...)) before self._jobs.mark_terminal(),
and EventBus.publish() is synchronous, so wait_for_test() returning is already
proof CrackResult has been observed -- no polling helper needed for it. A
separate, genuinely pre-existing bug was found this way -- JobRegistry.mark_terminal
was setting its event before its own DB write committed, which could race a
second job's write on the shared sqlite3 connection -- and has since been
fixed in jobs.py itself; no test-side workaround needed here anymore.)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Callable, Optional

import pytest

from aircommand.core.domain import Aborted, Exhausted, Found, MacAddress, StopReason
from aircommand.core.engine import Engine
from aircommand.core.events import CrackProgress, CrackResult, HandshakeCaptured
from aircommand.core.parse import _format_hashrate
from aircommand.core.procutil import FakeProcRunner, _FakeProcHandle, terminate_with_escalation

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")
BSSID_1_HEX = "aabbccddee01"  # BSSID_1.value, lowercase, no colons -- the AP-MAC
# field shape filter_hc22000_lines_by_bssid (parse.py) actually matches against,
# confirmed against real hcxpcapngtool output (see ADR-0015).
WRONG_BSSID_HEX = "112233445566"  # any value != BSSID_1_HEX -- simulates
# hcxpcapngtool extracting a hash for a DIFFERENT network than this Handshake's
# own Target, which filter_hc22000_lines_by_bssid must reject (ADR-0015).

# Gets Capture's own driver to find a handshake on its very first check (same
# recipe, same reasoning, as test_capture_acceptance.py's
# test_passive_capture_finds_handshake_on_first_check). Capture's _drive loop
# is a plain wall-clock loop now, driven by ProcHandle.poll() for liveness,
# not handle.lines() content (see test_capture_acceptance.py's module
# docstring for the full real-hardware finding) -- running_polls=1 for
# "airodump-ng" (see _make_engine) is enough for the loop to run its body
# exactly once, where it breaks out immediately on finding the handshake.
AIRCRACK_HANDSHAKE_FOUND = "   1  AA:BB:CC:DD:EE:01  Test-SSID              WPA (1 handshake)"
CAP_FILE_BYTES = b"fake-cap-file-bytes-for-sha256-hashing"

FOUND_KEY = "correcthorsebattery"  # plain text, no hash prefix -- matches
# --outfile-format 2 (HASHCAT_OUTFILE_FORMAT_PLAIN_ONLY), per crack.py's module
# docstring and the pinned TODO's reasoning about WPA2 passphrases containing ':'.

# See module docstring point 1. Real but tiny, same magnitude and reasoning as
# test_capture_acceptance.py's own PRE_CANCEL_SETTLE_S.
PRE_CANCEL_SETTLE_S = 0.02


def _status_json(progress: tuple[int, int], speed: int, estimated_stop: Optional[float] = None) -> str:
    """Builds one --status-json line shaped per parse_hashcat_status_line's TODO
    and HashcatStatus's docstring: progress=[done, total], devices=[{speed}, ...],
    optional estimated_stop (epoch seconds). recovered_hashes is real hashcat
    --status-json shape too, but deliberately unread by the parser (HashcatStatus
    has no found-key/outcome field -- see its own docstring) -- included here
    only so this fake line looks like a real one, not because anything checks it."""
    data = {
        "progress": list(progress),
        "recovered_hashes": [0, 1],
        "devices": [{"speed": speed}],
    }
    if estimated_stop is not None:
        data["estimated_stop"] = estimated_stop
    return json.dumps(data)


def _slow_hashcat_lines(count: int, delay_s: float):
    """Real (but tiny) per-line delay -- see module docstring point 1. Yields
    valid --status-json lines so the cancellation scenario also exercises real
    parsing along the way, not just cancellation."""
    for i in range(count):
        time.sleep(delay_s)
        yield _status_json((i, count), 1_000_000)


def _hc22000_line(bssid_hex: str = BSSID_1_HEX) -> str:
    """A syntactically-valid-shaped hc22000 line (ADR-0015) for FakeProcRunner's
    hcxpcapngtool on_spawn to write -- only the first four '*'-delimited fields
    matter to anything in this codebase (filter_hc22000_lines_by_bssid checks
    fields[0] == "WPA" and fields[3], parse.py), so the rest are placeholder
    hex, not a real captured handshake's bytes."""
    return f"WPA*02*{'0' * 32}*{bssid_hex}*{'1' * 12}*{'746573742d73736964'}*{'0' * 64}*02"


def _make_on_spawn(hashcat_key: Optional[str], *, conversion_bssid_hex: str = BSSID_1_HEX) -> Callable[[list[str]], None]:
    """Combines the FakeProcRunner on_spawn side effects needed here:
    airodump-ng's -w <cap_path> (so Capture's _drive has a real file to
    sha256 -- same guarded check as test_capture_acceptance.py's
    _write_cap_file_on_spawn, and needed for the same reason: this file's
    aircrack-ng invocation also carries '-w', for its unrelated /dev/null
    wordlist argument), hcxpcapngtool's -o <path> (so Crack's new conversion
    step, ADR-0015, has a real file to read a hash line from -- the shape
    filter_hc22000_lines_by_bssid actually checks, not a real tool run), and
    hashcat's --outfile <path> (so Crack's _drive has a real file to read the
    cracked plaintext from). hashcat_key=None means "don't write the outfile
    at all" -- the wordlist-exhausted/cancelled scenarios, where a real
    hashcat run would leave no outfile behind either. conversion_bssid_hex
    lets a test simulate hcxpcapngtool extracting a hash for the WRONG
    network (WRONG_BSSID_HEX) -- the filter is what's actually under test
    there, not the conversion step itself."""

    def on_spawn(argv: list[str]) -> None:
        if argv[0] == "airodump-ng" and "-w" in argv:
            # airodump-ng appends "-01.cap" to the given prefix itself -- see
            # capture.py's ThingsToChange-item-3 comment for how this was
            # confirmed against the real binary.
            cap_prefix = argv[argv.index("-w") + 1]
            cap_path = Path(f"{cap_prefix}-01.cap")
            cap_path.write_bytes(CAP_FILE_BYTES)
        elif argv[0] == "hcxpcapngtool":
            hash_file_path = Path(argv[argv.index("-o") + 1])
            hash_file_path.write_text(_hc22000_line(conversion_bssid_hex) + "\n")
        elif argv[0] == "hashcat" and hashcat_key is not None:
            outfile_path = Path(argv[argv.index("--outfile") + 1])
            outfile_path.write_text(hashcat_key + "\n")

    return on_spawn


def _make_engine(
    tmp_path, script: dict, hashcat_key: Optional[str] = None,
    returncodes: Optional[dict[str, int]] = None,
    conversion_bssid_hex: str = BSSID_1_HEX,
    running_polls: Optional[dict[str, int]] = None,
    on_spawn: Optional[Callable[[list[str]], None]] = None,
    ignore_terminate: Optional[set[str]] = None,
) -> Engine:
    """on_spawn, if given, REPLACES the default _make_on_spawn(hashcat_key, ...)
    entirely (rather than composing with it) -- a test that needs its own
    on_spawn side effect (e.g. writing a non-UTF-8 byte straight into the
    outfile, fix 2a) builds its own full on_spawn and passes hashcat_key=None,
    unused. ignore_terminate is passed straight through to FakeProcRunner -- a
    new knob this wave's tests need (simulating a process that won't exit on
    its own, only on kill()) that no existing call site here used before
    (defaults to this file's existing behavior, so every pre-existing call
    site is unaffected)."""
    return Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script=script,
            on_spawn=on_spawn or _make_on_spawn(hashcat_key, conversion_bssid_hex=conversion_bssid_hex),
            running_polls={"airodump-ng": 1, **(running_polls or {})},
            returncodes=returncodes or {},
            ignore_terminate=ignore_terminate,
        ),
        # Zero interval: Pacer.due() fires on every check -- same trick
        # test_capture_acceptance.py's _make_engine uses, needed here for the
        # same reason (Capture's handshake check runs as part of getting a
        # Handshake at all, even though this file is testing Crack).
        capture_handshake_check_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )


def _capture_handshake(engine: Engine, target):
    """Drives a real (scripted) passive Capture to get a real Handshake to
    crack against -- Handshake is mint-restricted (domain.py), so this is the
    only legitimate way to obtain one, same as test_capture_acceptance.py's
    first test. HandshakeCaptured itself fires mid-loop, strictly before the
    loop can exit -- same reasoning as that file's DeauthFired note -- so
    wait_for_test() returning is already enough proof *that* has happened, no
    extra settling needed for it. (An earlier version of this helper also
    slept here to work around a since-fixed jobs.py race -- see the module
    docstring's parenthetical -- no longer needed now that mark_terminal()
    itself orders its DB write before its event.)

    timeout=10.0, not 2.0: this is a genuinely fast, single-threaded, all-fake
    path (no real subprocess, no real sleep) that normally completes in low
    single-digit milliseconds -- but once the Phase 3 GUI test files joined the
    suite (many real ctk.CTk()/CTkToplevel() windows created and destroyed
    across dozens of tests), this call started intermittently taking multiple
    real seconds and occasionally missing a 2.0s deadline, confirmed
    reproducible (~50-100% of full-suite runs, depending on how many GUI tests
    ran first) and confirmed to NOT be a raw-Python-thread-count effect (a
    standalone repro spinning up 50 extra idle Engines showed zero measurable
    slowdown) -- it's specifically tied to accumulated real Tk/X11 overhead
    from many GUI tests run earlier in the same process, not a logic bug in
    Capture's own driver. Same "generous relative to the normal case, still
    bounded so a real bug fails loudly" reasoning as test_privilege.py's own
    WAIT_TIMEOUT_S."""
    captured: list[HandshakeCaptured] = []
    subscription = engine.subscribe(captured.append, HandshakeCaptured)
    handle = engine.capture.start_passive(target)
    handle.wait_for_test(timeout=10.0)
    subscription.unsubscribe()
    assert len(captured) == 1
    return captured[0].handshake


def _base_script(hashcat_lines) -> dict:
    return {
        # RadioController.reserve() now really spawns "airmon-ng" on Capture's
        # first monitor-mode use (docs/roadmap.md Phase 2 item 1) -- no rename-
        # announcement line, so it falls back to the original "wlan0" name.
        "airmon-ng": ["monitor mode already enabled on wlan0"],
        "airodump-ng": [],  # content unused -- see AIRCRACK_HANDSHAKE_FOUND's comment
        "aircrack-ng": [AIRCRACK_HANDSHAKE_FOUND],
        "hcxpcapngtool": [],  # content unused -- on_spawn writes the hc22000 file directly (ADR-0015)
        "hashcat": hashcat_lines,
    }


def test_crack_finds_key_reports_progress_and_persists_result(tmp_path):
    now = time.time()
    hashcat_lines = [
        _status_json((200_000, 1_000_000), 1_200_000, now + 120),
        _status_json((500_000, 1_000_000), 1_300_000, now + 60),
    ]
    engine = _make_engine(tmp_path, _base_script(hashcat_lines), hashcat_key=FOUND_KEY)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    progress_events = []
    results = []
    engine.subscribe(progress_events.append, CrackProgress)
    engine.subscribe(results.append, CrackResult)

    wordlist_path = tmp_path / "wordlist.txt"
    handle = engine.crack.start(handshake, wordlist_path)
    handle.wait_for_test(timeout=2.0)  # see module docstring point 2 -- already
    # sufficient proof CrackResult fired, no extra polling needed.

    assert len(progress_events) == 2
    first, second = progress_events
    assert first.job_id == handle.job_id
    assert first.percent == pytest.approx(20.0)
    assert first.hashrate == "1.2 MH/s"
    assert first.eta is not None and 0 < first.eta.total_seconds() <= 120
    assert second.percent == pytest.approx(50.0)
    assert second.hashrate == "1.3 MH/s"
    assert second.eta is not None and 0 < second.eta.total_seconds() <= 60

    assert len(results) == 1
    result_row = results[0].result
    assert results[0].job_id == handle.job_id
    assert isinstance(result_row.outcome, Found)
    assert result_row.outcome.key == FOUND_KEY
    assert result_row.handshake_id == handshake.id
    assert result_row.wordlist_path == wordlist_path
    assert result_row.stop_reason == StopReason.COMPLETED

    assert engine.crack.list_results(handshake) == [result_row]


def test_crack_exhausts_wordlist_without_finding_key(tmp_path):
    # No --status-json lines needed to exercise "ran out of wordlist" -- an
    # empty scripted stdout is exactly what a real exhausted hashcat run's
    # loop would look like from _drive's point of view (nothing left to
    # iterate), and hashcat_key=None means the outfile is never written.
    engine = _make_engine(tmp_path, _base_script([]), hashcat_key=None)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    results = []
    engine.subscribe(results.append, CrackResult)

    wordlist_path = tmp_path / "wordlist.txt"
    handle = engine.crack.start(handshake, wordlist_path)
    handle.wait_for_test(timeout=2.0)

    assert len(results) == 1
    result_row = results[0].result
    assert isinstance(result_row.outcome, Exhausted)
    assert result_row.handshake_id == handshake.id
    assert result_row.stop_reason == StopReason.COMPLETED

    assert engine.crack.list_results(handshake) == [result_row]


def test_crack_exhausted_wordlist_with_exit_code_1_is_completed_not_error(tmp_path):
    """D3 (approved design decision, confirmed against real hashcat 6.2.6, not
    re-derived here): `hashcat -m 22000 ... --potfile-disable` exits 1 when it
    exhausts the wordlist cleanly without finding the password -- a normal,
    complete run, not a crash. returncodes={"hashcat": 1} plus hashcat_key=None
    (no outfile written, matching a real exhausted run) simulates exactly
    that. Before this fix, exit 1 was indistinguishable from exit 255 (a real
    crash) and got misreported as StopReason.ERROR; this is the direct
    acceptance test for the fix: exit 1 with nothing found must be COMPLETED,
    never ERROR and never confused with Found() either."""
    engine = _make_engine(tmp_path, _base_script([]), hashcat_key=None, returncodes={"hashcat": 1})
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    results = []
    engine.subscribe(results.append, CrackResult)

    wordlist_path = tmp_path / "wordlist.txt"
    handle = engine.crack.start(handshake, wordlist_path)
    handle.wait_for_test(timeout=2.0)

    assert len(results) == 1
    result_row = results[0].result
    assert isinstance(result_row.outcome, Exhausted)
    assert result_row.handshake_id == handshake.id
    assert result_row.stop_reason == StopReason.COMPLETED

    assert engine.crack.list_results(handshake) == [result_row]


def test_crack_records_error_stop_reason_when_hashcat_crashes(tmp_path):
    """ADR-0009 regression: a crashed hashcat run (bad args, no GPU driver, an
    unreadable .cap file) used to be indistinguishable from a legitimately
    exhausted wordlist -- both leave an empty outfile, and the old code never
    checked hashcat's real exit code at all. returncodes={"hashcat": 255} plus
    hashcat_key=None (no outfile written, matching a real crash leaving
    nothing behind) and an empty scripted "hashcat" stdout together simulate
    exactly that: hashcat dying immediately, nothing to iterate, no outfile.
    255, not 1: D3 (see test_crack_exhausted_wordlist_with_exit_code_1_is_
    completed_not_error above) confirmed exit 1 is a normal, complete,
    nothing-found run, not a crash -- 255 is a real failure exit code
    (e.g. a malformed hash file), distinct from that. outcome stays
    Exhausted() (CrackOutcome has no fourth variant for this -- see
    domain.py); stop_reason is the only signal that should change.
    """
    engine = _make_engine(tmp_path, _base_script([]), hashcat_key=None, returncodes={"hashcat": 255})
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    results = []
    engine.subscribe(results.append, CrackResult)

    wordlist_path = tmp_path / "wordlist.txt"
    handle = engine.crack.start(handshake, wordlist_path)
    handle.wait_for_test(timeout=2.0)

    assert len(results) == 1
    result_row = results[0].result
    assert isinstance(result_row.outcome, Exhausted)
    assert result_row.handshake_id == handshake.id
    assert result_row.stop_reason == StopReason.ERROR

    assert engine.crack.list_results(handshake) == [result_row]


def test_crack_cancelled_before_finishing(tmp_path):
    engine = _make_engine(
        tmp_path, _base_script(_slow_hashcat_lines(1000, 0.001)), hashcat_key=None
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    results = []
    engine.subscribe(results.append, CrackResult)

    wordlist_path = tmp_path / "wordlist.txt"
    handle = engine.crack.start(handshake, wordlist_path)
    time.sleep(PRE_CANCEL_SETTLE_S)  # let real iterations happen first -- see
    # module docstring point 1.
    handle.cancel()
    handle.wait_for_test(timeout=2.0)

    assert len(results) == 1
    result_row = results[0].result
    assert isinstance(result_row.outcome, Aborted)
    assert result_row.handshake_id == handshake.id
    assert result_row.stop_reason == StopReason.CANCELLED

    assert engine.crack.list_results(handshake) == [result_row]


# --- Wave 2, fix group G5 regressions ----------------------------------------
# Hardening pass: cancellation responsiveness/escalation, a strict outfile
# decode and a DB failure that could each leave a job stuck RUNNING forever,
# unlinking the found-key artifact only after it's durably committed, and an
# absolute wordlist path for hashcat's own argv parser. See crack.py's own
# comments at each fix site for the full reasoning -- these tests just prove
# each one.

def test_crack_passes_tick_interval_as_idle_timeout_to_handle_lines(tmp_path, monkeypatch):
    """Fix 1 (wiring half): crack.py's hashcat loop now calls
    `handle.lines(idle_timeout=self._tick_interval_s)`, not the old bare
    `handle.lines()` -- that's what lets the loop recheck token.is_cancelled()
    on a schedule even if hashcat stays quiet between --status-json ticks,
    instead of blocking on queue.get() forever. FakeProcRunner's own
    _FakeProcHandle.lines() deliberately accepts and ignores idle_timeout
    (every script= here is a finite, already-known list -- no real "idle" to
    simulate against this test double), which makes a true behavioral test of
    the None-yielding path infeasible without a real subprocess (see
    test_procutil.py for that). This test proves the WIRING instead, via a
    thin spy around _FakeProcHandle.lines that still delegates to the real
    (ignoring) implementation -- not a redefinition of what it does."""
    # rf.py/capture.py also call .lines() on their own one-shot handles
    # (draining airmon-ng/aircrack-ng output, always with the old no-timeout
    # default) as part of getting a Handshake at all -- the spy keys by each
    # handle's own scripted_lines so the hashcat call can be picked out from
    # those unrelated ones, rather than assuming it's the only call made.
    real_lines = _FakeProcHandle.lines
    calls = []

    def spy_lines(self, idle_timeout=None):
        calls.append((self._scripted_lines, idle_timeout))
        return real_lines(self, idle_timeout=idle_timeout)

    monkeypatch.setattr(_FakeProcHandle, "lines", spy_lines)

    hashcat_lines = [_status_json((1, 2), 100)]
    engine = _make_engine(tmp_path, _base_script(hashcat_lines), hashcat_key=None)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    wordlist_path = tmp_path / "wordlist.txt"
    handle = engine.crack.start(handshake, wordlist_path)
    handle.wait_for_test(timeout=2.0)

    # Exactly one .lines() call on hashcat's own handle (the hcxpcapngtool
    # conversion step uses poll(), not lines() -- see _await_conversion),
    # carrying this Crack instance's own _tick_interval_s, not the old
    # no-timeout default (None).
    hashcat_calls = [idle for scripted, idle in calls if scripted == hashcat_lines]
    assert hashcat_calls == [engine.crack._tick_interval_s]


def test_crack_safety_net_kills_still_running_handle_when_loop_exits_via_exception(tmp_path, monkeypatch):
    """Fix 1 (safety-net half): the `finally` block's own
    `if handle is not None and handle.poll() is None: terminate_with_escalation(handle)`
    must run -- and must actually end up with hashcat confirmed dead -- when
    the loop ends via an exception that never itself signaled hashcat at all.
    This is deliberately NOT the loop's own cancel branch (already covered by
    test_crack_cancelled_before_finishing above): parse_hashcat_status_line is
    monkeypatched to raise mid-loop, simulating some unrelated unexpected
    failure, with token never cancelled.

    ignore_terminate={"hashcat"} simulates "still alive, nothing told it to
    stop" (poll() reports running forever until kill() -- see _FakeProcHandle's
    own docstring), so the ONLY thing that can end it is the safety net's own
    terminate_with_escalation call actually reaching kill().
    terminate_with_escalation's real grace periods (3s x2, procutil.py, Wave 1,
    not touched here) are genuine production behavior -- this test spies on
    crack.py's own module-level binding to swap in much shorter grace/tick
    values for speed, while still running the REAL escalation function, not a
    mock of the fix under test."""
    import aircommand.core.crack as crack_module

    escalation_calls = []

    def fast_terminate_with_escalation(handle, *args, **kwargs):
        escalation_calls.append(handle)
        return terminate_with_escalation(handle, grace_period_s=0.05, tick_s=0.01)

    monkeypatch.setattr(crack_module, "terminate_with_escalation", fast_terminate_with_escalation)

    def raise_instead_of_parsing(line):
        raise RuntimeError("simulated unexpected parsing failure -- not a cancel")

    monkeypatch.setattr(crack_module, "parse_hashcat_status_line", raise_instead_of_parsing)

    engine = _make_engine(
        tmp_path, _base_script([_status_json((1, 2), 100)]), hashcat_key=None,
        ignore_terminate={"hashcat"},
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    results = []
    engine.subscribe(results.append, CrackResult)

    wordlist_path = tmp_path / "wordlist.txt"
    # The forced RuntimeError propagates out of _drive uncaught (same shape
    # as test_crack_cleans_up_even_if_new_connection_scope_raises's own
    # ADR-0009 regression below) -- expected and scripted, so the default
    # excepthook's traceback spam is suppressed the same way that test does,
    # and the raised exception itself is captured for the assertion below.
    thread_exceptions = []
    original_hook = threading.excepthook
    threading.excepthook = thread_exceptions.append
    try:
        handle = engine.crack.start(handshake, wordlist_path)
        handle.wait_for_test(timeout=2.0)  # must NOT hang despite the forced exception
        # wait_for_test() returning is NOT proof threading.excepthook has already
        # been invoked: mark_terminal() (what wait_for_test() actually waits on)
        # runs inside _drive's own finally block, which completes BEFORE the
        # re-raised RuntimeError finishes unwinding out of _drive() and reaching
        # Thread._bootstrap_inner's own exception handling -- a real, narrow race
        # confirmed empirically (this assert flaked under a tight repeated-run
        # loop without this poll). Bounded, short poll rather than a blind sleep.
        deadline = time.monotonic() + 1.0
        while not thread_exceptions and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        threading.excepthook = original_hook

    assert len(thread_exceptions) == 1
    assert thread_exceptions[0].exc_type is RuntimeError

    # The safety net actually ran, on the real hashcat ProcHandle...
    assert len(escalation_calls) == 1
    # ...and confirmed it dead (poll() no longer None) -- not left running.
    assert escalation_calls[0].poll() is not None

    # The job still reached a terminal state -- proven by wait_for_test() not
    # hanging above, and by mark_terminal()/db_scope.close() having actually
    # run (the DB write immediately before mark_terminal() is what published
    # this one CrackResult -- see crack.py's finally block ordering comment).
    assert len(results) == 1


def test_crack_reads_outfile_with_replacement_char_on_invalid_utf8_byte(tmp_path):
    """Fix 2a: a found key containing a byte that isn't valid UTF-8 (plausible
    from some wordlists/encodings) must not hang the job. Before this fix,
    outfile_path.read_text()'s strict decode raised UnicodeDecodeError INSIDE
    _drive's `finally` block, which propagated out of the whole method --
    mark_terminal() never ran, so wait_for_test() below would hang forever.
    Writes the bad byte straight into the outfile via write_bytes (not
    _make_on_spawn's hashcat_key=<str>, which can only ever produce valid
    text) -- the same on_spawn idiom this file already uses for airodump-ng's
    .cap and hcxpcapngtool's hash file, just hashcat's own branch replaced."""
    bad_bytes = b"correcthorse\xffbattery\n"

    def on_spawn(argv: list[str]) -> None:
        if argv[0] == "airodump-ng" and "-w" in argv:
            cap_prefix = argv[argv.index("-w") + 1]
            Path(f"{cap_prefix}-01.cap").write_bytes(CAP_FILE_BYTES)
        elif argv[0] == "hcxpcapngtool":
            hash_file_path = Path(argv[argv.index("-o") + 1])
            hash_file_path.write_text(_hc22000_line() + "\n")
        elif argv[0] == "hashcat":
            outfile_path = Path(argv[argv.index("--outfile") + 1])
            outfile_path.write_bytes(bad_bytes)

    engine = _make_engine(tmp_path, _base_script([]), on_spawn=on_spawn)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    results = []
    engine.subscribe(results.append, CrackResult)

    wordlist_path = tmp_path / "wordlist.txt"
    handle = engine.crack.start(handshake, wordlist_path)
    handle.wait_for_test(timeout=2.0)  # must not hang

    assert len(results) == 1
    result_row = results[0].result
    assert isinstance(result_row.outcome, Found)
    # errors="replace" -> U+FFFD for the one bad byte, read successfully
    # rather than raising.
    assert result_row.outcome.key == bad_bytes.decode(errors="replace").strip()
    assert result_row.stop_reason == StopReason.COMPLETED

    assert engine.crack.list_results(handshake) == [result_row]


def test_crack_logs_and_still_marks_terminal_when_result_insert_raises(tmp_path, caplog):
    """Fix 2b: a `crack_results.insert()` failure (e.g. a real DB error) must
    not prevent mark_terminal()/db_scope.close() from running. Before this
    fix, the raise propagated straight out of the finally block's db-write
    section, skipping both -- the job stuck RUNNING forever (wait_for_test()
    hangs), with the failure itself silently swallowed (no log, nothing).
    Monkeypatches _new_connection_scope (same established idiom as
    test_crack_cleans_up_even_if_new_connection_scope_raises below) to swap in
    a crack_results stand-in whose insert() raises, rather than making the
    real one raise, which would need a real DB fault to trigger."""
    engine = _make_engine(tmp_path, _base_script([]), hashcat_key=FOUND_KEY)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    real_new_connection_scope = engine.crack._new_connection_scope

    class _FailingCrackResults:
        def insert(self, *args, **kwargs):
            raise RuntimeError("simulated crack_results.insert() DB failure")

    def scope_with_failing_insert(*args, **kwargs):
        scope = real_new_connection_scope(*args, **kwargs)
        scope.crack_results = _FailingCrackResults()
        return scope

    engine.crack._new_connection_scope = scope_with_failing_insert

    results = []
    engine.subscribe(results.append, CrackResult)

    wordlist_path = tmp_path / "wordlist.txt"
    with caplog.at_level(logging.ERROR, logger="aircommand.core.crack"):
        handle = engine.crack.start(handshake, wordlist_path)
        handle.wait_for_test(timeout=2.0)  # must not hang

    assert results == []  # insert raised before publish ever ran
    assert any("failed to record the result" in r.message for r in caplog.records), (
        "expected the swallowed insert failure to be logged, not silent")

    # A second job, through the real (un-monkeypatched) connection scope,
    # still completes normally -- proves mark_terminal()/db_scope.close()
    # actually ran for the first job rather than leaving anything wedged
    # (same "second job still completes" proof style as
    # test_crack_cleans_up_even_if_new_connection_scope_raises below).
    engine.crack._new_connection_scope = real_new_connection_scope
    second_handle = engine.crack.start(handshake, wordlist_path)
    second_handle.wait_for_test(timeout=2.0)

    assert len(results) == 1
    assert results[0].job_id == second_handle.job_id


def test_crack_unlinks_outfile_only_after_result_is_committed(tmp_path):
    """Fix 3: the found-password outfile artifact is unlinked once (and only
    once) the key is durably committed to the DB -- not left on disk
    indefinitely, and not unlinked before the insert (which would risk
    losing the only record of the found key if insert then failed)."""
    engine = _make_engine(tmp_path, _base_script([]), hashcat_key=FOUND_KEY)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    results = []
    engine.subscribe(results.append, CrackResult)

    wordlist_path = tmp_path / "wordlist.txt"
    handle = engine.crack.start(handshake, wordlist_path)
    handle.wait_for_test(timeout=2.0)

    # Same path-construction formula as crack.py's own _drive.
    outfile_path = handshake.cap_file_path.parent / f"{handle.job_id}-hashcat.outfile"
    assert not outfile_path.exists()

    assert len(results) == 1
    result_row = results[0].result
    assert isinstance(result_row.outcome, Found)
    # The key read back is correct -- proving the unlink happened AFTER the
    # value was durably committed, not instead of committing it.
    assert result_row.outcome.key == FOUND_KEY
    assert engine.crack.list_results(handshake) == [result_row]


def test_crack_unlinks_outfile_even_if_publish_raises_after_insert_succeeds(tmp_path):
    """The actual HIGH-severity bug a single shared try/except around
    insert+publish+unlink had: if insert() succeeds but publish() itself
    raises, the unlink was skipped even though the key was already safely
    committed -- leaving the plaintext passphrase sitting on disk
    indefinitely (nothing else in this codebase ever cleans up this
    artifact). EventBus.publish() already catches an ordinary raising
    SUBSCRIBER internally (events.py) and never re-raises, so this
    monkeypatches self._bus.publish itself to raise directly -- the only way
    to actually exercise this path, proving the unlink no longer depends on
    it."""
    engine = _make_engine(tmp_path, _base_script([]), hashcat_key=FOUND_KEY)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    real_publish = engine.crack._bus.publish

    def raise_on_crack_result(event):
        if isinstance(event, CrackResult):
            raise RuntimeError("simulated publish failure")
        return real_publish(event)

    engine.crack._bus.publish = raise_on_crack_result

    wordlist_path = tmp_path / "wordlist.txt"
    handle = engine.crack.start(handshake, wordlist_path)
    handle.wait_for_test(timeout=2.0)  # must not hang despite the forced publish failure

    outfile_path = handshake.cap_file_path.parent / f"{handle.job_id}-hashcat.outfile"
    assert not outfile_path.exists()  # unlinked despite publish() raising

    # The row itself was still committed -- insert() succeeded before publish
    # ever ran -- confirmed via the real (un-monkeypatched) read path.
    results = engine.crack.list_results(handshake)
    assert len(results) == 1
    assert results[0].outcome.key == FOUND_KEY


def test_crack_passes_absolute_wordlist_path_to_hashcat(tmp_path):
    """Fix 4: a relative wordlist_path must still reach hashcat's own argv as
    an absolute path -- a relative path starting with "-" would otherwise be
    parsed by hashcat's own argv parser as an option rather than the wordlist
    argument. Asserting absoluteness for an ordinary relative path (not one
    that actually starts with "-") is sufficient: the fix resolves
    unconditionally at the point of use, not just when it detects a leading
    "-"."""
    spawned_argvs = []
    base_on_spawn = _make_on_spawn(None)

    def recording_on_spawn(argv: list[str]) -> None:
        spawned_argvs.append(argv)
        base_on_spawn(argv)

    engine = _make_engine(tmp_path, _base_script([]), on_spawn=recording_on_spawn)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    wordlist_path = Path("some-relative-wordlist.txt")  # deliberately relative --
    # cwd is wherever pytest runs from (not tmp_path), so this genuinely
    # exercises wordlist_path.resolve() rather than already being absolute.
    handle = engine.crack.start(handshake, wordlist_path)
    handle.wait_for_test(timeout=2.0)

    hashcat_argvs = [argv for argv in spawned_argvs if argv[0] == "hashcat"]
    assert len(hashcat_argvs) == 1
    # argv: ["hashcat", "-m", "22000", <hash_file>, <wordlist>, ...] -- see crack.py's _drive.
    wordlist_token = hashcat_argvs[0][4]
    assert Path(wordlist_token).is_absolute()


# --- ADR-0015: the hcxpcapngtool conversion step's own failure modes --------
# hashcat never even gets spawned in any of these -- the conversion step is
# what's under test, so each scenario's assertion that results in exactly one
# CrackResult with no plaintext and no COMPLETED stop_reason is also implicit
# proof hashcat wasn't reached (a COMPLETED/Found result could only come from
# a real hashcat run writing outfile_path).

def test_crack_reports_error_when_conversion_finds_no_matching_bssid(tmp_path):
    """hcxpcapngtool "succeeds" (real exit 0) but the only hash it extracted is
    for a DIFFERENT network than this Handshake's own Target -- simulates a
    capture that, for whatever reason, didn't stay single-network the way
    capture.py's own --bssid filter is relied on to guarantee. filter_hc22000_
    lines_by_bssid (parse.py) must reject it; crack.py must then skip hashcat
    entirely rather than attempt an unauthorized network's hash (ADR-0015 --
    cracking's authorization is inherited from the Handshake's own Target,
    CONTEXT.md)."""
    engine = _make_engine(tmp_path, _base_script([]), hashcat_key=None, conversion_bssid_hex=WRONG_BSSID_HEX)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    results = []
    engine.subscribe(results.append, CrackResult)

    wordlist_path = tmp_path / "wordlist.txt"
    handle = engine.crack.start(handshake, wordlist_path)
    handle.wait_for_test(timeout=2.0)

    assert len(results) == 1
    result_row = results[0].result
    assert isinstance(result_row.outcome, Exhausted)
    assert result_row.stop_reason == StopReason.ERROR

    assert engine.crack.list_results(handshake) == [result_row]


def test_crack_reports_error_when_conversion_process_fails(tmp_path):
    """hcxpcapngtool itself exits non-zero (a real crash, an unreadable .cap --
    same "don't trust an external tool's exit to mean success" posture ADR-
    0009 already established for hashcat, applied to this new call site too).
    Same ERROR classification as every other "never got a real crack attempt
    in" case -- no new CrackOutcome variant (domain.py's sealed set is
    unchanged)."""
    engine = _make_engine(tmp_path, _base_script([]), hashcat_key=None, returncodes={"hcxpcapngtool": 1})
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    results = []
    engine.subscribe(results.append, CrackResult)

    wordlist_path = tmp_path / "wordlist.txt"
    handle = engine.crack.start(handshake, wordlist_path)
    handle.wait_for_test(timeout=2.0)

    assert len(results) == 1
    result_row = results[0].result
    assert isinstance(result_row.outcome, Exhausted)
    assert result_row.stop_reason == StopReason.ERROR

    assert engine.crack.list_results(handshake) == [result_row]


def test_crack_reports_error_when_conversion_times_out(tmp_path):
    """hcxpcapngtool never exits at all -- _await_conversion's bounded wait
    (ADR-0015, the same ProcHandle.poll()-based idiom ADR-0008/0011 already
    established) must still kill it and give up, not hang the job the way an
    unbounded handle.wait() would have. engine.crack._conversion_timeout_s is
    monkeypatched directly to keep this test fast -- same established
    precedent as test_crack_cleans_up_even_if_new_connection_scope_raises
    below, reaching into a private attribute of a constructed Crack via
    Engine, rather than threading a conversion_timeout override through
    Engine's own constructor for a tunable nothing else needs."""
    engine = _make_engine(
        tmp_path, _base_script([]), hashcat_key=None,
        running_polls={"hcxpcapngtool": 10_000},  # never exits on its own within this test
    )
    engine.crack._conversion_timeout_s = 0.05
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    results = []
    engine.subscribe(results.append, CrackResult)

    wordlist_path = tmp_path / "wordlist.txt"
    started = time.monotonic()
    handle = engine.crack.start(handshake, wordlist_path)
    handle.wait_for_test(timeout=2.0)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"took {elapsed:.2f}s -- expected to give up at the ~0.05s conversion timeout"
    assert len(results) == 1
    result_row = results[0].result
    assert isinstance(result_row.outcome, Exhausted)
    assert result_row.stop_reason == StopReason.ERROR

    assert engine.crack.list_results(handshake) == [result_row]


def test_crack_cancelled_during_conversion(tmp_path):
    """Cancelling while hcxpcapngtool is still running must end the job via
    the CANCELLED path, not ERROR -- same precedence rule crack.py's finally
    block already applies once hashcat is running (token.is_cancelled() wins
    over any other signal); this proves it holds for the new conversion step
    too, on a real thread (not scripted-instant), same reasoning as this
    file's own module docstring point 1."""
    engine = _make_engine(
        tmp_path, _base_script([]), hashcat_key=None,
        running_polls={"hcxpcapngtool": 10_000},
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    results = []
    engine.subscribe(results.append, CrackResult)

    wordlist_path = tmp_path / "wordlist.txt"
    handle = engine.crack.start(handshake, wordlist_path)
    time.sleep(PRE_CANCEL_SETTLE_S)  # let the conversion wait loop actually start -- see module docstring point 1
    handle.cancel()
    handle.wait_for_test(timeout=2.0)

    assert len(results) == 1
    result_row = results[0].result
    assert isinstance(result_row.outcome, Aborted)
    assert result_row.stop_reason == StopReason.CANCELLED

    assert engine.crack.list_results(handshake) == [result_row]


def test_crack_cleans_up_even_if_new_connection_scope_raises(tmp_path):
    """ADR-0009 regression: _drive used to call self._new_connection_scope()
    BEFORE its own try:, so a raise there skipped `finally` entirely -- no
    mark_terminal() call at all. Crack takes no RF reservation (unlike
    Discovery/Capture/Enumerate -- "hashcat doesn't touch the radio"), so the
    externally visible symptom isn't AdapterBusy; it's JobRegistry's
    in-memory bookkeeping for the job never getting cleaned up, i.e.
    wait_for_test() hanging forever. Proves the fix: the first job's thread
    still terminates (no row is written for it -- db_scope never opened, so
    no CrackResult publishes either, per crack.py's own guard), and a second
    crack job started right after still completes normally.
    """
    engine = _make_engine(tmp_path, _base_script([]), hashcat_key=None)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    handshake = _capture_handshake(engine, target)

    real_new_connection_scope = engine.crack._new_connection_scope
    calls = {"n": 0}

    def raise_on_first_call(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection scope failed")
        return real_new_connection_scope(*args, **kwargs)

    engine.crack._new_connection_scope = raise_on_first_call

    results = []
    engine.subscribe(results.append, CrackResult)

    wordlist_path = tmp_path / "wordlist.txt"

    # No except clause in Crack._drive -- the raise above propagates out of
    # the (daemon) thread uncaught, same as any other unexpected exception
    # there. Suppress the default excepthook's traceback spam for this
    # expected-and-scripted case, same pattern test_discovery_acceptance.py's
    # and test_capture_acceptance.py's own ADR-0009 regression tests use.
    original_hook = threading.excepthook
    threading.excepthook = lambda args: None
    try:
        first_handle = engine.crack.start(handshake, wordlist_path)
        first_handle.wait_for_test(timeout=2.0)  # must not hang
    finally:
        threading.excepthook = original_hook

    assert results == []  # no connection ever opened -- nothing to write or publish

    # The real assertion: JobRegistry's in-memory bookkeeping for the first
    # job was cleaned up correctly (mark_terminal ran, even with repo=None,
    # falling back to the main connection from the wrong thread -- exactly
    # what jobs.py's own mark_terminal hardening guards against) -- a second
    # crack job right after completes normally, proving the live session
    # isn't wedged.
    second_handle = engine.crack.start(handshake, wordlist_path)
    second_handle.wait_for_test(timeout=2.0)

    assert len(results) == 1
    assert results[0].job_id == second_handle.job_id


def _make_stress_on_spawn(wordlist_to_key: dict[str, str]) -> Callable[[list[str]], None]:
    """Same side effects as _make_on_spawn above, except the hashcat found key
    is looked up per-job from its own wordlist_path (argv[4] -- see crack.py's
    _drive argv) instead of one shared constant -- each concurrent job in the
    stress test below needs its OWN distinct outfile content to prove results
    don't cross-contaminate between threads."""

    def on_spawn(argv: list[str]) -> None:
        if argv[0] == "airodump-ng" and "-w" in argv:
            # airodump-ng appends "-01.cap" to the given prefix itself -- see
            # capture.py's ThingsToChange-item-3 comment for how this was
            # confirmed against the real binary.
            cap_prefix = argv[argv.index("-w") + 1]
            cap_path = Path(f"{cap_prefix}-01.cap")
            cap_path.write_bytes(CAP_FILE_BYTES)
        elif argv[0] == "hcxpcapngtool":
            hash_file_path = Path(argv[argv.index("-o") + 1])
            hash_file_path.write_text(_hc22000_line() + "\n")
        elif argv[0] == "hashcat":
            key = wordlist_to_key.get(argv[4])
            if key is not None:
                outfile_path = Path(argv[argv.index("--outfile") + 1])
                outfile_path.write_text(key + "\n")

    return on_spawn


def test_concurrent_crack_jobs_all_complete_with_distinct_results_and_no_sqlite_errors(tmp_path):
    """Proves the connection-per-thread fix (docs/roadmap.md Phase 2 item 4)
    actually closes the race it was designed to close, not just that the
    happy-path single-job tests above still pass. Before the fix, every
    _drive thread wrote through the SAME unsynchronized sqlite3 connection;
    a second job starting immediately after a first one's wait_for_test()
    returned could hit sqlite3.OperationalError on that shared connection
    (see "Current state" in docs/roadmap.md, and jobs.py's own mark_terminal
    ordering fix for the first, narrower instance of this race). Crack is the
    only driver that can be started genuinely concurrently through one Engine
    without AdapterBusy serializing the starts -- it takes no RF reservation
    (crack.py's own comment: "hashcat doesn't touch the radio"), unlike
    Discovery/Capture/Enumerate.
    """
    job_count = 8
    wordlist_to_key: dict[str, str] = {}
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={
                "airmon-ng": ["monitor mode already enabled on wlan0"],
                "airodump-ng": [],  # content unused -- see AIRCRACK_HANDSHAKE_FOUND's comment
                "aircrack-ng": [AIRCRACK_HANDSHAKE_FOUND],
                "hcxpcapngtool": [],  # content unused -- on_spawn writes the hc22000 file directly (ADR-0015)
                "hashcat": [],  # outcome decided by outfile content only, not scripted stdout
            },
            on_spawn=_make_stress_on_spawn(wordlist_to_key),
            running_polls={"airodump-ng": 1},
        ),
        capture_handshake_check_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    # Build job_count REAL Handshakes first -- sequentially, since Capture (unlike
    # Crack) does take an RF reservation. Each capture's cap_path is keyed by its
    # own job_id (capture.py's _drive), so these are genuinely distinct rows, not
    # job_count references to the same one.
    handshakes = [_capture_handshake(engine, target) for _ in range(job_count)]
    assert len({h.id for h in handshakes}) == job_count

    results_by_job_id = {}
    engine.subscribe(lambda e: results_by_job_id.setdefault(e.job_id, e), CrackResult)

    # Catches an sqlite3 error (or any other exception) raised inside a _drive
    # thread that a naive read of "did wait_for_test() return" could otherwise
    # miss -- exactly the kind of failure this refactor is supposed to prevent.
    # (This exact gap bit tests/test_jobs.py during this same session: a
    # background thread's sqlite3.ProgrammingError was silently swallowed by
    # pytest into a warning instead of a failure until this hook was added.)
    thread_exceptions = []
    original_hook = threading.excepthook
    threading.excepthook = thread_exceptions.append
    try:
        handles = []
        for i, handshake in enumerate(handshakes):
            wordlist_path = tmp_path / f"wordlist-{i}.txt"
            expected_key = f"key-{i}"
            wordlist_to_key[str(wordlist_path)] = expected_key
            handle = engine.crack.start(handshake, wordlist_path)
            handles.append((handle, handshake, expected_key))
            # No sleep, no waiting on the previous job -- fired back-to-back,
            # exactly the shape that reproduced the race being tested for.

        for handle, _, _ in handles:
            handle.wait_for_test(timeout=5.0)
    finally:
        threading.excepthook = original_hook

    assert thread_exceptions == []

    assert len(results_by_job_id) == job_count
    for handle, handshake, expected_key in handles:
        result_row = results_by_job_id[handle.job_id].result
        assert isinstance(result_row.outcome, Found)
        assert result_row.outcome.key == expected_key
        assert result_row.handshake_id == handshake.id
        assert result_row.stop_reason == StopReason.COMPLETED

    # Independently readable back through the main connection too, not just
    # observed via the published events -- proves the writes are actually
    # durable, not merely visible to whichever thread wrote them.
    all_results = engine.crack.list_results()
    assert len(all_results) == job_count
    assert {r.handshake_id for r in all_results} == {h.id for h in handshakes}
    assert len({r.id for r in all_results}) == job_count  # distinct rows, no overwritten/reused id


# --- _format_hashrate ---------------------------------------------------------
# Plain function tests, no Engine/FakeProcRunner needed.

@pytest.mark.parametrize(
    "h_per_s, expected",
    [
        (0, "0 H/s"),  # the edge case _format_hashrate's own TODO calls out --
        # avoid a spurious "0.0 H/s".
        (500, "500.0 H/s"),  # H/s range, no scaling.
        (12345, "12.3 kH/s"),  # kH/s range.
        (12345678, "12.3 MH/s"),  # MH/s range -- the exact worked example from
        # _format_hashrate's own docstring.
    ],
)
def test_format_hashrate(h_per_s, expected):
    assert _format_hashrate(h_per_s) == expected
