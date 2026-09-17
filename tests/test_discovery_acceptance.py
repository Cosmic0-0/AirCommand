"""The slice's actual deliverable: FakeProcRunner -> parse.py -> NetworkDiscovered
-> SQLite, wired through a real Engine with no GUI, no root, and no hardware. See
docs/design/core-gui-boundary.md 'Usage (caller's view)', headless call site.
"""

from __future__ import annotations

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
STATION_HEADER = "Station MAC, First time seen, Last time seen, Power, # packets, BSSID, Probed ESSIDs"

# Two simulated airodump-ng --write-csv refresh cycles: BSSID_1 is seen in both
# (with a changed Power/Last-time-seen, and a bogus later First-time-seen the
# second time round, to prove the DB's original first_seen wins); BSSID_2 only
# in the first. A blank line + station section sit between them, matching real
# airodump-ng CSV output, to prove those are skipped rather than mis-parsed.
AIRODUMP_SCRIPT = {
    "airodump-ng": [
        AP_HEADER,
        f"{BSSID_1}, 2024-01-01 10:00:00, 2024-01-01 10:00:05, 6, 54, WPA2, CCMP, PSK, -40, 10, 0, 0.0.0.0, 4, Net1, ",
        f"{BSSID_2}, 2024-01-01 10:00:00, 2024-01-01 10:00:06, 11, 54, OPN, , , -55, 8, 0, 0.0.0.0, 4, Net2, ",
        "",
        STATION_HEADER,
        f"11:22:33:44:55:66, 2024-01-01 10:00:01, 2024-01-01 10:00:02, -60, 5, {BSSID_1}, ",
        AP_HEADER,
        f"{BSSID_1}, 2024-01-01 10:00:08, 2024-01-01 10:00:09, 6, 54, WPA2, CCMP, PSK, -35, 14, 0, 0.0.0.0, 4, Net1, ",
    ]
}


def test_discovery_end_to_end_persists_and_publishes_networks(tmp_path):
    engine = Engine(
        db_path=":memory:",
        work_dir=tmp_path,
        adapter="wlan0",
        proc=FakeProcRunner(script=AIRODUMP_SCRIPT),
    )
    discovered = []
    sighting_updates = []
    engine.subscribe(discovered.append, NetworkDiscovered)
    engine.subscribe(sighting_updates.append, NetworkSightingUpdated)

    handle = engine.discovery.start()
    handle.wait_for_test(timeout=2.0)

    expected_bssids = {MacAddress.parse(BSSID_1), MacAddress.parse(BSSID_2)}

    assert {e.network.bssid for e in discovered} == expected_bssids
    assert len(discovered) == 2  # one NetworkDiscovered per BSSID -- not two for BSSID_1

    assert len(sighting_updates) == 1  # BSSID_1's second occurrence, and only that one
    assert sighting_updates[0].network.bssid == MacAddress.parse(BSSID_1)
    assert sighting_updates[0].network.last_signal_dbm == -35

    networks = engine.discovery.list_networks()
    assert {n.bssid for n in networks} == expected_bssids

    net1 = next(n for n in networks if n.bssid == MacAddress.parse(BSSID_1))
    assert net1.last_signal_dbm == -35
    assert net1.last_seen.isoformat() == "2024-01-01T10:00:09"
    # The second scan line claimed a first_seen of 10:00:08 -- proves the stored
    # value is the ORIGINAL first sighting, never overwritten by a later upsert.
    assert net1.first_seen.isoformat() == "2024-01-01T10:00:00"
