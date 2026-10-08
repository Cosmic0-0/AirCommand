"""Pure parsers: tool output -> domain types. No I/O, no subprocess, no SQLite —
independently unit-testable against captured real tool output (fixtures). See
docs/design/core-gui-boundary.md 'Testability'.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from aircommand.core.domain import Band, EncryptionType, EnumHost, MacAddress, Network


def parse_airodump_csv_line(line: str) -> Network | None:
    """One line of airodump-ng's --write (csv output format) -> a Network, or
    None if the line isn't a network row (airodump's CSV also emits a
    client-list section in the same file, separated by a blank line and a
    different header)."""
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
    """True if a one-shot `aircrack-ng -w /dev/null <cap_path>` run's full
    stdout reports at least one captured handshake, via the per-network
    summary table's "<Encryption> (N handshake)" column. See docs/adr/0012
    for how this invocation shape was pinned down against REAL captured
    handshakes (not assumed, and not the synthetic .cap crafting ADR-0011
    already found didn't work): two confirmed, now-fixed problems,

    1. `-b <bssid>` -- previously part of this invocation, now dropped --
       suppresses the ENTIRE summary table (not just the interactive
       network-selection prompt it was added for) whenever it matches
       exactly one BSSID, which is every real call capture.py makes. The
       table, and this text, never appeared at all with -b given, regardless
       of whether a real handshake was present. Dropping -b is safe here
       specifically because airodump-ng's own --bssid filter (capture.py's
       airodump-ng invocation) already guarantees the .cap file this reads
       contains exactly one network -- confirmed against 6 real capture
       files from actual tool runs, every one auto-selected ("Choosing
       first network as target.") with no interactive prompt.
    2. This function's own check used to be a plain `"handshake)" in output`
       substring test -- which matches "(0 handshake)" just as much as
       "(1 handshake)", a real false-positive bug independent of (1) above,
       caught only once real output (which legitimately prints "(0
       handshake)" for a network with none yet) made the table reachable at
       all. Now requires the parsed count to be > 0.

    -w /dev/null itself still errors ("Processing dictionary file /dev/null")
    -- confirmed harmless for this purpose: the error happens before the
    table is printed and doesn't suppress it, and doesn't risk attempting a
    real crack (the dictionary never validates), so there was no need to
    supply or clean up a real dummy wordlist file."""
    import re

    match = re.search(r"\((\d+)\s+handshakes?\)", output)
    return match is not None and int(match.group(1)) > 0


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


def parse_airmon_monitor_interface(output: str, fallback: str) -> str:
    """`airmon-ng start <adapter>`'s own stdout -> the resulting monitor-mode
    interface name. HARDWARE-CONFIRMED (docs/roadmap.md Phase 2 item 5) against
    a real Ralink RT2870/RT3070 (rt2800usb driver) adapter with a udev-persistent
    name (`wlx<mac>`, 15 characters — right at Linux's IFNAMSIZ-1 limit):

        Interface wlx24050f7d7ae0mon is too long for linux so it will be
        renamed to the old style (wlan#) name.

            (mac80211 monitor mode vif enabled on [phy1]wlan0mon)

    This driver does rename (matching the "classically... reportedly true for
    rt2800usb" research), but NOT to `<original>mon` here — `<original>mon`
    would exceed IFNAMSIZ, so airmon-ng falls back to old-style short naming
    (`wlan0`, `wlan1`, ...) instead, and — not previously anticipated —
    its announcement line in this fallback case has NO "for <original>" clause
    at all, just "vif enabled on [phyN]<new>". The originally-researched
    "vif enabled for [phy0]wlan0 on [phy0]wlan0mon" shape (both a "for" and an
    "on" clause) was never hardware-confirmed and may not be real airmon-ng
    output at all — kept supported below since it's a harmless superset, not
    because it's confirmed. Returns `fallback` (the original adapter name) if
    no rename-announcement line is present at all, covering the in-place
    (no rename) case — and, defensively, any output shape this regex doesn't
    recognize, rather than raising."""
    import re

    # (?:\[\w+\])? matches an entire optional "[phy0]"-style prefix as one unit,
    # deliberately not two independently-optional bracket characters around a
    # greedy \w* -- that first shape was tried and empirically failed the
    # no-bracket case (a bare "on wlan0mon" with no "[phyN]" prefix): the greedy
    # \w* had nothing to stop it from swallowing the whole interface name,
    # leaving (\w+) to backtrack down to capturing just its last character ("n").
    # Caught by actually running this against sample output before shipping it,
    # not just hand-tracing the pattern.
    #
    # (?:for \S+ )? is optional for the same reason: real hardware output (see
    # docstring) omits the "for <original>" clause entirely in the old-style-
    # rename case. A bare, always-required "for \S+ on" (the original shape)
    # silently fell all the way through to `fallback` — the WRONG interface
    # name — against this real output, since the whole regex simply failed to
    # match. Caught only by testing against real captured output, not by
    # hand-tracing the pattern; see tests/test_parse.py's own regression test
    # for the exact string this broke on.
    match = re.search(r"monitor mode vif enabled (?:for \S+ )?on (?:\[\w+\])?(\w+)", output)
    return match.group(1) if match else fallback


# One `iw phy <phy> info` frequency entry: "\t\t\t* 2412.0 MHz [1] (20.0 dBm)".
# See parse_iw_phy_bands's docstring for why the bracket is kept.
_IW_FREQUENCY_LINE = re.compile(r"^\s*\*\s+(\d+(?:\.\d+)?)\s+MHz\s+\[\d+\](.*)$")


def parse_iw_phy_bands(output: str) -> frozenset[Band]:
    """`iw phy <phy> info`'s stdout -> the Bands that phy can use. Parses the
    single-phy form, not `iw list`: `iw list` prints every phy on the machine
    (on the dev machine phy3 is the USB adapter and phy0 is the laptop's
    internal card, which also has a 5GHz band), so parsing it would report
    bands the adapter does not have.

    HARDWARE-CONFIRMED against a real dual-band adapter's output on
    2026-10-08: frequencies print as floats (`2412.0 MHz [1]`, not `2412`);
    the flags seen after the channel are `(disabled)` (2.4GHz channel 14) and
    `(radar detection)` (DFS channels); and two lines elsewhere in the output
    mention MHz without being frequency entries (`short GI (80 MHz)` and
    `* short GI for 40 MHz`). Those two are rejected because the pattern
    wants `* <number> MHz` (checked: they fail with or without the bracket
    requirement). The bracketed channel number is kept as a second guard
    against any other `* <number> MHz` line that is not a frequency entry.

    Each line is classified independently by its frequency alone; the
    "Band 1:" / "Band 2:" headers are not used. A band counts as supported if
    at least one of its frequencies is not `(disabled)`. `(radar detection)`
    and `(no IR)` channels still count -- they can be listened on. 2400-2499
    MHz is 2.4GHz and 5150-5924 MHz is 5GHz; anything else (4.9GHz, 6GHz,
    garbage) is ignored, so 6GHz never produces a Band (ADR-0006). Empty or
    unrecognizable input returns an empty set rather than raising."""
    bands: set[Band] = set()
    for line in output.splitlines():
        match = _IW_FREQUENCY_LINE.match(line)
        if match is None or "(disabled)" in match.group(2):
            continue
        mhz = float(match.group(1))
        if 2400 <= mhz < 2500:
            bands.add(Band.GHZ_2_4)
        elif 5150 <= mhz < 5925:
            bands.add(Band.GHZ_5)
    return frozenset(bands)


def parse_nmap_xml(xml_bytes: bytes) -> tuple[EnumHost, ...]:
    """nmap -oX output -> the hosts/ports it found. nmap's XML schema (host/
    address/hostnames/hostname/ports/port/state) is stable and well-documented;
    unlike airodump/hashcat's output this one didn't need research to pin."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_bytes)
    hosts = []
    for host_el in root.findall("host"):
        address_el = host_el.find("address")
        if address_el is None:
            continue
        hostname_el = host_el.find("hostnames/hostname")
        open_ports = tuple(
            int(port_el.get("portid"))
            for port_el in host_el.findall("ports/port")
            if (state_el := port_el.find("state")) is not None and state_el.get("state") == "open"
        )
        hosts.append(EnumHost(
            ip=address_el.get("addr"),
            hostname=hostname_el.get("name") if hostname_el is not None else None,
            open_ports=open_ports,
        ))
    return tuple(hosts)
