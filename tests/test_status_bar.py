"""StatusBar for the Qt port. See docs/design/gui-structure.md "Status bar" and
docs/design/gui-port/05-boundary-design.md "View mapping". Widgets are built
under qtbot (which owns the QApplication and cleans each one up); the banner,
the privilege pill, and the error surface are read through their Qt getters.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from PySide6.QtWidgets import QLabel

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


def _summary(stale: int, terminated: int, interrupted=()) -> ReconciliationSummary:
    return ReconciliationSummary(
        stale_job_count=stale, processes_terminated=terminated, interrupted_deauth_target_ids=interrupted
    )


def _banner_label_text(banner) -> str:
    """Concatenate the text of every QLabel child of the banner, so the test
    does not hard-code which child is the message."""
    return " ".join(child.text() for child in banner.findChildren(QLabel))


def test_no_processes_terminated_builds_no_banner(qtbot):
    bar = StatusBar(None, PrivilegeStatus.ACTIVE, _summary(0, 0), [])
    qtbot.addWidget(bar)
    assert bar._reconciliation_banner is None


def test_processes_terminated_with_no_interrupted_targets_shows_banner_without_audit_log_pointer(qtbot):
    bar = StatusBar(None, PrivilegeStatus.ACTIVE, _summary(2, 2), [])
    qtbot.addWidget(bar)

    assert bar._reconciliation_banner is not None
    banner_text = _banner_label_text(bar._reconciliation_banner)
    assert "2" in banner_text
    assert "Audit Log" not in banner_text


def test_processes_terminated_with_interrupted_targets_mentions_logs(qtbot):
    target = make_target()
    bar = StatusBar(None, PrivilegeStatus.ACTIVE, _summary(1, 1, (target.id,)), [target])
    qtbot.addWidget(bar)

    assert bar._reconciliation_banner is not None
    banner_text = _banner_label_text(bar._reconciliation_banner)
    assert "Logs" in banner_text   # renamed from "Audit Log tab" -- see status_bar.py


def test_show_and_clear_sudo_warning_toggle_the_privilege_pill(qtbot):
    bar = StatusBar(None, PrivilegeStatus.ACTIVE, _summary(0, 0), [])
    qtbot.addWidget(bar)

    failed = SudoKeepaliveFailed(event_id=uuid.uuid4(), occurred_at=datetime.now(), consecutive_failures=1)
    bar.show_sudo_warning(failed)
    assert "LOST" in bar._privilege_pill.text()

    recovered = SudoKeepaliveRecovered(event_id=uuid.uuid4(), occurred_at=datetime.now())
    bar.clear_sudo_warning(recovered)
    assert bar._privilege_pill.text() == "Sudo session active"


def test_show_error_sets_error_label_text(qtbot):
    bar = StatusBar(None, PrivilegeStatus.ACTIVE, _summary(0, 0), [])
    qtbot.addWidget(bar)

    bar.show_error("boom")

    assert bar._error_label.text() == "boom"
