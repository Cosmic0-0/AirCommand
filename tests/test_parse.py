import pytest

from aircommand.core.domain import Band, EncryptionType, MacAddress
from aircommand.core.parse import (
    parse_aircrack_handshake_check,
    parse_airmon_monitor_interface,
    parse_airodump_csv_line,
    parse_iw_phy_bands,
)

AP_HEADER = (
    "BSSID, First time seen, Last time seen, channel, Speed, Privacy, Cipher, "
    "Authentication, Power, # beacons, # IV, LAN IP, ID-length, ESSID, Key"
)
STATION_HEADER = "Station MAC, First time seen, Last time seen, Power, # packets, BSSID, Probed ESSIDs"


def ap_line(privacy: str = "WPA2") -> str:
    return (
        f"AA:BB:CC:DD:EE:01, 2024-01-01 10:00:00, 2024-01-01 10:00:05, 6, 54, "
        f"{privacy}, CCMP, PSK, -40, 10, 0, 0.0.0.0, 4, MyNetwork, "
    )


def test_valid_ap_row_parses_every_field():
    network = parse_airodump_csv_line(ap_line())

    assert network is not None
    assert network.bssid == MacAddress.parse("AA:BB:CC:DD:EE:01")
    assert network.ssid == "MyNetwork"
    assert network.channel == 6
    assert network.encryption == EncryptionType.WPA2
    assert network.last_signal_dbm == -40
    assert network.first_seen.isoformat() == "2024-01-01T10:00:00"
    assert network.last_seen.isoformat() == "2024-01-01T10:00:05"


def test_ap_section_header_row_returns_none():
    assert parse_airodump_csv_line(AP_HEADER) is None


def test_blank_line_returns_none():
    assert parse_airodump_csv_line("") is None


def test_station_section_header_returns_none():
    assert parse_airodump_csv_line(STATION_HEADER) is None


def test_station_data_row_returns_none():
    station_row = "11:22:33:44:55:66, 2024-01-01 10:00:01, 2024-01-01 10:00:02, -60, 5, AA:BB:CC:DD:EE:01, "

    assert parse_airodump_csv_line(station_row) is None


@pytest.mark.parametrize(
    "privacy, expected",
    [
        ("OPN", EncryptionType.OPEN),
        ("WEP", EncryptionType.WEP),
        ("WPA", EncryptionType.WPA),
        ("WPA2", EncryptionType.WPA2),
        ("WPA3", EncryptionType.WPA3),
        ("WPA WPA2", EncryptionType.WPA2),
        ("WPA2 WPA3", EncryptionType.WPA3),
    ],
    ids=["open", "wep", "wpa", "wpa2", "wpa3", "compound_wpa_wpa2_most_specific_wins", "compound_wpa2_wpa3_most_specific_wins"],
)
def test_privacy_field_variants_map_to_the_right_encryption_type(privacy, expected):
    network = parse_airodump_csv_line(ap_line(privacy=privacy))

    assert network.encryption == expected


# --- parse_airmon_monitor_interface ------------------------------------------

# Real captured `sudo airmon-ng start wlx24050f7d7ae0` output (docs/roadmap.md
# Phase 2 item 5): a Ralink RT2870/RT3070 (rt2800usb) adapter with a 15-
# character udev-persistent name. `<original>mon` (18 chars) would exceed
# Linux's IFNAMSIZ-1 limit, so airmon-ng falls back to an old-style short name
# (wlan0mon) instead -- and, not previously anticipated, its announcement line
# in this case has no "for <original>" clause at all, unlike the originally
# (never hardware-confirmed) researched shape below. This is the exact string
# that silently fell through to `fallback` -- the WRONG interface -- before
# the regex fix; kept verbatim as a regression test, not trimmed to a minimal
# repro, so a future change can be checked against the real thing again.
REAL_RT2870_OLD_STYLE_RENAME_OUTPUT = """Found 4 processes that could cause trouble.
Kill them using 'airmon-ng check kill' before putting
the card in monitor mode, they will interfere by changing channels
and sometimes putting the interface back in managed mode

    PID Name
    939 avahi-daemon
    995 avahi-daemon
   1015 NetworkManager
   1017 wpa_supplicant

PHY    Interface    Driver        Chipset

phy0    wlo1        rtw89_8852be    Realtek Semiconductor Co., Ltd. RTL8852BE PCIe 802.11ax Wireless Network Controller
phy1    wlx24050f7d7ae0    rt2800usb    Ralink Technology, Corp. RT2870/RT3070
Interface wlx24050f7d7ae0mon is too long for linux so it will be renamed to the old style (wlan#) name.

\t(mac80211 monitor mode vif enabled on [phy1]wlan0mon)
\t(mac80211 station mode vif disabled for [phy1]wlx24050f7d7ae0)
"""


def test_real_rt2870_old_style_rename_output_parses_to_wlan0mon():
    result = parse_airmon_monitor_interface(REAL_RT2870_OLD_STYLE_RENAME_OUTPUT, fallback="wlx24050f7d7ae0")

    assert result == "wlan0mon"


def test_originally_researched_for_and_on_shape_still_parses_correctly():
    # Never hardware-confirmed (see parse_airmon_monitor_interface's own
    # docstring), but kept supported as a harmless superset of the real shape
    # above -- this proves the regex fix didn't break it.
    output = "(mac80211 monitor mode vif enabled for [phy0]wlan0 on [phy0]wlan0mon)"

    assert parse_airmon_monitor_interface(output, fallback="wlan0") == "wlan0mon"


def test_no_rename_announcement_line_returns_fallback():
    output = "some other unrelated airmon-ng output, no rename happened"

    assert parse_airmon_monitor_interface(output, fallback="wlan0") == "wlan0"


# --- parse_aircrack_handshake_check ---------------------------------------------
#
# Fixtures below are the REAL shape confirmed against the installed aircrack-ng
# 1.7 binary and a genuinely captured handshake (docs/adr/0012) -- BSSID/ESSID
# genericized to this codebase's existing AA:BB:CC:DD:EE:01 test convention
# rather than the real home network they were captured against, but every
# other line (the /dev/null errors, the exact table formatting, the trailing
# "Please specify a dictionary" fallthrough) is verbatim real stdout, not a
# guess -- including the real quirk that "0 handshake)" is NOT pluralized any
# differently from "1 handshake)", which is exactly the false-positive this
# function's old plain substring check missed.

_REAL_AIRCRACK_NG_PREAMBLE = (
    "ERROR: Processing dictionary file /dev/null (No such file or directory)\n"
    "ERROR: Processing dictionary file /dev/null (No such file or directory)\n"
    "Reading packets, please wait...\n"
    "Opening capture-01.cap\n"
    "Read 59079 packets.\n\n"
)
_REAL_AIRCRACK_NG_TRAILER = (
    "\nChoosing first network as target.\n\n"
    "Reading packets, please wait...\n"
    "Opening capture-01.cap\n"
    "Read 59079 packets.\n\n"
    "1 potential targets\n\n"
    "Please specify a dictionary (option -w).\n"
)


def _real_table_output(handshake_count: int) -> str:
    row = f"   1  AA:BB:CC:DD:EE:01  Test-SSID                 WPA ({handshake_count} handshake)"
    return _REAL_AIRCRACK_NG_PREAMBLE + "   #  BSSID              ESSID                     Encryption\n\n" \
        + row + _REAL_AIRCRACK_NG_TRAILER


def test_real_output_with_a_genuine_handshake_returns_true():
    assert parse_aircrack_handshake_check(_real_table_output(1)) is True


def test_real_output_with_zero_handshakes_returns_false():
    # The exact false-positive the old `"handshake)" in output` substring
    # check missed: "0 handshake)" contains "handshake)" too.
    assert parse_aircrack_handshake_check(_real_table_output(0)) is False


def test_real_no_networks_found_output_returns_false():
    output = (
        "Reading packets, please wait...\n"
        "Opening header-only.cap\n"
        "Read 0 packets.\n\n"
        "No networks found, exiting.\n\n\n"
        "Quitting aircrack-ng...\n"
    )

    assert parse_aircrack_handshake_check(output) is False


def test_old_minus_b_suppressed_shape_with_no_table_at_all_returns_false():
    # Confirmed real shape when -b matches exactly one BSSID (the invocation
    # this function used to be paired with, before docs/adr/0012 dropped -b):
    # no summary table at all, so there is nothing for this function to find
    # regardless of whether a real handshake was actually in the file. Kept
    # as a regression test for WHY -b had to go, not because this shape is
    # still produced by the current capture.py invocation.
    output = (
        "ERROR: Processing dictionary file /dev/null (No such file or directory)\n"
        "ERROR: Processing dictionary file /dev/null (No such file or directory)\n"
        "Reading packets, please wait...\n"
        "Opening capture-01.cap\n"
        "Read 59079 packets.\n\n"
        "1 potential targets\n\n"
        "Please specify a dictionary (option -w).\n"
    )

    assert parse_aircrack_handshake_check(output) is False


# --- parse_iw_phy_bands ---------------------------------------------------------
#
# Fixtures are trimmed excerpts of REAL `iw phy phy3 info` output from a dual-band
# USB adapter (2026-10-08): every line below is verbatim, with its real tab
# indentation (Band header 1 tab, "Frequencies:" 2 tabs, entries 3 tabs), and the
# two MHz decoys sit where they really do -- "short GI (80 MHz)" inside Band 2's
# VHT capabilities, "* short GI for 40 MHz" in the HT Capability overrides after
# both bands. Tests that need other flag combinations build them from these pieces
# plus single synthetic frequency lines in the same real format.

_IW_PHY_HEADER = "Wiphy phy3\n\twiphy index: 3\n"

_IW_BAND_1_2_4GHZ = (
    "\tBand 1:\n"
    "\t\tCapabilities: 0x196f\n"
    "\t\tFrequencies:\n"
    "\t\t\t* 2412.0 MHz [1] (20.0 dBm)\n"
    "\t\t\t* 2462.0 MHz [11] (20.0 dBm)\n"
    "\t\t\t* 2472.0 MHz [13] (20.0 dBm)\n"
    "\t\t\t* 2484.0 MHz [14] (disabled)\n"
)

# Everything in Band 2 up to (not including) its frequency entries, including the
# first decoy, "short GI (80 MHz)".
_IW_BAND_2_PREAMBLE = (
    "\tBand 2:\n"
    "\t\tCapabilities: 0x196f\n"
    "\t\tVHT Capabilities (0x03d071b2):\n"
    "\t\t\tSupported Channel Width: neither 160 nor 80+80\n"
    "\t\t\tRX LDPC\n"
    "\t\t\tshort GI (80 MHz)\n"
    "\t\t\tTX STBC\n"
    "\t\tFrequencies:\n"
)

_IW_BAND_2_5GHZ_ENTRIES = (
    "\t\t\t* 5180.0 MHz [36] (24.0 dBm)\n"
    "\t\t\t* 5260.0 MHz [52] (24.0 dBm) (radar detection)\n"
    "\t\t\t* 5700.0 MHz [140] (24.0 dBm) (radar detection)\n"
    "\t\t\t* 5825.0 MHz [165] (30.0 dBm)\n"
)

# Everything after the last band, including the second decoy, "* short GI for 40 MHz".
_IW_TRAILER = (
    "\tSupported commands:\n"
    "\t\t * new_interface\n"
    "\tHT Capability overrides:\n"
    "\t\t * MCS: ff ff ff ff ff ff ff ff ff ff\n"
    "\t\t * supported channel width\n"
    "\t\t * short GI for 40 MHz\n"
    "\t\t * max A-MPDU length exponent\n"
)

_IW_BAND_2_5GHZ = _IW_BAND_2_PREAMBLE + _IW_BAND_2_5GHZ_ENTRIES

_REAL_DUAL_BAND_IW_OUTPUT = _IW_PHY_HEADER + _IW_BAND_1_2_4GHZ + _IW_BAND_2_5GHZ + _IW_TRAILER


def test_real_dual_band_output_reports_both_bands():
    """The real dual-band excerpt, decoys and all, yields exactly 2.4GHz and 5GHz."""
    assert parse_iw_phy_bands(_REAL_DUAL_BAND_IW_OUTPUT) == frozenset({Band.GHZ_2_4, Band.GHZ_5})


def test_2_4ghz_only_phy_reports_only_2_4ghz():
    """With Band 2 removed, the trailing "short GI for 40 MHz" decoy does not invent a band."""
    output = _IW_PHY_HEADER + _IW_BAND_1_2_4GHZ + _IW_TRAILER

    assert parse_iw_phy_bands(output) == frozenset({Band.GHZ_2_4})


def test_5ghz_only_phy_reports_only_5ghz():
    """A phy with only the 5GHz band section reports only 5GHz."""
    output = _IW_PHY_HEADER + _IW_BAND_2_5GHZ + _IW_TRAILER

    assert parse_iw_phy_bands(output) == frozenset({Band.GHZ_5})


def test_band_whose_only_entries_are_disabled_is_not_offered():
    """A Band 2 section present but with every 5GHz entry (disabled) does not add 5GHz."""
    all_disabled = (
        _IW_BAND_2_PREAMBLE
        + "\t\t\t* 5180.0 MHz [36] (disabled)\n"
        + "\t\t\t* 5200.0 MHz [40] (disabled)\n"
        + "\t\t\t* 5825.0 MHz [165] (disabled)\n"
    )
    output = _IW_PHY_HEADER + _IW_BAND_1_2_4GHZ + all_disabled + _IW_TRAILER

    assert parse_iw_phy_bands(output) == frozenset({Band.GHZ_2_4})


def test_band_with_one_enabled_entry_among_disabled_ones_is_offered():
    """One enabled 5GHz entry is enough, however many of its siblings are (disabled)."""
    mostly_disabled = (
        _IW_BAND_2_PREAMBLE
        + "\t\t\t* 5180.0 MHz [36] (disabled)\n"
        + "\t\t\t* 5200.0 MHz [40] (disabled)\n"
        + "\t\t\t* 5825.0 MHz [165] (30.0 dBm)\n"
    )
    output = _IW_PHY_HEADER + _IW_BAND_1_2_4GHZ + mostly_disabled + _IW_TRAILER

    assert parse_iw_phy_bands(output) == frozenset({Band.GHZ_2_4, Band.GHZ_5})


def test_5ghz_band_of_only_radar_detection_channels_is_offered():
    """DFS (radar detection) channels can be listened on, so they count as 5GHz."""
    dfs_only = (
        _IW_BAND_2_PREAMBLE
        + "\t\t\t* 5260.0 MHz [52] (24.0 dBm) (radar detection)\n"
        + "\t\t\t* 5500.0 MHz [100] (24.0 dBm) (radar detection)\n"
    )

    assert parse_iw_phy_bands(_IW_PHY_HEADER + dfs_only + _IW_TRAILER) == frozenset({Band.GHZ_5})


def test_6ghz_only_entries_never_produce_a_band():
    """6GHz is out of scope (ADR-0006): 5955 and 6115 MHz entries yield no Band at all."""
    output = (
        "\tBand 4:\n"
        "\t\tFrequencies:\n"
        "\t\t\t* 5955.0 MHz [1] (20.0 dBm)\n"
        "\t\t\t* 6115.0 MHz [33] (20.0 dBm)\n"
    )

    assert parse_iw_phy_bands(output) == frozenset()


def test_mhz_decoy_lines_without_a_real_frequency_entry_yield_nothing():
    """"short GI (80 MHz)" and "* short GI for 40 MHz" alone are not frequency entries."""
    output = "\t\t\tshort GI (80 MHz)\n\t\t * short GI for 40 MHz\n"

    assert parse_iw_phy_bands(output) == frozenset()


@pytest.mark.parametrize("output", ["", "\n", "   \n\t\n", "not iw output at all", "* MHz [] 2412.0\n\x00\xff garbage"])
def test_empty_or_unrecognizable_input_returns_an_empty_set(output):
    """Empty and garbage input return frozenset() instead of raising."""
    assert parse_iw_phy_bands(output) == frozenset()


def test_integer_mhz_form_is_also_accepted():
    """`* 2412 MHz [1]` (no decimal part) is a harmless superset of the real float form."""
    output = "\t\t\t* 2412 MHz [1] (20.0 dBm)\n"

    assert parse_iw_phy_bands(output) == frozenset({Band.GHZ_2_4})


def test_5ghz_lower_bound_is_inclusive_and_upper_bound_exclusive():
    """5150.0 MHz counts as 5GHz; 5925.0 MHz (the 6GHz start) does not."""
    assert parse_iw_phy_bands("\t\t\t* 5150.0 MHz [30] (20.0 dBm)\n") == frozenset({Band.GHZ_5})
    assert parse_iw_phy_bands("\t\t\t* 5925.0 MHz [1] (20.0 dBm)\n") == frozenset()
