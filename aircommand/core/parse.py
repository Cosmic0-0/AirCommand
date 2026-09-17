"""Pure parsers: tool output -> domain types. No I/O, no subprocess, no SQLite —
independently unit-testable against captured real tool output (fixtures). See
docs/design/core-gui-boundary.md 'Testability'.
"""

from __future__ import annotations

from datetime import datetime

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


def parse_airodump_handshake_flag(csv_block: str) -> bool:
    """True if airodump-ng's own CSV output reports a WPA handshake captured for
    the target BSSID. See docs/design/core-gui-boundary.md Open questions re:
    whether this alone is trusted or cross-checked before minting a Handshake."""
    raise NotImplementedError


def parse_hashcat_status_line(line: str) -> "HashcatStatus | None":
    """One line of hashcat's --status-json output -> parsed progress, or None for
    a non-status line (banner/warnings)."""
    raise NotImplementedError


class HashcatStatus:
    """TODO: progress percent, hashrate string, ETA, and — on completion — the
    found key if any. Shape TBD once real --status-json output is on hand."""


def parse_nmap_xml(xml_bytes: bytes) -> tuple[EnumHost, ...]:
    """nmap -oX output -> the hosts/ports it found."""
    raise NotImplementedError
