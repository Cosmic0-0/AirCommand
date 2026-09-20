"""App.__init__ end-to-end against a real Engine + FakeProcRunner, same style as
tests/test_engine.py: no mocking of core's own internals beyond
aircommand.core.privilege.subprocess.run (the sudo calls), assertions against
real object state. Real ctk.CTk widgets throughout (this environment has a
real X display) -- every App constructed here is closed via on_close() (or, on
the cancel path, never fully constructed) in a finally so tests don't leak
windows across the suite.
"""

from __future__ import annotations

import sqlite3
import subprocess
import time
import tkinter
from unittest.mock import Mock, patch

import pytest

from aircommand.core import PrivilegeStatus
from aircommand.core.domain import JobKind, MacAddress
from aircommand.core.jobs import JobRegistry
from aircommand.core.persistence.db import Database
from aircommand.core.procutil import FakeProcRunner
from aircommand.gui.app import App
from aircommand.gui.discovery_view import NetworksView, TargetPicker
from aircommand.gui.status_bar import StatusBar

PASSWORD = "correct-horse-battery-staple"

# RadioController.reserve() spawns "airmon-ng" on its first monitor-mode use --
# see test_engine.py's identical constant/comment.
AIRMON_NO_RENAME_OUTPUT = ["monitor mode already enabled on wlan0"]

TAB_NAMES = ["Discovery & Targets", "Target Actions", "Crack", "Audit Log"]


def _completed(returncode: int) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode)


def _slow_lines(count: int, delay_s: float):
    for i in range(count):
        time.sleep(delay_s)
        yield f"CH 6 ][ Elapsed: {i} s ][ 2024-01-01 10:00"


def _fake_proc() -> FakeProcRunner:
    return FakeProcRunner(script={
        "airmon-ng": AIRMON_NO_RENAME_OUTPUT,
        "airodump-ng": _slow_lines(300, 0.001),
    })


@patch("aircommand.core.privilege.subprocess.run")
def test_happy_path_builds_full_app(mock_run, tmp_path, monkeypatch):
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    app = App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=_fake_proc())
    try:
        assert app.engine.privilege.status == PrivilegeStatus.ACTIVE
        assert isinstance(app.status_bar, StatusBar)
        assert app.status_bar.master is app
        for name in TAB_NAMES:
            assert app.tabview.tab(name) is not None
        assert app._discovery_handle is not None

        assert isinstance(app.networks_view, NetworksView)
        assert isinstance(app.target_picker, TargetPicker)

        # Real proof the Discovery & Targets tab's pump wiring is genuinely
        # connected end-to-end, not just that the classes got instantiated:
        # add a Target for real, drive one pump tick by hand (same idiom as
        # tests/test_event_pump.py), and confirm the picker picked it up via
        # the TargetAdded event, not by us calling _upsert directly.
        bssid = MacAddress.parse("AA:BB:CC:DD:EE:99")
        app.engine.targets.add(bssid, "New-Net", 6, "New target")
        app.pump._tick()

        assert bssid in app.target_picker._rows
    finally:
        app.on_close()


@patch("aircommand.core.privilege.subprocess.run")
def test_on_close_shuts_down_engine_and_destroys_window(mock_run, tmp_path, monkeypatch):
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    app = App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=_fake_proc())

    app.on_close()

    with pytest.raises(sqlite3.ProgrammingError):
        app.engine._db._conn.execute("SELECT 1")
    # app IS the root Tk window (ctk.CTk), not a Toplevel -- destroying it tears
    # down the whole Tcl interpreter, so winfo_exists() itself raises TclError
    # afterward rather than returning a falsy value (unlike a Toplevel child,
    # e.g. SudoPasswordDialog, whose parent interpreter survives its destroy()).
    # Confirmed empirically against this environment's real Tk.
    with pytest.raises(tkinter.TclError):
        app.winfo_exists()


def test_incorrect_password_is_retried_then_succeeds(tmp_path, monkeypatch):
    # sudo -k, sudo -S -v (rejected), sudo -k, sudo -S -v (accepted), plus
    # padding in case a keepalive tick or two fires for real before shutdown.
    side_effect = [_completed(0), _completed(1), _completed(0), _completed(0)] + [_completed(0)] * 20
    dialog_mock = Mock(side_effect=["wrong-password", "correct-password"])
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", dialog_mock)

    with patch("aircommand.core.privilege.subprocess.run") as mock_run:
        mock_run.side_effect = side_effect
        app = App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=_fake_proc())
        try:
            assert app.engine.privilege.status == PrivilegeStatus.ACTIVE
            assert dialog_mock.call_count == 2
            assert dialog_mock.call_args_list[1] == ((), {"error": "Incorrect password — try again"})
        finally:
            app.on_close()


def test_cancelling_the_dialog_exits_the_app(tmp_path, monkeypatch):
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: None)

    with pytest.raises(SystemExit):
        App(db_path=tmp_path / "test.db", work_dir=tmp_path, adapter="wlan0", proc=_fake_proc())


@patch("aircommand.core.privilege.subprocess.run")
def test_reconciliation_banner_reflects_a_real_stale_job_found_at_startup(mock_run, tmp_path, monkeypatch):
    mock_run.return_value = _completed(0)
    monkeypatch.setattr(App, "_ask_sudo_password_dialog", lambda self, error=None: PASSWORD)

    db_path = tmp_path / "test.db"

    # Seed a stale RUNNING job row, backed by a REAL orphaned process, in a
    # file-backed DB (":memory:" gives each Database its own private store --
    # see Database._resolve_path -- so a real file is required for the
    # seeding connection and App's own Engine to see the same row).
    seed_db = Database(db_path)
    seed_jobs = JobRegistry(seed_db.jobs)
    popen = subprocess.Popen(
        ["python3", "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        job_id, _ = seed_jobs.new_job(JobKind.CAPTURE_DEAUTH, target_id=None)
        seed_jobs.record_process(job_id, popen.pid, popen.pid, "time.sleep(30)")
        seed_db.close()  # close the seeding connection before App opens its own

        app = App(db_path=db_path, work_dir=tmp_path, adapter="wlan0", proc=_fake_proc())
        try:
            assert app.status_bar._reconciliation_banner is not None
            banner_text = _banner_label_text(app.status_bar._reconciliation_banner)
            assert "1" in banner_text
        finally:
            app.on_close()
    finally:
        if popen.poll() is None:
            popen.kill()
            popen.wait()


def _banner_label_text(banner) -> str:
    import customtkinter as ctk
    texts = []
    for child in banner.winfo_children():
        if isinstance(child, ctk.CTkLabel):
            texts.append(child.cget("text"))
    return " ".join(texts)
