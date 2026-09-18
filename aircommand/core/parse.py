"""Pure parsers: tool output -> domain types. No I/O, no subprocess, no SQLite —
independently unit-testable against captured real tool output (fixtures). See
docs/design/core-gui-boundary.md 'Testability'.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from aircommand.core.domain import EncryptionType, EnumHost, MacAddress, Network


def parse_airodump_csv_line(line: str) -> Network | None:
    """One line of airodump-ng's --write-csv output -> a Network, or None if the
    line isn't a network row (airodump's CSV also emits a client-list section in
    the same file, separated by a blank line and a different header)."""
    # AP section field order: 0 BSSID, 1 First time seen, 2 Last time seen,
    # 3 channel, 4 Speed, 5 Privacy, 6 Cipher, 7 Authentication, 8 Power,
    # 9 # beacons, 10 # IV, 11 LAN IP, 12 ID-length, 13 ESSID, [14 Key, often
    # blank/truncated -- hence >= 14 fields required, not == 15].
    fields = [f.strip() for f in line.split(",")]
    if len(fields) < 14:
        return None

    # MacAddress.parse on field 0 doubles as the row-type filter: it rejects the
    # AP section's own header row ("BSSID, ...") without hardcoding that string.
    # The length check above already excludes the blank separator line and the
    # (7-field) station section, so a station row's own valid-looking MAC here
    # never gets this far.
    try:
        bssid = MacAddress.parse(fields[0])
    except ValueError:
        return None

    privacy = fields[5]
    if "WPA3" in privacy:
        encryption = EncryptionType.WPA3
    elif "WPA2" in privacy:
        encryption = EncryptionType.WPA2
    elif "WPA" in privacy:
        encryption = EncryptionType.WPA
    elif "WEP" in privacy:
        encryption = EncryptionType.WEP
    else:
        encryption = EncryptionType.OPEN

    return Network(
        bssid=bssid,
        ssid=fields[13],
        channel=int(fields[3]),
        encryption=encryption,
        last_signal_dbm=int(fields[8]),
        first_seen=datetime.strptime(fields[1], "%Y-%m-%d %H:%M:%S"),
        last_seen=datetime.strptime(fields[2], "%Y-%m-%d %H:%M:%S"),
    )


def parse_aircrack_handshake_check(output: str) -> bool:
    """True if a one-shot `aircrack-ng -b <bssid> -w /dev/null <cap_path>` run's
    full stdout reports a captured handshake for that BSSID. Replaces the
    original (confirmed wrong -- no such flag exists in airodump-ng's CSV output)
    premise of checking a per-line handshake flag; see docs/roadmap.md Phase 1
    item 1. -b restricts aircrack-ng's output to one BSSID, so a plain substring
    check is enough -- no table parsing needed."""
    return "handshake)" in output


@dataclass(frozen=True)
class HashcatStatus:
    """One parsed --status-json tick. Field-shape research is in
    docs/roadmap.md Phase 1 item 2 (strong-confidence via a third-party typed
    binding + hashcat's own issue discussion, not yet checked against a real
    hashcat run — flagged there for a Phase 2 sanity check). Deliberately holds
    no found-key/outcome field: Crack determines outcome from the --outfile
    after the process exits, not from any --status-json tick — see crack.py."""

    percent: Optional[float]  # progress[0] / progress[1] * 100 when progress[1] > 0, else None
    hashrate: str  # e.g. "12.3 MH/s" — summed device speed, unit-scaled from raw H/s
    eta: Optional[timedelta]  # None if estimated_stop is absent, or already in the past


def parse_hashcat_status_line(line: str) -> Optional[HashcatStatus]:
    """One line of hashcat's --status-json output (one JSON object per line) ->
    parsed progress, or None for a non-status line (banner/warnings — hashcat
    doesn't guarantee every stdout line is a status object)."""
    import json

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    progress = data.get("progress")
    percent = (progress[0] / progress[1] * 100) if progress and progress[1] > 0 else None
    hashrate = _format_hashrate(sum(d["speed"] for d in data.get("devices", [])))
    estimated_stop = data.get("estimated_stop")
    eta = None
    if estimated_stop is not None:
        import time

        remaining = estimated_stop - time.time()
        if remaining > 0:
            eta = timedelta(seconds=remaining)
    return HashcatStatus(percent=percent, hashrate=hashrate, eta=eta)


def _format_hashrate(h_per_s: int) -> str:
    """Raw devices[].speed values are H/s ints (per docs/roadmap.md Phase 1 item
    2) — scale to a human string, e.g. 12345678 -> '12.3 MH/s'."""
    if h_per_s == 0:
        return "0 H/s"
    units = ["H/s", "kH/s", "MH/s", "GH/s", "TH/s"]
    value = float(h_per_s)
    unit_index = 0
    while value >= 1000 and unit_index < len(units) - 1:
        value /= 1000
        unit_index += 1
    return f"{value:.1f} {units[unit_index]}"


def parse_nmap_xml(xml_bytes: bytes) -> tuple[EnumHost, ...]:
    """nmap -oX output -> the hosts/ports it found. nmap's XML schema (host/
    address/hostnames/hostname/ports/port/state) is stable and well-documented;
    unlike airodump/hashcat's output this one didn't need research to pin."""
    raise NotImplementedError
    # TODO: import xml.etree.ElementTree as ET
    #   root = ET.fromstring(xml_bytes)
    #   hosts = []
    #   for host_el in root.findall("host"):
    #     address_el = host_el.find("address")
    #     if address_el is None: continue
    #     hostname_el = host_el.find("hostnames/hostname")
    #     open_ports = tuple(
    #       int(port_el.get("portid"))
    #       for port_el in host_el.findall("ports/port")
    #       if (state_el := port_el.find("state")) is not None and state_el.get("state") == "open"
    #     )
    #     hosts.append(EnumHost(
    #       ip=address_el.get("addr"),
    #       hostname=hostname_el.get("name") if hostname_el is not None else None,
    #       open_ports=open_ports,
    #     ))
    #   return tuple(hosts)
