"""Real-subprocess verification for docs/adr/0015 -- the hcxpcapngtool
conversion step Crack now runs before hashcat.

Spawns the ACTUAL installed hcxpcapngtool binary through the real
SubprocessRunner/_RealProcHandle (procutil.py), not FakeProcRunner. No sudo
and no wifi hardware needed: hcxpcapngtool always runs privileged=False in
crack.py, and this file only ever gives it a plain on-disk .cap file.

A genuine captured handshake (and the full end-to-end "does hashcat then
actually find the real password" proof) is deliberately NOT a fixture here --
same reasoning test_capture_real_subprocess.py's own module docstring and
docs/adr/0011's Consequences already settled for aircrack-ng: a hand-crafted
synthetic WPA handshake wasn't recognized by the real tool, and a genuine one
is a real passphrase for a real network, the same "no secrets" category
CLAUDE.md's repository-hygiene rule already covers for wordlist.txt -- not
something to check into this repo as test fixture data. That full chain (real
hcxpcapngtool -> real hashcat -> the correct plaintext) was instead verified
by hand against the reporting user's own real capture + wordlist while
writing ADR-0015; this file covers what IS safe and useful to keep as an
automated, portable regression: that the real binary, called the exact way
crack.py calls it, behaves correctly on an input with no handshake to find.

Skipped automatically (not failed) if hcxpcapngtool isn't on PATH -- this
machine has it installed, but CI or another dev's machine may not (ADR-0015:
it ships in the separate `hcxtools` package, not `hashcat` or
`aircrack-ng-suite`).
"""

from __future__ import annotations

import shutil

import pytest

from aircommand.core.crack import Crack
from aircommand.core.jobs import CancellationToken
from aircommand.core.procutil import SubprocessRunner

pytestmark = pytest.mark.skipif(
    shutil.which("hcxpcapngtool") is None, reason="hcxpcapngtool (hcxtools) not installed on this machine"
)

# Same minimal valid pcap global header (magic/version/tz/sigfigs/snaplen/
# linktype) test_capture_real_subprocess.py's header-only fixture already
# uses -- a complete, valid, EMPTY capture: no packet records, so definitely
# no handshake to extract.
_HEADER_ONLY_PCAP = bytes.fromhex("d4c3b2a1" "0200" "0400" "00000000" "00000000" "ffff0000" "69000000")


def _unprivileged_runner() -> SubprocessRunner:
    def _never_privileged(argv):
        raise AssertionError(f"hcxpcapngtool must never run privileged=True, got {argv}")

    return SubprocessRunner(sudo_run_privileged=_never_privileged)


def _make_bare_crack(**overrides) -> Crack:
    """A Crack instance with no real dependencies beyond what _await_conversion
    itself touches (self._conversion_timeout_s, self._tick_interval_s) --
    this test is a narrow, real-subprocess unit test of that one method, not
    a full Crack._drive integration (tests/test_crack_acceptance.py already
    covers the full flow, against FakeProcRunner). Every other constructor
    arg is an unused None stand-in, same pattern as test_capture_real_
    subprocess.py's own _make_bare_capture."""
    from datetime import timedelta

    return Crack(
        repo=None, bus=None, jobs=None, proc=None, new_connection_scope=None,
        tick_interval=timedelta(seconds=0.01),
        **overrides,
    )


def test_real_hcxpcapngtool_exits_cleanly_on_a_header_only_capture(tmp_path):
    """A real, valid, but empty .cap (header, zero packets) converts without
    hanging and without crashing -- exits 0 (hcxpcapngtool's own behavior for
    "nothing found", confirmed by hand while writing ADR-0015: it reports
    zero EAPOL pairs and still exits cleanly, it does not treat "nothing to
    extract" as a tool failure) and does not write the hash file at all, or
    writes an effectively empty one. Either way, Crack's own caller-side
    check (`converted and hash_file_path.exists()`, crack.py's _drive) must
    end up treating this as "nothing to crack" without ever reaching
    hashcat -- test_crack_acceptance.py's FakeProcRunner-based tests already
    prove that branching logic; this test proves the real binary's behavior
    the branching logic is built to handle."""
    empty_cap = tmp_path / "empty-01.cap"
    empty_cap.write_bytes(_HEADER_ONLY_PCAP)
    hash_file = tmp_path / "out.hc22000"

    runner = _unprivileged_runner()
    handle = runner.spawn(["hcxpcapngtool", "-o", str(hash_file), str(empty_cap)], privileged=False)

    crack = _make_bare_crack()
    token = CancellationToken()

    converted = crack._await_conversion(handle, token)

    assert converted is True  # the real binary exits 0 even when it found nothing to extract
    assert handle.poll() == 0
    # No crackable hash either way -- confirmed real behavior, not assumed:
    # hcxpcapngtool does not write -o's file at all when it extracts zero
    # EAPOL pairs, so crack.py's own `hash_file_path.exists()` check (not
    # exercised by this narrow a test) is what actually catches this case.
    assert not hash_file.exists() or hash_file.read_text().strip() == ""


def test_real_conversion_wait_is_cancellable(tmp_path):
    """Cancelling while the conversion step's own wait loop is in flight must
    kill the real process and return False promptly, not wait for it to
    finish on its own -- same ProcHandle.poll()-based idiom ADR-0008/0011
    already established. _await_conversion itself is generic over any
    ProcHandle (it doesn't know or care that crack.py only ever calls it on
    hcxpcapngtool), so a real but deliberately slow python3 stand-in proves
    the mechanism without racing a real hcxpcapngtool run's own (sub-second
    against this fixture) speed -- hcxpcapngtool itself has no flag to make
    it run slower on demand. test_crack_acceptance.py's test_crack_cancelled_
    during_conversion separately confirms this wiring end to end through
    Crack._drive, against FakeProcRunner."""
    runner = _unprivileged_runner()
    handle = runner.spawn(["python3", "-c", "import time; time.sleep(30)"], privileged=False)

    crack = _make_bare_crack()
    token = CancellationToken()
    token.cancel()  # already cancelled before _await_conversion even starts waiting

    converted = crack._await_conversion(handle, token)

    assert converted is False
    # The real process must actually be gone afterward -- not just abandoned.
    import time

    for _ in range(50):
        if handle.poll() is not None:
            break
        time.sleep(0.1)
    assert handle.poll() is not None, "the real process was left running after cancellation"
