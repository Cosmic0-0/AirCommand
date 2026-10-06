"""FakeProcRunner -> on-disk CSV poll -> parse.py -> NetworkDiscovered -> SQLite,
wired through a real Engine with no GUI, no root, and no hardware. See
docs/design/core-gui-boundary.md 'Usage (caller's view)', headless call site.

airodump-ng writes network data to <prefix>-01.csv on disk, not to stdout --
stdout carries its live interactive display instead (see docs/roadmap.md Phase 1
item 0). _drive's loop doesn't touch handle.lines() content at all anymore
(see discovery.py's own comment on a later, deeper real-hardware finding: even
*gating ticks on stdout arriving* was unreliable) -- it's a plain wall-clock
loop now, driven by ProcHandle.poll() for liveness. These tests feed
"airodump-ng" an empty scripted line list (content is simply never read) and
control how many poll ticks happen via FakeProcRunner's running_polls= instead.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from aircommand.core.domain import MacAddress
from aircommand.core.engine import Engine
from aircommand.core.events import NetworkDiscovered, NetworkSightingUpdated
from aircommand.core.procutil import FakeProcRunner

BSSID_1 = "AA:BB:CC:DD:EE:01"
BSSID_2 = "AA:BB:CC:DD:EE:02"

AP_HEADER = (
    "BSSID, First time seen, Last time seen, channel, Speed, Privacy, Cipher, "
    "Authentication, Power, # beacons, # IV, LAN IP, ID-length, ESSID, Key"
)

# What airodump-ng actually writes to <prefix>-01.csv -- the on-disk artifact
# _poll_csv reads, rewritten in place each refresh cycle. Two networks, same row
# shape real airodump-ng --write output uses.
CSV_CONTENT = (
    "\n".join(
        [
            AP_HEADER,
            f"{BSSID_1}, 2024-01-01 10:00:00, 2024-01-01 10:00:05, 6, 54, WPA2, CCMP, PSK, -40, 10, 0, 0.0.0.0, 4, Net1, ",
            f"{BSSID_2}, 2024-01-01 10:00:00, 2024-01-01 10:00:06, 11, 54, OPN, , , -55, 8, 0, 0.0.0.0, 4, Net2, ",
        ]
    )
    + "\n"
)

# _drive's loop no longer iterates handle.lines() at all (see discovery.py's
# own comment -- a real-hardware finding that gating CSV polling on the
# spawned tool's own stdout chatter is unreliable). It's a plain wall-clock
# loop instead, driven by ProcHandle.poll() for liveness: FakeProcHandle.poll()
# reports "still running" for exactly this many calls before reporting
# "exited", so this number IS the poll-tick count now (with the zero poll
# interval below, each tick fires the Pacer) -- same role DISCOVERY_NOISE's
# line count used to play, enough to prove "discovered once, then updated on
# every later tick" rather than just once.
DISCOVERY_TICK_COUNT = 4

# RadioController.reserve() now really spawns "airmon-ng" on its first
# monitor-mode use (docs/roadmap.md Phase 2 item 1) -- every script below needs
# an entry for it or FakeProcRunner KeyErrors. No rename-announcement line, so
# parse_airmon_monitor_interface falls back to the original "wlan0" name.
AIRMON_NO_RENAME_OUTPUT = ["monitor mode already enabled on wlan0"]


def _write_csv_on_spawn(argv: list[str]) -> None:
    """Simulates airodump-ng's --write side effect: writes the real on-disk
    artifact _poll_csv reads, at the path airodump-ng itself would use (the
    --write prefix argument, plus airodump-ng's own "-01.csv" suffix
    convention -- see Discovery._drive). Guarded on "--write" actually being
    present since FakeProcRunner now fires on_spawn for every spawn, including
    RadioController's own "airmon-ng start <adapter>" call (docs/roadmap.md
    Phase 2 item 1), which carries no such flag."""
    if "--write" not in argv:
        return
    prefix = argv[argv.index("--write") + 1]
    Path(f"{prefix}-01.csv").write_text(CSV_CONTENT)


def test_discovery_polls_csv_file_not_stdout_for_networks(tmp_path):
    """The regression test. Networks can only appear here because the on-disk
    CSV file was read -- _drive's loop never touches handle.lines() content at
    all now, so this would discover nothing if _poll_csv itself were broken."""
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
            on_spawn=_write_csv_on_spawn,
            running_polls={"airodump-ng": DISCOVERY_TICK_COUNT},
        ),
        # Zero interval: Pacer.due() fires on every tick. Zero tick interval:
        # fast and deterministic, no real sleeping needed to exercise several
        # iterations -- DISCOVERY_TICK_COUNT alone controls how many happen.
        discovery_poll_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )
    discovered = []
    sighting_updates = []
    engine.subscribe(discovered.append, NetworkDiscovered)
    engine.subscribe(sighting_updates.append, NetworkSightingUpdated)

    handle = engine.discovery.start()
    handle.wait_for_test(timeout=2.0)

    expected_bssids = {MacAddress.parse(BSSID_1), MacAddress.parse(BSSID_2)}

    # Both networks discovered, exactly once each -- not once per poll tick.
    assert {e.network.bssid for e in discovered} == expected_bssids
    assert len(discovered) == 2

    # The CSV file is unchanged on every tick after the first read, so every
    # later tick updates the existing sighting instead of re-discovering it.
    assert len(sighting_updates) == 2 * (DISCOVERY_TICK_COUNT - 1)
    assert {u.network.bssid for u in sighting_updates} == expected_bssids

    networks = engine.discovery.list_networks()
    assert {n.bssid for n in networks} == expected_bssids


def test_discovery_survives_csv_file_never_appearing(tmp_path):
    """Robustness: if airodump-ng's CSV file never shows up (no on_spawn writes
    it here), _drive must not crash or hang -- each tick just has nothing to
    report, via _poll_csv's FileNotFoundError->return path."""
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        # no on_spawn -- csv never written
        proc=FakeProcRunner(
            script={"airmon-ng": AIRMON_NO_RENAME_OUTPUT, "airodump-ng": []},
            running_polls={"airodump-ng": DISCOVERY_TICK_COUNT},
        ),
        discovery_poll_interval=timedelta(seconds=0),
        drive_tick_interval=timedelta(seconds=0),
    )

    handle = engine.discovery.start()
    handle.wait_for_test(timeout=2.0)  # must return well inside the timeout, not hang

    assert engine.discovery.list_networks() == []
