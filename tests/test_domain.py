import pytest

from aircommand.core.domain import MacAddress

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
