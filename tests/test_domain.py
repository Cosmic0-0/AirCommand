from datetime import datetime

import pytest

from aircommand.core.domain import (
    Band, DiscoveryOptions, EncryptionType, MacAddress, Network, band_of_channel,
)

CANONICAL = "AA:BB:CC:DD:EE:FF"


def test_parse_colon_form():
    assert MacAddress.parse("aa:bb:cc:dd:ee:ff").value == CANONICAL


def test_parse_dash_form():
    assert MacAddress.parse("aa-bb-cc-dd-ee-ff").value == CANONICAL


def test_parse_bare_hex_form():
    assert MacAddress.parse("aabbccddeeff").value == CANONICAL


def test_parse_lowercase_normalizes_to_uppercase():
    assert MacAddress.parse("aa:bb:cc:dd:ee:ff").value == CANONICAL


def test_parse_mixed_case_normalizes_to_uppercase():
    assert MacAddress.parse("Aa-bB-cC-dD-eE-fF").value == CANONICAL


def test_parse_returns_mac_address_instance():
    assert MacAddress.parse("aabbccddeeff") == MacAddress(value=CANONICAL)


@pytest.mark.parametrize(
    "raw",
    [
        "aa:bb:cc:dd:ee",
        "aabbccddee",
    ],
    ids=["too_short_colon", "too_short_bare_hex"],
)
def test_parse_too_short_raises_value_error(raw):
    with pytest.raises(ValueError) as excinfo:
        MacAddress.parse(raw)
    assert raw in str(excinfo.value)


@pytest.mark.parametrize(
    "raw",
    [
        "aa:bb:cc:dd:ee:ff:00",
        "aabbccddeeff00",
    ],
    ids=["too_long_colon", "too_long_bare_hex"],
)
def test_parse_too_long_raises_value_error(raw):
    with pytest.raises(ValueError) as excinfo:
        MacAddress.parse(raw)
    assert raw in str(excinfo.value)


@pytest.mark.parametrize(
    "raw",
    [
        "gg:bb:cc:dd:ee:ff",
        "aabbccddeegg",
    ],
    ids=["non_hex_colon", "non_hex_bare_hex"],
)
def test_parse_non_hex_characters_raises_value_error(raw):
    with pytest.raises(ValueError) as excinfo:
        MacAddress.parse(raw)
    assert raw in str(excinfo.value)


@pytest.mark.parametrize(
    "raw",
    [
        "aa:bb:cc:dd:ee:ffff",
        "a:bb:cc:dd:ee:ff",
    ],
    ids=["octet_too_long", "octet_too_short"],
)
def test_parse_wrong_octet_count_raises_value_error(raw):
    with pytest.raises(ValueError) as excinfo:
        MacAddress.parse(raw)
    assert raw in str(excinfo.value)


@pytest.mark.parametrize(
    "raw",
    [
        "aa:bb-cc:dd:ee:ff",
        "aa-bb:cc-dd:ee-ff",
    ],
    ids=["single_dash_among_colons", "alternating_separators"],
)
def test_parse_mixed_separators_raises_value_error(raw):
    with pytest.raises(ValueError) as excinfo:
        MacAddress.parse(raw)
    assert raw in str(excinfo.value)


# --- Band ----------------------------------------------------------------------------


@pytest.mark.parametrize("channel", [1, 6, 11, 13, 14])
def test_2_4ghz_channels_map_to_the_2_4ghz_band(channel):
    assert band_of_channel(channel) == Band.GHZ_2_4


@pytest.mark.parametrize("channel", [36, 40, 52, 100, 144, 149, 165, 177])
def test_5ghz_channels_map_to_the_5ghz_band(channel):
    assert band_of_channel(channel) == Band.GHZ_5


@pytest.mark.parametrize("channel", [-1, 0, 15, 31, 178, 184, 233])
def test_channels_outside_both_bands_have_no_band(channel):
    """-1 is what airodump-ng reports for a channel it couldn't pin down. 233
    is a real 6GHz channel number: it must not be guessed into a band."""
    assert band_of_channel(channel) is None


def test_network_band_is_derived_from_its_channel():
    now = datetime(2024, 1, 1)

    def network(channel):
        return Network(
            bssid=MacAddress.parse("AA:BB:CC:DD:EE:01"), ssid="x", channel=channel,
            encryption=EncryptionType.WPA2, last_signal_dbm=-40, first_seen=now, last_seen=now,
        )

    assert network(6).band == Band.GHZ_2_4
    assert network(36).band == Band.GHZ_5
    assert network(-1).band is None


def test_band_values_are_the_display_strings():
    assert Band.GHZ_2_4.value == "2.4 GHz"
    assert Band.GHZ_5.value == "5 GHz"


def test_discovery_options_default_is_2_4ghz_only_matching_airodumps_own_default():
    assert DiscoveryOptions().bands == frozenset({Band.GHZ_2_4})


def test_discovery_options_rejects_an_empty_band_set():
    with pytest.raises(ValueError):
        DiscoveryOptions(bands=frozenset())
