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

Three real-thread-timing subtleties apply here -- the first two are the ones
test_capture_acceptance.py documents too, but checked here against what
crack.py's own _drive actually does rather than assumed by analogy; the third
is new, found while making these tests pass reliably:

1. Cancellation race (identical to test_capture_acceptance.py point 2): a
   plain, instantly-iterable scripted hashcat stdout lets the driver thread's
   loop exhaust before this thread's next line (the .cancel() call) ever runs,
   for the same threading.Thread.start()-hands-off-the-CPU reason documented
   there. _slow_hashcat_lines()/PRE_CANCEL_SETTLE_S below are the same fix:
   real (tiny) per-line delays plus a short real settle sleep before cancel(),
   so cancellation reliably lands mid-loop instead of racing an already-
   exhausted stream.

2. Terminal-event race -- checked, and it does NOT apply to CrackResult, for
   a reason specific to what crack.py's _drive actually does (this is NOT the
   same conclusion test_capture_acceptance.py draws for CaptureStopped, so
   don't copy that file's _wait_until()-after-wait_for_test() pattern here
   without re-deriving it):

   crack.py's _drive finally block calls `self._bus.publish(CrackResult(...))`
   BEFORE `self._jobs.mark_terminal(job_id)` -- both on the *same* driver
   thread, with nothing async in between. EventBus.publish() is documented
   (events.py) and implemented to be synchronous: "publish() does not return
   until every subscriber has been called". mark_terminal() is what sets the
   threading.Event that JobHandle.wait_for_test() blocks on (jobs.py). So by
   the time _drive reaches mark_terminal(), publish(CrackResult) has already
   fully returned -- every subscriber (including a plain list.append here)
   has already run. wait_for_test() returning is therefore already proof
   CrackResult has been observed; no extra polling helper is needed for it.

   (This is actually also true of capture.py's current _drive, which publishes
   CaptureStopped before calling mark_terminal the same way -- but
   test_capture_acceptance.py's own module docstring still describes the
   opposite, pre-fix ordering for Capture and polls anyway via _wait_until().
   That docstring looks stale relative to the code it's next to; out of scope
   to fix here since capture.py/test_capture_acceptance.py aren't files this
   task touches. Verified empirically too: this file's tests pass repeatedly,
   with no _wait_until()-style helper, using wait_for_test() alone.)

3. A different, genuinely pre-existing race, one level down from either of the
   above: JobRegistry.mark_terminal (jobs.py, out of scope to fix here) calls
   `event.set()` -- what unblocks wait_for_test() -- *before* its own
   `self._repo.mark_terminal(job_id)` DB delete+commit, against the single
   shared, unlocked sqlite3 connection every driver thread on an Engine writes
   through (db.py's Database docstring already flags that sharing as
   provisional). test_capture_acceptance.py never exercises this, because
   nothing there starts a second job on the same Engine immediately after
   wait_for_test() on a first one -- but cracking is exactly that shape
   (capture a Handshake, then immediately crack it), so _capture_handshake()
   below adds a short real settle sleep after wait_for_test() to avoid
   engine.crack.start()'s JobRepository write landing while Capture's driver
   thread is still mid-mark_terminal. See _capture_handshake()'s own docstring
   for the full account, including the exact exception this reproduced.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path
from typing import Callable, Optional

import pytest

from aircommand.core.domain import Aborted, Exhausted, Found, MacAddress, StopReason
from aircommand.core.engine import Engine
from aircommand.core.events import CrackProgress, CrackResult, HandshakeCaptured
from aircommand.core.parse import _format_hashrate
from aircommand.core.procutil import FakeProcRunner

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")

# Gets Capture's own driver to find a handshake on its very first check (same
# recipe, same reasoning, as test_capture_acceptance.py's
# test_passive_capture_finds_handshake_on_first_check): a plain, instantly-
# iterable list is fine here because the loop breaks out on iteration 1
# regardless, before any cancellation-race concern could ever apply.
CAPTURE_NOISE = [f"CH 6 ][ Elapsed: {i} s ][ 2024-01-01 10:00" for i in range(3)]
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


def _make_on_spawn(hashcat_key: Optional[str]) -> Callable[[list[str]], None]:
    """Combines two FakeProcRunner on_spawn side effects needed here:
    airodump-ng's -w <cap_path> (so Capture's _drive has a real file to
    sha256 -- same guarded check as test_capture_acceptance.py's
    _write_cap_file_on_spawn, and needed for the same reason: this file's
    aircrack-ng invocation also carries '-w', for its unrelated /dev/null
    wordlist argument), and hashcat's --outfile <path> (so Crack's _drive has
    a real file to read the cracked plaintext from). hashcat_key=None means
    "don't write the outfile at all" -- the wordlist-exhausted/cancelled
    scenarios, where a real hashcat run would leave no outfile behind either."""

    def on_spawn(argv: list[str]) -> None:
        if argv[0] == "airodump-ng" and "-w" in argv:
            cap_path = Path(argv[argv.index("-w") + 1])
            cap_path.write_bytes(CAP_FILE_BYTES)
        elif argv[0] == "hashcat" and hashcat_key is not None:
            outfile_path = Path(argv[argv.index("--outfile") + 1])
            outfile_path.write_text(hashcat_key + "\n")

    return on_spawn


def _make_engine(tmp_path, script: dict, hashcat_key: Optional[str] = None) -> Engine:
    return Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(script=script, on_spawn=_make_on_spawn(hashcat_key)),
        # Zero interval: Pacer.due() fires on every check -- same trick
        # test_capture_acceptance.py's _make_engine uses, needed here for the
        # same reason (Capture's handshake check runs as part of getting a
        # Handshake at all, even though this file is testing Crack).
        capture_handshake_check_interval=timedelta(seconds=0),
    )


def _capture_handshake(engine: Engine, target):
    """Drives a real (scripted) passive Capture to get a real Handshake to
    crack against -- Handshake is mint-restricted (domain.py), so this is the
    only legitimate way to obtain one, same as test_capture_acceptance.py's
    first test. HandshakeCaptured itself fires mid-loop, strictly before the
    loop can exit -- same reasoning as that file's DeauthFired note -- so
    wait_for_test() returning is already enough proof *that* has happened, no
    extra settling needed for it.

    The settle sleep below is for a different, genuinely pre-existing race,
    found while making this file's tests pass reliably: JobRegistry.mark_terminal
    (jobs.py) calls `event.set()` -- what unblocks wait_for_test() -- *before*
    `self._repo.mark_terminal(job_id)`, its own DB delete+commit against the
    single shared, unlocked sqlite3 connection (db.py's Database docstring
    already flags that sharing as provisional: "each job-DRIVER thread is
    eventually meant to open its own separate connection... not the case yet
    for this milestone's drivers"). Nothing in test_capture_acceptance.py ever
    exercises this, since nothing there starts a second job on the same Engine
    immediately after wait_for_test() on a first one -- but Crack usage is
    exactly that shape (capture a Handshake, then immediately crack it), and
    without a settle here, engine.crack.start()'s own JobRepository write can
    land while Capture's driver thread is still mid-mark_terminal, racing on
    the shared connection (observed directly: a real, intermittent
    sqlite3.OperationalError: "cannot commit - no transaction is active").
    jobs.py/db.py are out of scope for this task to fix, so this is a
    test-side settle, not a production fix -- same idiom as
    test_capture_acceptance.py's own PRE_CANCEL_SETTLE_S for a different real
    thread-timing race."""
    captured: list[HandshakeCaptured] = []
    subscription = engine.subscribe(captured.append, HandshakeCaptured)
    handle = engine.capture.start_passive(target)
    handle.wait_for_test(timeout=2.0)
    subscription.unsubscribe()
    time.sleep(PRE_CANCEL_SETTLE_S)  # see docstring above -- lets Capture's
    # driver thread finish its own mark_terminal DB write before this test
    # starts a second job (Crack) on the same shared connection.
    assert len(captured) == 1
    return captured[0].handshake


def _base_script(hashcat_lines) -> dict:
    return {
        "airodump-ng": CAPTURE_NOISE,
        "aircrack-ng": [AIRCRACK_HANDSHAKE_FOUND],
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
