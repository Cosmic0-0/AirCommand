import pytest

from aircommand.core.domain import EncryptionType, MacAddress
from aircommand.core.parse import parse_airmon_monitor_interface, parse_airodump_csv_line

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
