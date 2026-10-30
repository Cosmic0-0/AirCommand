"""The hardware harness's report writer: Redactor (what --redact replaces and
what it deliberately leaves alone) and Reporter (files written, rewritten at
finalize, never leaking an identifier into a file name). No test spawns a real
subprocess: Reporter.snapshot() runs against a monkeypatched probe.run_command.
"""

from __future__ import annotations

import json
import logging
import stat
import uuid
from datetime import datetime

from aircommand.core.domain import EncryptionType, MacAddress, Network
from aircommand.core.events import NetworkDiscovered
from aircommand.hwtest import probe
from aircommand.hwtest.report import Redactor, Reporter, Status, make_report_dir

MAC = "5c:62:8b:9f:aa:9d"
META = {"generated_at": "t", "adapter": "a", "argv": "", "notes": []}


def all_report_text(root) -> str:
    """Every file under the report dir, names and contents, as one string."""
    chunks = []
    for path in sorted(root.rglob("*")):
        chunks.append(str(path.relative_to(root)))
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


# --- Redactor: MACs and interface names ---

def test_wlx_interface_name_shares_the_macs_pseudonym():
    r = Redactor(True)

    out = r.scrub("Interface wlx5c628b9faa9d addr 5c:62:8b:9f:aa:9d")

    assert out == "Interface wlx-MAC-001 addr MAC-001"


def test_enx_interface_name_shares_the_macs_pseudonym():
    r = Redactor(True)

    out = r.scrub("Interface enx5c628b9faa9d addr 5c:62:8b:9f:aa:9d")

    assert out == "Interface enx-MAC-001 addr MAC-001"


def test_mac_edge_forms_hyphen_capture_filename_and_unredacted_values():
    r = Redactor(True)
    uid = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"

    assert "5c-62-8b-9f-aa-9d" not in r.scrub("addr 5c-62-8b-9f-aa-9d end")
    assert r.scrub("addr 5c-62-8b-9f-aa-9d end") == "addr MAC-001 end"
    # Capture file names: "<BSSID>-<job id>-01.cap". The job id is not a MAC.
    out = r.scrub(f"AA:BB:CC:DD:EE:01-{uid}-01.cap")
    assert out == f"MAC-002-{uid}-01.cap"
    # All-zero and broadcast addresses carry no identity and stay readable.
    assert r.scrub("00:00:00:00:00:00") == "00:00:00:00:00:00"
    assert r.scrub("ff:ff:ff:ff:ff:ff") == "ff:ff:ff:ff:ff:ff"
    # iw's cipher suite "(00-0f-ac:1)" mixes separators and is not a MAC.
    assert r.scrub("CCMP-128 (00-0f-ac:4)") == "CCMP-128 (00-0f-ac:4)"
    assert r.scrub("WEP40 (00-0f-ac:1)") == "WEP40 (00-0f-ac:1)"


# --- Redactor: SSIDs ---

def test_longer_ssid_is_replaced_before_the_shorter_one_it_contains():
    r = Redactor(True)
    r.note_ssid("Home")
    r.note_ssid("Home 5G")

    out = r.scrub("Home 5G and Home")

    assert out == "SSID-002 and SSID-001"


def test_ssid_only_matches_on_word_boundaries_in_free_text():
    r = Redactor(True)
    r.note_ssid("Net")

    assert r.scrub("NetworkManager and Network") == "NetworkManager and Network"
    assert r.scrub("joined Net.") == "joined SSID-001."


def test_reserved_word_ssid_is_replaced_only_in_structured_places():
    r = Redactor(True)
    r.note_ssid("monitor")

    assert r.scrub("\ttype monitor") == "\ttype monitor"
    assert r.scrub("Network(ssid='monitor', channel=6)") == "Network(ssid='SSID-001', channel=6)"
    assert r.scrub("\tssid monitor") == "\tssid SSID-001"


def test_short_ssid_is_replaced_in_fields_but_not_free_text_and_is_counted():
    r = Redactor(True)
    r.note_ssid("ab")

    assert r.scrub("ab cd") == "ab cd"
    assert r.scrub("ssid='ab'") == "ssid='SSID-001'"
    assert r.summary()["kept_in_free_text"] == 1


def test_escaped_forms_of_an_ssid_with_both_quote_kinds_are_all_replaced():
    ssid = 'Bob\'s "Net"'
    r = Redactor(True)
    r.note_ssid(ssid)

    assert "Bob" not in r.scrub(f"saw {repr(ssid)} here")
    assert "Bob" not in r.scrub(f"saw {json.dumps(ssid)} here")
    assert "Bob" not in r.scrub(f"saw {ssid} here")


def test_pseudonyms_are_never_rescanned_by_later_rules():
    r = Redactor(True)
    r.note_ssid("MAC")

    assert r.scrub("MAC MAC 5c:62:8b:9f:aa:9d") == "SSID-001 SSID-001 MAC-001"


def test_scrub_learns_ssids_from_structured_text_and_applies_them_to_free_text():
    r = Redactor(True)

    r.scrub("Network(ssid='Cafe Wifi')")

    assert "Cafe" not in r.scrub("connected to Cafe Wifi")


# --- Redactor: identity, IP addresses, passthrough ---

def test_identity_hostname_username_and_home_are_replaced():
    r = Redactor(True)
    r.register_identity(hostname="box-01.lan", username="alice", home="/home/alice")

    assert r.scrub("/home/alice/.aircommand/aircommand.db") == "~/.aircommand/aircommand.db"
    assert r.scrub("Linux box-01 7.0.0") == "Linux HOST 7.0.0"
    assert r.scrub("-home-alice-Documents") == "-home-USER-Documents"
    assert r.scrub("alicex") == "alicex"


def test_ipv4_addresses_get_stable_pseudonyms_and_loopback_is_kept():
    r = Redactor(True)

    out = r.scrub("inet 192.168.1.23/24 brd 192.168.1.255")
    assert "192.168.1" not in out
    assert out.count("IPV4-001") == 1 and out.count("IPV4-002") == 1

    twice = r.scrub("192.168.1.23 then 192.168.1.23")
    assert twice == "IPV4-001 then IPV4-001"

    assert r.scrub("127.0.0.1") == "127.0.0.1"
    assert r.scrub("0.0.0.0") == "0.0.0.0"
    assert r.scrub("aircrack-ng 1.7") == "aircrack-ng 1.7"


def test_ipv6_is_replaced_but_loopback_and_timestamps_are_kept():
    r = Redactor(True)

    assert "fe80" not in r.scrub("inet6 fe80::5e62:8bff:fe9f:aa9d/64 scope link")
    assert r.scrub("::1") == "::1"
    assert r.scrub("[2026-10-30 18:44:01.123]") == "[2026-10-30 18:44:01.123]"


def test_disabled_redactor_is_a_passthrough():
    r = Redactor(False)
    r.note_ssid("Cafe Wifi")
    text = "addr 5c:62:8b:9f:aa:9d inet 192.168.1.23 ssid='Cafe Wifi' Cafe Wifi"

    assert r.scrub(text) == text


# --- Reporter ---

def test_finalize_rewrites_earlier_files_with_ssids_learned_late(tmp_path):
    r = Reporter(tmp_path, redact=True)
    snap = tmp_path / "snapshots" / "before-x.txt"
    r.write_text("snapshots/before-x.txt", "connected to LateNet\n")
    assert "LateNet" in snap.read_text(encoding="utf-8")  # not yet known

    r.note_ssid("LateNet")
    r.finalize(dict(META))

    assert "LateNet" not in snap.read_text(encoding="utf-8")
    json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Redaction: on" in report


def test_non_ascii_ssid_is_redacted_from_results_and_report(tmp_path):
    r = Reporter(tmp_path, redact=True)
    r.note_ssid("Café Ñandú")
    r.add("Preflight", "x", Status.INFO, "heard Café Ñandú")

    r.finalize(dict(META))

    assert "Café" not in (tmp_path / "results.json").read_text(encoding="utf-8")
    assert "Café" not in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_preregistered_connections_are_scrubbed_and_loopback_is_left_alone(tmp_path):
    r = Reporter(tmp_path, redact=True)
    r.preregister_connections([("HomeWifi", "802-11-wireless"), ("lo", "loopback"), ("Office VPN", "vpn")])
    table = (
        "wlo1  wifi  connected  HomeWifi\n"
        "lo  loopback  connected (externally)  lo\n"
        "tun0  tun  connected  Office VPN"
    )

    path = r.write_text("snapshots/before-nmcli.txt", table)
    out = path.read_text(encoding="utf-8")

    assert "HomeWifi" not in out
    assert "Office VPN" not in out
    assert "lo  loopback  connected (externally)  lo" in out.splitlines()


def test_command_file_names_and_contents_carry_no_interface_mac(tmp_path):
    r = Reporter(tmp_path, redact=True)

    r.record(probe.CommandResult(["iw", "dev", "wlx5c628b9faa9d", "info"], 0, "", "", 0.0),
             label="iw-dev-wlx5c628b9faa9d")

    files = list((tmp_path / "commands").iterdir())
    assert len(files) == 1
    assert "5c628b9faa9d" not in files[0].name
    assert "5c628b9faa9d" not in files[0].read_text(encoding="utf-8")


def test_event_subscriber_scrubs_ssid_and_bssid_before_finalize(tmp_path):
    r = Reporter(tmp_path, redact=True)
    now = datetime.now()
    network = Network(
        bssid=MacAddress.parse("AA:BB:CC:DD:EE:01"), ssid="HiddenCove", channel=6,
        encryption=EncryptionType.WPA2, last_signal_dbm=-40, first_seen=now, last_seen=now,
    )
    event = NetworkDiscovered(event_id=uuid.uuid4(), occurred_at=now, network=network)

    r.event_subscriber()(event)

    log = (tmp_path / "events.log").read_text(encoding="utf-8")
    assert "NetworkDiscovered" in log
    assert "HiddenCove" not in log
    assert "AA:BB:CC:DD:EE:01" not in log


def test_attach_logging_copies_core_records_scrubbed_and_detach_removes_the_handler(tmp_path):
    logger = logging.getLogger("aircommand")
    before = len(logger.handlers)
    level_before = logger.level
    r = Reporter(tmp_path, redact=True)
    try:
        r.attach_logging()
        assert len(logger.handlers) == before + 1

        logging.getLogger("aircommand.core.rf").warning("airmon-ng stop %s exited %d", "wlx5c628b9faa9d", 1)

        text = (tmp_path / "engine.log").read_text(encoding="utf-8")
        assert "exited 1" in text
        assert "5c628b9faa9d" not in text
    finally:
        r.detach_logging()

    assert len(logger.handlers) == before
    assert logger.level == level_before


def test_make_report_dir_twice_gives_two_distinct_private_dirs(tmp_path):
    root = tmp_path / "reports"

    first = make_report_dir(root)
    second = make_report_dir(root)

    assert first != second
    for path in (first, second):
        assert path.is_dir()
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_snapshot_leaves_no_mac_interface_name_or_ssid_in_any_snapshot_file(tmp_path, monkeypatch):
    stdout = (
        "4: wlx5c628b9faa9d: <UP>\n"
        "    link/ether 5c:62:8b:9f:aa:9d brd ff:ff:ff:ff:ff:ff\n"
        "\tssid HomeWifi\n"
    )
    monkeypatch.setattr(probe, "run_command",
                        lambda argv, timeout=15.0: probe.CommandResult(list(argv), 0, stdout, "", 0.0))
    r = Reporter(tmp_path, redact=True)

    r.snapshot("before", "wlx5c628b9faa9d")

    files = list((tmp_path / "snapshots").iterdir())
    assert files
    text = all_report_text(tmp_path / "snapshots")
    assert "5c628b9faa9d" not in text
    assert "5c:62:8b" not in text
    assert "HomeWifi" not in text
