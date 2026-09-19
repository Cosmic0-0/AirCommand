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
    extra settling needed for it. (An earlier version of this helper also
    slept here to work around a since-fixed jobs.py race -- see the module
    docstring's parenthetical -- no longer needed now that mark_terminal()
    itself orders its DB write before its event.)"""
    captured: list[HandshakeCaptured] = []
    subscription = engine.subscribe(captured.append, HandshakeCaptured)
    handle = engine.capture.start_passive(target)
    handle.wait_for_test(timeout=2.0)
    subscription.unsubscribe()
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
