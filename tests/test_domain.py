import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from aircommand.core.domain import (
    Band, DeauthOptions, DiscoveryOptions, EncryptionType, HandshakeKind, JobId, MacAddress, Network,
    band_of_channel,
)
from aircommand.core.persistence.db import Database

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


# --- Target / Handshake mint protection ------------------------------------------------

# Target is only legitimately constructed through TargetRepository (via
# Allowlist in production); Handshake only through HandshakeRepository (via
# Capture). Both repository methods pass the right module-private mint
# sentinel as a keyword argument, so going through them -- rather than calling
# Target(...)/Handshake(...) directly here -- is what actually exercises the
# real, reachable construction path, same as test_persistence_db.py's own
# target/handshake round-trip tests.


def _make_target(db: Database):
    return db.targets.upsert(MacAddress.parse(CANONICAL), "Home-WiFi", 6, "My house")


def _make_handshake(db: Database, target):
    return db.handshakes.insert(
        target_id=target.id,
        bssid=target.bssid,
        capture_job_id=JobId(uuid.uuid4()),
        cap_file_path=Path("capture.cap"),
        cap_file_sha256="a" * 64,
        kind=HandshakeKind.WPA2_EAPOL,
    )


def test_target_construction_through_the_repository_still_works():
    db = Database(":memory:")

    target = _make_target(db)

    assert target.bssid == MacAddress.parse(CANONICAL)
    assert target.label == "My house"


def test_handshake_construction_through_the_repository_still_works():
    db = Database(":memory:")
    target = _make_target(db)

    handshake = _make_handshake(db, target)

    assert handshake.target_id == target.id
    assert handshake.kind == HandshakeKind.WPA2_EAPOL


def test_dataclasses_replace_cannot_forge_a_tampered_target():
    # Before this fix, _proof was a plain stored field (field(default=None,
    # ...)): dataclasses.replace() reuses an EXISTING instance's own stored
    # value for any field the caller doesn't override, so
    # replace(target, bssid=attacker_bssid) silently carried over the
    # original's already-valid proof and the tampered copy's __post_init__
    # saw a valid mint sentinel anyway. _proof is now an InitVar, which is
    # never stored on the instance at all -- replace() has nothing to recover
    # it from, so a call that doesn't explicitly pass _proof= fails instead of
    # quietly minting an unauthorized Target with an attacker-controlled bssid.
    db = Database(":memory:")
    target = _make_target(db)
    attacker_bssid = MacAddress.parse("00:11:22:33:44:55")

    with pytest.raises((TypeError, ValueError)):
        replace(target, bssid=attacker_bssid)


def test_dataclasses_replace_cannot_forge_a_tampered_handshake():
    db = Database(":memory:")
    target = _make_target(db)
    handshake = _make_handshake(db, target)
    attacker_bssid = MacAddress.parse("00:11:22:33:44:55")

    with pytest.raises((TypeError, ValueError)):
        replace(handshake, bssid=attacker_bssid)


# --- DeauthOptions ----------------------------------------------------------------------


def test_deauth_options_defaults_construct_fine():
    options = DeauthOptions()

    assert options.burst_size == 5
    assert options.interval == timedelta(seconds=15)
    assert options.max_bursts is None


def test_deauth_options_accepts_explicit_valid_values():
    options = DeauthOptions(burst_size=1, interval=timedelta(seconds=0), max_bursts=0)

    assert options.burst_size == 1
    assert options.interval == timedelta(seconds=0)
    assert options.max_bursts == 0


def test_deauth_options_rejects_burst_size_zero():
    # burst_size=0 means "continuous deauth, never stop" to aireplay-ng -- that
    # must be rejected at construction, not silently accepted.
    with pytest.raises(ValueError):
        DeauthOptions(burst_size=0)


def test_deauth_options_rejects_negative_interval():
    with pytest.raises(ValueError):
        DeauthOptions(interval=timedelta(seconds=-1))


def test_deauth_options_rejects_negative_max_bursts():
    with pytest.raises(ValueError):
        DeauthOptions(max_bursts=-1)
