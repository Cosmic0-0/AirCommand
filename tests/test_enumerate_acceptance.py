"""FakeProcRunner -> nmap -> NmapScanCompleted -> SQLite. The successful-scan
scenario exercises Enumerator directly (not through a real Engine); the two
gate scenarios (AdapterBusy, NotATargetError) go through a real Engine, same
style as test_capture_acceptance.py.

get_subnet injection: Engine.__init__ wires Enumerator with no get_subnet
kwarg at all (see engine.py -- `Enumerator(self.targets, self._db.enum_results,
self._bus, self._jobs, self._rf, self._proc, self._db.new_connection_scope)`,
production always wants the real get_interface_subnet default), so a test
needing a *fake* subnet can't reach it through Engine. _make_enumerator() below
instead builds Allowlist/
JobRegistry/RadioController by hand and constructs Enumerator directly -- the
same shape as test_allowlist.py's make_allowlist() building an Allowlist
directly instead of through Engine, extended with the extra collaborators
Enumerator needs. The AdapterBusy/NotATargetError scenarios never reach
get_subnet at all (both raise before self._get_subnet is ever called), so they
use a real Engine instead, matching test_capture_acceptance.py's own equivalent
tests exactly.

Real-thread-timing subtlety: checked against what _drive actually does, not
assumed by analogy to Capture/Crack -- there is none to work around here.
- The successful-scan scenario's scripted "nmap" output is a single-element
  list (the whole XML document -- see the fixture note below), so the driver
  thread has nothing to iterate mid-flight; wait_for_test() alone is enough,
  there's no cancel()-race window to land in.
- The AdapterBusy scenario's reservation is held by Discovery's driver thread,
  not Enumerator's -- identical recipe to test_capture_acceptance.py's own
  AdapterBusy test, already verified reliable on this system there.
- NotATargetError raises before any thread is spawned.
- get_interface_subnet is called directly, no thread involved.
This matches what _drive's own TODO comment (enumerate.py) says: cancellation
isn't meaningfully checkable mid-scan at all, since nmap's -oX - output is
buffered to completion rather than iterated line by line.

Fixture note: nmap's real -oX output is one XML document, not a line-oriented
stream, and _drive's `b"".join(l.encode() for l in handle.lines())` joins
scripted lines with NO separator -- splitting the document across multiple
scripted lines would need each one to carry its own line ending to reconstruct
correctly. Putting the whole document in a single scripted list element
sidesteps that entirely.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest

from aircommand.core.allowlist import Allowlist, NotATargetError
from aircommand.core.domain import MacAddress
from aircommand.core.engine import Engine
from aircommand.core.enumerate import Enumerator, get_interface_subnet
from aircommand.core.events import EnumerationFailed, EventBus, NmapScanCompleted
from aircommand.core.jobs import JobRegistry
from aircommand.core.persistence.db import Database
from aircommand.core.procutil import FakeProcRunner
from aircommand.core.rf import AdapterBusy, RadioController

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")

# A full nmap -oX document, two hosts: one with a <hostnames> entry and one
# open + one closed port (closed must never surface in open_ports), one with
# no <hostnames> element at all and a single open port -- real variation
# nmap's schema allows, per parse_nmap_xml's own TODO (host/address/hostnames/
# hostname/ports/port/state). Put in ONE scripted list element -- see module
# docstring's fixture note.
NMAP_XML = """<?xml version="1.0"?>
<nmaprun>
<host>
<status state="up"/>
<address addr="192.168.1.5" addrtype="ipv4"/>
<hostnames>
<hostname name="printer.local" type="user"/>
</hostnames>
<ports>
<port protocol="tcp" portid="80"><state state="open"/></port>
<port protocol="tcp" portid="443"><state state="closed"/></port>
</ports>
</host>
<host>
<status state="up"/>
<address addr="192.168.1.10" addrtype="ipv4"/>
<ports>
<port protocol="tcp" portid="22"><state state="open"/></port>
</ports>
</host>
</nmaprun>
"""


def _make_enumerator(script: dict, get_subnet) -> tuple[Enumerator, Allowlist, EventBus, JobRegistry]:
    """Builds Allowlist/JobRegistry/RadioController by hand and constructs
    Enumerator directly, bypassing Engine -- see module docstring for why.

    "iw" defaults to empty: RadioController.reserve(MANAGED, ...) (what
    start_scan calls) now runs a real-mode sync check once per call when
    self._monitor_adapter reads None (ADR-0016) -- empty "iw" output has no
    "type" line for it to find, so it's a harmless no-op, same as every
    other RadioController test not specifically exercising that check
    (tests/test_rf.py)."""
    db = Database(":memory:")
    bus = EventBus()
    jobs = JobRegistry(db.jobs)
    script.setdefault("iw", [])
    proc = FakeProcRunner(script=script)
    rf = RadioController("wlan0", proc)
    allowlist = Allowlist(db.targets, bus)
    enumerator = Enumerator(
        allowlist, db.enum_results, bus, jobs, rf, proc, db.new_connection_scope, get_subnet=get_subnet
    )
    return enumerator, allowlist, bus, jobs


def test_successful_scan_publishes_nmap_scan_completed_with_both_hosts():
    get_subnet_calls = []

    def fake_get_subnet(adapter: str) -> str:
        get_subnet_calls.append(adapter)
        return "192.168.1.0/24"

    enumerator, allowlist, bus, _ = _make_enumerator({"nmap": [NMAP_XML]}, fake_get_subnet)
    target = allowlist.add(BSSID_1, "Test-SSID", 6, "My house")

    completed = []
    bus.subscribe(completed.append, NmapScanCompleted)

    handle = enumerator.start_scan(target)
    handle.wait_for_test(timeout=2.0)

    assert get_subnet_calls == ["wlan0"]

    assert len(completed) == 1
    event = completed[0]
    assert event.job_id == handle.job_id
    assert event.target_id == target.id

    hosts_by_ip = {host.ip: host for host in event.hosts}
    assert set(hosts_by_ip) == {"192.168.1.5", "192.168.1.10"}
    assert hosts_by_ip["192.168.1.5"].hostname == "printer.local"
    assert hosts_by_ip["192.168.1.5"].open_ports == (80,)
    assert hosts_by_ip["192.168.1.10"].hostname is None
    assert hosts_by_ip["192.168.1.10"].open_ports == (22,)


# --- ADR-0016: bounded retry around get_subnet -------------------------------
# A single immediate attempt can't tell "the interface is still reconnecting
# after a real monitor->managed switch" (rf.py's own ADR-0016 fix) apart from
# "the operator genuinely never joined this network" -- both raise the
# identical OSError. _await_subnet retries for a bounded window instead.

def test_subnet_retry_succeeds_once_a_transient_oserror_clears():
    """Proves the retry itself, not just that the eventual success path still
    works: get_subnet raises twice (simulating "still reconnecting"), then
    succeeds on the third call -- the scan must still complete normally."""
    call_count = {"n": 0}

    def flaky_get_subnet(adapter: str) -> str:
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise OSError("network is unreachable")
        return "192.168.1.0/24"

    enumerator, allowlist, bus, _ = _make_enumerator({"nmap": [NMAP_XML]}, flaky_get_subnet)
    enumerator._subnet_retry_interval_s = 0.01   # real but tiny -- keeps this test fast
    target = allowlist.add(BSSID_1, "Test-SSID", 6, "My house")

    completed = []
    bus.subscribe(completed.append, NmapScanCompleted)

    handle = enumerator.start_scan(target)
    handle.wait_for_test(timeout=2.0)

    assert call_count["n"] == 3
    assert len(completed) == 1


def test_subnet_retry_gives_up_after_its_own_bounded_timeout():
    """The retry is genuinely bounded, not an accidental infinite loop: a
    get_subnet that NEVER succeeds still ends (EnumerationFailed, not a hang)
    once _subnet_wait_timeout_s elapses -- checked against a real wall-clock
    duration, not just that it eventually returns."""

    def always_raises(adapter: str) -> str:
        raise OSError("network is unreachable")

    enumerator, allowlist, bus, _ = _make_enumerator({}, always_raises)
    enumerator._subnet_wait_timeout_s = 0.3
    enumerator._subnet_retry_interval_s = 0.05
    target = allowlist.add(BSSID_1, "Test-SSID", 6, "My house")

    failed = []
    bus.subscribe(failed.append, EnumerationFailed)

    original_hook = threading.excepthook   # same reason as the existing always-fails test below
    threading.excepthook = lambda args: None
    try:
        started = time.monotonic()
        handle = enumerator.start_scan(target)
        handle.wait_for_test(timeout=2.0)
        elapsed = time.monotonic() - started
    finally:
        threading.excepthook = original_hook

    assert 0.3 <= elapsed < 2.0, f"took {elapsed:.2f}s -- expected to give up at the ~0.3s bound"
    assert len(failed) == 1


def test_subnet_retry_is_cancellable():
    """Cancelling while the retry is in-flight must end it promptly, not wait
    out the full timeout -- this wait didn't exist before ADR-0016, so there
    was nothing to cancel here previously; now that there is, it shouldn't
    ignore a cancel that arrives while it's waiting."""

    def always_raises(adapter: str) -> str:
        raise OSError("network is unreachable")

    enumerator, allowlist, bus, _ = _make_enumerator({}, always_raises)
    enumerator._subnet_wait_timeout_s = 30.0   # long enough that only cancellation explains an early return
    enumerator._subnet_retry_interval_s = 0.05
    target = allowlist.add(BSSID_1, "Test-SSID", 6, "My house")

    original_hook = threading.excepthook
    threading.excepthook = lambda args: None
    try:
        started = time.monotonic()
        handle = enumerator.start_scan(target)
        time.sleep(0.1)   # let a couple of real retry iterations happen first
        handle.cancel()
        handle.wait_for_test(timeout=2.0)
        elapsed = time.monotonic() - started
    finally:
        threading.excepthook = original_hook

    assert elapsed < 2.0, f"took {elapsed:.2f}s -- cancellation should have ended the retry almost immediately"


def test_adapter_busy_propagates_synchronously_and_does_not_start_a_job(tmp_path):
    # Discovery's _drive loop is a plain wall-clock loop now, driven by
    # ProcHandle.poll() for liveness, not handle.lines() content -- see
    # test_capture_acceptance.py's module docstring for the full real-hardware
    # finding. running_polls below (plus a tiny drive_tick_interval) keeps
    # Discovery's driver thread demonstrably still holding the RF reservation
    # by the time this test's very next line (the enumerate.start_scan() call)
    # runs -- same idiom as that file's own AdapterBusy test.
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        # RadioController.reserve() now really spawns "airmon-ng" on its first
        # monitor-mode use (docs/roadmap.md Phase 2 item 1) -- no rename-
        # announcement line, so it falls back to the original "wlan0" name.
        proc=FakeProcRunner(
            script={
                "airmon-ng": ["monitor mode already enabled on wlan0"],
                "airodump-ng": [],
            },
            running_polls={"airodump-ng": 1000},
        ),
        drive_tick_interval=timedelta(seconds=0.001),
    )
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    # Reserves the adapter synchronously inside .start() itself, before
    # Discovery's driver thread even runs -- deterministic, same recipe as
    # test_capture_acceptance.py's equivalent test.
    engine.discovery.start()

    with pytest.raises(AdapterBusy):
        engine.enumerate.start_scan(target)


def test_not_a_target_error_when_target_removed_after_fetch(tmp_path):
    # require_target() raises before Enumerator ever touches the adapter or
    # self._proc, so no tool needs to be scripted at all here.
    engine = Engine(db_path=":memory:", work_dir=tmp_path, adapter="wlan0", proc=FakeProcRunner(script={}))
    stale_target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    engine.targets.remove(BSSID_1)

    with pytest.raises(NotATargetError):
        engine.enumerate.start_scan(stale_target)


def test_failed_scan_publishes_enumeration_failed_and_cleans_up():
    def raising_get_subnet(adapter: str) -> str:
        raise OSError("network is unreachable")

    enumerator, allowlist, bus, jobs = _make_enumerator({}, raising_get_subnet)
    # ADR-0016's bounded retry would otherwise spend its full real timeout
    # retrying a fake that always raises -- same reach-into-the-private-
    # attribute precedent test_crack_acceptance.py's own ADR-0015 tests use
    # for an identical reason (keeps this test fast without threading a new
    # override through every construction path).
    enumerator._subnet_wait_timeout_s = 0
    target = allowlist.add(BSSID_1, "Test-SSID", 6, "My house")

    failed = []
    bus.subscribe(failed.append, EnumerationFailed)

    # pytest.warns(pytest.PytestUnhandledThreadExceptionWarning) around the
    # wait_for_test() call (the originally-specified approach here) does NOT
    # work on this repo's pytest (9.1.1): verified empirically that it fails
    # deterministically (0/5, even with a 1s sleep inside the `with` block) --
    # not a race. _pytest/threadexception.py's collect_thread_exception (which
    # is what actually calls warnings.warn(...)) is registered as a `trylast`
    # impl of the SAME `pytest_runtest_call` hook whose normal-priority impl
    # (_pytest/runner.py) is what invokes the test function itself -- so the
    # warning is only ever emitted *after* the whole test function has already
    # returned, never reachable by a pytest.warns(...) block placed inside the
    # test body, regardless of how long it sleeps first. Using this codebase's
    # own already-established pattern for the exact same problem instead (see
    # test_crack_acceptance.py's stress test): swap threading.excepthook
    # directly to prove the exception really propagated out of the thread
    # uncaught, rather than being silently swallowed.
    thread_exceptions = []
    original_hook = threading.excepthook
    threading.excepthook = thread_exceptions.append
    try:
        handle = enumerator.start_scan(target)
        handle.wait_for_test(timeout=2.0)
        # wait_for_test() unblocks the instant mark_terminal() sets its internal
        # event, inside `finally` -- slightly BEFORE the exception actually
        # finishes propagating out of the thread and hits threading.excepthook.
        # Poll briefly instead of assuming either ordering.
        deadline = time.monotonic() + 2.0
        while not thread_exceptions and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        threading.excepthook = original_hook

    assert len(failed) == 1
    assert failed[0].job_id == handle.job_id
    assert failed[0].target_id == target.id
    assert "network is unreachable" in failed[0].error
    # finally still ran despite the exception -- job row cleared, RF reservation released
    assert jobs.active_job_ids() == []

    # the exception really propagated out of the thread uncaught, per
    # enumerate.py's own "still logged via the default threading excepthook" comment
    assert len(thread_exceptions) == 1
    assert thread_exceptions[0].exc_type is OSError


def test_enumerate_releases_rf_reservation_even_if_new_connection_scope_raises():
    """ADR-0009 regression: _drive used to call self._new_connection_scope()
    BEFORE its own try:, so a raise there skipped `finally` (the RF release
    inside it included) entirely -- leaking the reservation for the rest of
    the live session. Proves both halves of the fix: the first job's thread
    still terminates (wait_for_test doesn't hang) and the reservation it held
    is genuinely released -- a second start_scan() right after succeeds
    instead of raising AdapterBusy.
    """

    def fake_get_subnet(adapter: str) -> str:
        return "192.168.1.0/24"

    enumerator, allowlist, bus, jobs = _make_enumerator({"nmap": [NMAP_XML]}, fake_get_subnet)
    target = allowlist.add(BSSID_1, "Test-SSID", 6, "My house")

    real_new_connection_scope = enumerator._new_connection_scope
    calls = {"n": 0}

    def raise_on_first_call(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection scope failed")
        return real_new_connection_scope(*args, **kwargs)

    enumerator._new_connection_scope = raise_on_first_call

    # Unlike Discovery/Capture, Enumerator._drive HAS an except Exception clause
    # that re-raises after publishing EnumerationFailed -- the raise above is
    # still caught there (it sits inside the now-widened try) before
    # propagating out of the thread uncaught. Suppress the default excepthook's
    # traceback spam for this expected-and-scripted case, same pattern this
    # file's own test_failed_scan_publishes_enumeration_failed_and_cleans_up
    # already uses.
    original_hook = threading.excepthook
    threading.excepthook = lambda args: None
    try:
        first_handle = enumerator.start_scan(target)
        first_handle.wait_for_test(timeout=2.0)  # must not hang
    finally:
        threading.excepthook = original_hook

    # The real assertion: RadioController's reservation from the first (failed)
    # job was released -- a second start_scan() right after succeeds rather
    # than raising AdapterBusy.
    second_handle = enumerator.start_scan(target)
    second_handle.wait_for_test(timeout=2.0)


def test_get_interface_subnet_against_real_loopback_interface():
    """Real ioctl call, no fake, no mocking -- validates the struct-packing/
    ioctl logic against actual Linux behavior rather than only against
    research. "lo" always carries 127.0.0.1/8 on any Linux box (sandboxed or
    not) and needs no root to read its own address."""
    assert get_interface_subnet("lo") == "127.0.0.0/8"
