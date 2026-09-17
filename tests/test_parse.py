import pytest

from aircommand.core.domain import EncryptionType, MacAddress
from aircommand.core.parse import parse_airodump_csv_line

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
