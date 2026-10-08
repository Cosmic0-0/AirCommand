"""Regression coverage for ADR-0015's own flagged follow-up: Discovery,
Capture, and Enumerate each built their record_process() fingerprint the same
wrong way crack.py did before that ADR's fix -- a f"<tool> <arg>" string that
is NEVER a real substring of the spawned process's actual (space-joined)
/proc/pid/cmdline, because real argv puts other flags between the tool name
and that arg (or, for Enumerate, never puts that arg in argv at all). See
discovery.py/capture.py/enumerate.py's own record_process() call-site comments
for the fix and the exact mismatch shape each one had.

One file for all three, not three additions split across
test_discovery_acceptance.py/test_capture_acceptance.py/
test_enumerate_acceptance.py: this is the identical bug, found the identical
way, in three unrelated drivers (plus crack.py, fixed separately) -- the thing
worth proving here is the fingerprint-vs-cmdline substring check itself, which
needs no FakeProcRunner, no Engine, no domain objects, and no driver instance
at all, just a real process whose argv has the same token shape the driver
actually builds. That's a narrower unit than anything those three acceptance
files otherwise test, so it doesn't fit naturally alongside them -- same
reasoning test_procutil.py already applies to is_process_group_alive's own
tests, which this file is a direct extension of.

Same technique as test_procutil.py's test_is_process_group_alive_true_for_a_
real_process_with_matching_fingerprint and test_reconciliation.py's real-
orphan tests: a real `python3 -c "..."` process stands in for the actual
tool binary (airodump-ng/nmap aren't needed, and FakeProcRunner can't be used
here at all -- it never touches a real cmdline, which is exactly the part of
this bug class it's unable to catch). Once python3 has consumed its own `-c
<script>` pair, every argv token after that is passed straight through to the
real process's argv (and so into /proc/pid/cmdline) without being
reinterpreted as another interpreter flag -- including a second, unrelated
"-c" token, which is exactly what capture.py's own real argv shape needs
below. Each test spawns its own process and kills + waits on it in a
finally, even on assertion failure, matching that same established idiom.
"""

from __future__ import annotations

import subprocess

from aircommand.core.procutil import is_process_group_alive

_STANDIN_PREFIX = ["python3", "-c", "import time; time.sleep(30)"]


def test_discovery_fingerprint_is_a_real_cmdline_substring():
    # Real shape (discovery.py _drive): ["airodump-ng", "--band", <band>,
    # "--write", <csv_prefix>, <adapter>]. csv_prefix is job_id-derived, same
    # as production (self._work_dir / f"aircommand-discovery-{job_id}").
    csv_prefix = "/tmp/aircommand-discovery-7f2c1e90"
    adapter = "wlan0mon"
    old_buggy_fingerprint = f"airodump-ng {adapter}"  # the pre-fix string -- never a real substring

    popen = subprocess.Popen(
        [*_STANDIN_PREFIX, "--band", "bg", "--write", csv_prefix, adapter],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        assert is_process_group_alive(popen.pid, csv_prefix) is True
        # Documents the bug this replaces: the flags between the tool name
        # and the adapter (here "--band bg --write <csv_prefix>") mean the
        # old f"airodump-ng {adapter}" shape never matches.
        assert is_process_group_alive(popen.pid, old_buggy_fingerprint) is False
    finally:
        popen.kill()
        popen.wait()


def test_capture_fingerprint_is_a_real_cmdline_substring():
    # Real shape (capture.py _drive): ["airodump-ng", "-c", <channel>,
    # "--bssid", <bssid>, "-w", <cap_prefix>, <adapter>]. cap_prefix is
    # job_id-derived, same as production
    # (self._work_dir / f"{target.bssid}-{job_id}"). The stand-in's OWN "-c"
    # (python3's script flag) is consumed first; this second, unrelated "-c"
    # (airodump-ng's channel flag) passes straight through, exactly
    # reproducing capture.py's real argv shape.
    bssid = "AA:BB:CC:DD:EE:01"
    cap_prefix = f"/tmp/{bssid}-9a3d5b10"
    adapter = "wlan0mon"
    old_buggy_fingerprint = f"airodump-ng {bssid} {adapter}"  # the pre-fix string -- never a real substring

    popen = subprocess.Popen(
        [*_STANDIN_PREFIX, "-c", "6", "--bssid", bssid, "-w", cap_prefix, adapter],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        assert is_process_group_alive(popen.pid, cap_prefix) is True
        # Documents the bug this replaces: "-w <cap_prefix>" sits between
        # bssid and adapter, and neither sits right after "airodump-ng" --
        # the old f"airodump-ng {bssid} {adapter}" shape never matches.
        assert is_process_group_alive(popen.pid, old_buggy_fingerprint) is False
    finally:
        popen.kill()
        popen.wait()


def test_enumerate_fingerprint_is_a_real_cmdline_substring():
    # Real shape (enumerate.py _drive), no --ports/-sV (the simplest of the
    # real shapes _drive can build, per EnumOptions' own defaults): ["nmap",
    # "-oX", "-", <subnet>].
    subnet = "192.168.1.0/24"
    bssid = "AA:BB:CC:DD:EE:01"
    old_buggy_fingerprint = f"nmap {bssid}"  # the pre-fix string -- never a real substring, by construction

    popen = subprocess.Popen(
        [*_STANDIN_PREFIX, "-oX", "-", subnet],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        assert is_process_group_alive(popen.pid, subnet) is True
        # Documents the bug this replaces: target.bssid never appeared
        # anywhere in nmap's own argv at all (only subnet does) -- the old
        # f"nmap {bssid}" shape could never match, not even by bad luck.
        assert is_process_group_alive(popen.pid, old_buggy_fingerprint) is False
    finally:
        popen.kill()
        popen.wait()
