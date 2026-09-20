"""StatusBar against a real ctk.CTk() root — see docs/design/gui-structure.md
'Status bar'. Real Tk widgets, not mocks; every window constructed here is
destroyed in a finally so tests don't leak windows across the suite.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import customtkinter as ctk

from aircommand.core.allowlist import Allowlist
from aircommand.core.domain import MacAddress
from aircommand.core.events import EventBus, SudoKeepaliveFailed, SudoKeepaliveRecovered
from aircommand.core.persistence.db import Database
from aircommand.core.privilege import PrivilegeStatus
from aircommand.core.reconciliation import ReconciliationSummary
from aircommand.gui.status_bar import StatusBar

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")


def make_target():
    allowlist = Allowlist(Database(":memory:").targets, EventBus())
    return allowlist.add(BSSID_1, "Home-WiFi", 6, "My house")


def test_no_processes_terminated_builds_no_banner():
    root = ctk.CTk()
    try:
        summary = ReconciliationSummary(stale_job_count=0, processes_terminated=0,
                                         interrupted_deauth_target_ids=())
        bar = StatusBar(root, PrivilegeStatus.ACTIVE, summary, [])

        assert bar._reconciliation_banner is None
    finally:
        root.destroy()


def test_processes_terminated_with_no_interrupted_targets_shows_banner_without_audit_log_pointer():
    root = ctk.CTk()
    try:
        summary = ReconciliationSummary(stale_job_count=2, processes_terminated=2,
                                         interrupted_deauth_target_ids=())
        bar = StatusBar(root, PrivilegeStatus.ACTIVE, summary, [])

        assert bar._reconciliation_banner is not None
        banner_text = _banner_label_text(bar._reconciliation_banner)
        assert "2" in banner_text
        assert "Audit Log" not in banner_text
    finally:
        root.destroy()


def test_processes_terminated_with_interrupted_targets_mentions_audit_log():
    root = ctk.CTk()
    try:
        target = make_target()
        summary = ReconciliationSummary(stale_job_count=1, processes_terminated=1,
                                         interrupted_deauth_target_ids=(target.id,))
        bar = StatusBar(root, PrivilegeStatus.ACTIVE, summary, [target])

        assert bar._reconciliation_banner is not None
        banner_text = _banner_label_text(bar._reconciliation_banner)
        assert "Audit Log" in banner_text
    finally:
        root.destroy()


def test_show_sudo_warning_and_clear_sudo_warning_toggle_privilege_label():
    root = ctk.CTk()
    try:
        summary = ReconciliationSummary(stale_job_count=0, processes_terminated=0,
                                         interrupted_deauth_target_ids=())
        bar = StatusBar(root, PrivilegeStatus.ACTIVE, summary, [])

        failed = SudoKeepaliveFailed(event_id=uuid.uuid4(), occurred_at=datetime.now(), consecutive_failures=1)
        bar.show_sudo_warning(failed)
        assert "LOST" in bar._privilege_label.cget("text")

        recovered = SudoKeepaliveRecovered(event_id=uuid.uuid4(), occurred_at=datetime.now())
        bar.clear_sudo_warning(recovered)
        assert "ACTIVE" in bar._privilege_label.cget("text")
    finally:
        root.destroy()


def test_show_error_sets_error_label_text():
    root = ctk.CTk()
    try:
        summary = ReconciliationSummary(stale_job_count=0, processes_terminated=0,
                                         interrupted_deauth_target_ids=())
        bar = StatusBar(root, PrivilegeStatus.ACTIVE, summary, [])

        bar.show_error("boom")

        assert bar._error_label.cget("text") == "boom"
    finally:
        root.destroy()


def _banner_label_text(banner: ctk.CTkFrame) -> str:
    """Concatenates the text of every CTkLabel child of the banner frame — the
    banner is a small frame with a label and a dismiss button, and this avoids
    hard-coding which child is the label."""
    texts = []
    for child in banner.winfo_children():
        if isinstance(child, ctk.CTkLabel):
            texts.append(child.cget("text"))
    return " ".join(texts)
