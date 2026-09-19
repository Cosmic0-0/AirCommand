"""FakeProcRunner -> nmap -> NmapScanCompleted -> SQLite. The successful-scan
scenario exercises Enumerator directly (not through a real Engine); the two
gate scenarios (AdapterBusy, NotATargetError) go through a real Engine, same
style as test_capture_acceptance.py.

get_subnet injection: Engine.__init__ wires Enumerator with no get_subnet
kwarg at all (see engine.py -- `Enumerator(self.targets, self._db.enum_results,
self._bus, self._jobs, self._rf, self._proc)`, production always wants the
real get_interface_subnet default), so a test needing a *fake* subnet can't
reach it through Engine. _make_enumerator() below instead builds Allowlist/
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

import time

import pytest

from aircommand.core.allowlist import Allowlist, NotATargetError
from aircommand.core.domain import MacAddress
from aircommand.core.engine import Engine
from aircommand.core.enumerate import Enumerator, get_interface_subnet
from aircommand.core.events import EventBus, NmapScanCompleted
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


def _slow_lines(count: int, delay_s: float):
    """Real (but tiny) per-line delay -- same helper, same reasoning, as
    test_capture_acceptance.py's own _slow_lines: keeps Discovery's driver
    thread demonstrably still holding the RF reservation by the time this
    test's very next line (the enumerate.start_scan() call) runs."""
    for i in range(count):
        time.sleep(delay_s)
        yield f"CH 6 ][ Elapsed: {i} s ][ 2024-01-01 10:00"


def _make_enumerator(script: dict, get_subnet) -> tuple[Enumerator, Allowlist, EventBus]:
    """Builds Allowlist/JobRegistry/RadioController by hand and constructs
    Enumerator directly, bypassing Engine -- see module docstring for why."""
    db = Database(":memory:")
    bus = EventBus()
    jobs = JobRegistry(db.jobs)
    proc = FakeProcRunner(script=script)
    rf = RadioController("wlan0", proc)
    allowlist = Allowlist(db.targets, bus)
    enumerator = Enumerator(allowlist, db.enum_results, bus, jobs, rf, proc, get_subnet=get_subnet)
    return enumerator, allowlist, bus


def test_successful_scan_publishes_nmap_scan_completed_with_both_hosts():
    get_subnet_calls = []

    def fake_get_subnet(adapter: str) -> str:
        get_subnet_calls.append(adapter)
        return "192.168.1.0/24"

    enumerator, allowlist, bus = _make_enumerator({"nmap": [NMAP_XML]}, fake_get_subnet)
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


def test_adapter_busy_propagates_synchronously_and_does_not_start_a_job(tmp_path):
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(script={"airodump-ng": _slow_lines(30, 0.01)}),
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


def test_get_interface_subnet_against_real_loopback_interface():
    """Real ioctl call, no fake, no mocking -- validates the struct-packing/
    ioctl logic against actual Linux behavior rather than only against
    research. "lo" always carries 127.0.0.1/8 on any Linux box (sandboxed or
    not) and needs no root to read its own address."""
    assert get_interface_subnet("lo") == "127.0.0.0/8"
