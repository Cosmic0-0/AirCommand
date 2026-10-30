"""Opt-in checks of the hardware harness (aircommand.hwtest) against the REAL
machine: real tools, real sysfs, real NetworkManager. None of them touches the
radio or runs anything through sudo: they run preflight, and the one gate test
runs the harness without --allow-network-drop and checks that it refuses.

They run only with ``AIRCOMMAND_HW_TESTS=1`` (see conftest.py, and the skipif
below). The harness's decision logic is covered against a simulated radio in
the default suite (tests/test_hwtest_*.py); this layer checks it against
reality. The full guided run with sudo is ``scripts/hw_test.sh``.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import re
import socket
import subprocess
from pathlib import Path

import pytest

from aircommand.hwtest import __main__ as hwmain
from aircommand.hwtest import probe
from aircommand.hwtest.__main__ import main
from aircommand.hwtest.report import MIN_FREE_TEXT_LENGTH, RESERVED_WORDS

pytestmark = [
    pytest.mark.hardware,
    # Backs up conftest.py's collect_ignore_glob, which does not apply when this
    # file is named directly on the command line.
    pytest.mark.skipif(os.environ.get("AIRCOMMAND_HW_TESTS") != "1",
                       reason="opt-in hardware test; set AIRCOMMAND_HW_TESTS=1"),
]

ADAPTER = os.environ.get("AIRCOMMAND_HW_ADAPTER")
BOGUS = "wlan-does-not-exist0"


def _latest_report(reports_dir: Path) -> Path:
    dirs = sorted(p for p in reports_dir.iterdir() if p.is_dir())
    assert dirs, "no report directory was produced"
    return dirs[-1]


def _results(report: Path) -> dict:
    return {(r["tier"], r["name"]): r for r in json.loads((report / "results.json").read_text())["results"]}


def _never_called(*args, **kwargs):
    raise AssertionError("the radio gate let the harness past preflight")


def test_preflight_only_produces_a_wellformed_report_and_leaves_the_app_database_alone(tmp_path):
    """--preflight-only against a made-up adapter: a complete report folder, the
    missing adapter a FAIL, every radio tier SKIP, and the app database never
    opened (preflight builds no Engine)."""
    db = tmp_path / "app" / "aircommand.db"
    rc = main(["--adapter", BOGUS, "--preflight-only", "--reports-dir", str(tmp_path / "reports"),
               "--db-path", str(db), "--work-dir", str(tmp_path / "app" / "work")])

    report = _latest_report(tmp_path / "reports")
    for name in ("report.md", "results.json", "versions.txt", "events.log", "engine.log",
                 "snapshots/before-iw-dev.txt", "snapshots/after-iw-dev.txt"):
        assert (report / name).exists(), name
    results = _results(report)
    assert results[("Preflight", "adapter detected")]["status"] == "FAIL"
    for tier in ("Monitor mode", "Discovery", "RF cleanup", "Final state"):
        assert results[(tier, "skipped")]["status"] == "SKIP"
    assert not db.exists() and not (tmp_path / "app").exists()
    assert rc == 1


def test_required_binaries_are_all_installed_on_this_machine(tmp_path):
    """The target environment has the aircrack-ng suite, iw, nmcli and the rest;
    the detail names whatever is missing."""
    main(["--adapter", BOGUS, "--preflight-only", "--reports-dir", str(tmp_path)])
    row = _results(_latest_report(tmp_path))[("Preflight", "required binaries")]
    assert row["status"] == "PASS", row["detail"]


@pytest.mark.skipif(os.environ.get("AIRCOMMAND_HW_RADIO") != "1" or not ADAPTER,
                    reason="set AIRCOMMAND_HW_RADIO=1 and AIRCOMMAND_HW_ADAPTER=<iface> for the real-adapter gate")
def test_radio_tiers_refuse_without_allow_network_drop(tmp_path, monkeypatch):
    """With a real, present, monitor-capable adapter and nothing else running,
    the ONLY thing between the harness and the radio is --allow-network-drop: it
    stops at that gate, without a sudo prompt or an Engine."""
    monkeypatch.setattr(hwmain.getpass, "getpass", _never_called)
    monkeypatch.setattr(hwmain, "_build_engine", _never_called)

    main(["--adapter", ADAPTER, "--reports-dir", str(tmp_path)])

    results = _results(_latest_report(tmp_path))
    assert results[("Preflight", "adapter detected")]["status"] == "PASS", "adapter not detected"
    assert results[("Preflight", "monitor-mode capability")]["status"] == "PASS"
    assert results[("Preflight", "radio tools and GUI not running")]["status"] == "PASS", \
        "close the GUI / stop airodump-ng etc. first"
    skipped = results[("Monitor mode", "skipped")]
    assert skipped["status"] == "SKIP" and "--allow-network-drop" in skipped["detail"]


def _secrets() -> dict[str, list[str]]:
    """This machine's real identifiers, gathered independently of the harness."""
    found: dict[str, set[str]] = {"mac": set(), "iface": set(), "host": set(), "user": set(),
                                  "ip": set(), "ssid": set()}
    for path in Path("/sys/class/net").iterdir():
        try:
            mac = (path / "address").read_text().strip().lower()
        except OSError:
            continue
        if mac and mac not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
            found["mac"].add(mac)
        if re.fullmatch(r"(wlx|enx)[0-9a-f]{12}", path.name):
            found["iface"].add(path.name)
    found["host"] |= {n for n in (socket.gethostname(), platform.node()) if n and n != "localhost"}
    found["user"].add(getpass.getuser())
    ip = subprocess.run(["ip", "-o", "addr"], capture_output=True, text=True, errors="replace").stdout
    for addr in re.findall(r"\binet6? (\S+?)/\d+", ip):
        if not addr.startswith(("127.", "::1")):
            found["ip"].add(addr)
    nmcli = subprocess.run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
                           capture_output=True, text=True, errors="replace").stdout
    for name, kind in probe.saved_connection_names() if nmcli else []:
        if kind == "802-11-wireless":
            found["ssid"].add(name)
    iw = subprocess.run(["iw", "dev"], capture_output=True, text=True, errors="replace").stdout
    found["ssid"] |= {m.strip() for m in re.findall(r"^\s*ssid (.+)$", iw, re.M)}
    # The documented free-text exemption (report.py): too short, or a tool keyword.
    found["ssid"] = {s for s in found["ssid"]
                     if len(s.strip()) >= MIN_FREE_TEXT_LENGTH and s.strip().lower() not in RESERVED_WORDS}
    return {k: sorted(v) for k, v in found.items()}


def test_redacted_report_holds_none_of_this_machines_identifiers(tmp_path):
    """--redact on a real preflight (the real adapter if AIRCOMMAND_HW_ADAPTER is
    set): no MAC (any separator, or bare inside a wlx/enx name), hostname,
    username, home path, IP address or saved wifi SSID anywhere in the report
    folder, file names included."""
    main(["--adapter", ADAPTER or BOGUS, "--preflight-only", "--redact", "--reports-dir", str(tmp_path)])
    report = _latest_report(tmp_path)
    blobs = []
    for path in report.rglob("*"):
        blobs.append(str(path.relative_to(report)))
        if path.is_file():
            blobs.append(path.read_text(errors="replace"))
    text = "\n".join(blobs)
    lowered = text.lower()

    secrets = _secrets()
    leaks = []
    for mac in secrets["mac"]:
        for form in (mac, mac.replace(":", "-"), mac.replace(":", "")):
            if form in lowered:
                leaks.append(form)
    for kind in ("iface", "host", "user", "ssid"):
        for value in secrets[kind]:
            if re.search(rf"(?<!\w){re.escape(value)}(?!\w)", text):
                leaks.append(value)
    if str(Path.home()) in text:
        leaks.append(str(Path.home()))
    for addr in secrets["ip"]:
        if re.search(rf"(?<![\w.:]){re.escape(addr)}(?![\w:]|\.\d)", text):
            leaks.append(addr)
    assert leaks == [], f"redaction left these in the report: {leaks}"
