"""Pure parsers: tool output -> domain types. No I/O, no subprocess, no SQLite —
independently unit-testable against captured real tool output (fixtures). See
docs/design/core-gui-boundary.md 'Testability'.
"""

from __future__ import annotations

from aircommand.core.domain import EnumHost, Network


def parse_airodump_csv_line(line: str) -> Network | None:
    """One line of airodump-ng's --write-csv output -> a Network, or None if the
    line isn't a network row (airodump's CSV also emits a client-list section in
    the same file, separated by a blank line and a different header)."""
    raise NotImplementedError


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
