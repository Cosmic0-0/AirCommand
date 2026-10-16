"""TargetActionsView for the PySide6 port: a real view under qtbot against a
real Engine + FakeProcRunner and a real GuiEventPump, with a minimal fake status
bar. Two things are pinned: the view composes the selector and both panels and
wires the live TargetAdded/TargetRemoved subscriptions to its selector, and the
deauth confirmation is a QDialog whose exec() returns a bool, defaulting to
reject so a stray keypress or a closed window never fires a burst (ADR-0001's
mandatory confirmation friction).
"""

from __future__ import annotations

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QDialog

from aircommand.core.domain import MacAddress
from aircommand.core.engine import Engine
from aircommand.core.procutil import FakeProcRunner
from aircommand.gui.capture_view import CapturePanel
from aircommand.gui.dialog import Dialog
from aircommand.gui.enumerate_view import EnumeratePanel
from aircommand.gui.event_pump import GuiEventPump
from aircommand.gui.target_actions_view import TargetActionsView, _DeauthConfirmDialog
from aircommand.gui.target_selector import TargetSelector

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")
BSSID_2 = MacAddress(value="AA:BB:CC:DD:EE:02")


class _FakeStatusBar:
    def show_error(self, msg: str) -> None:
        pass


class _FakeApp:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._pump_parent = QObject()   # keeps the pump alive for the test's lifetime
        self.pump = GuiEventPump(engine, self._pump_parent)
        self.status_bar = _FakeStatusBar()


def _make_engine(tmp_path) -> Engine:
    return Engine(db_path=":memory:", work_dir=tmp_path, adapter="wlan0", proc=FakeProcRunner(script={}))


def test_view_composes_children_and_wires_target_subscriptions_live(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    target_1 = engine.targets.add(BSSID_1, "Net1", 6, "House1")

    view = TargetActionsView(_FakeApp(engine))
    qtbot.addWidget(view)

    # Composition and the on_change fan-out: the selector seeds target_1 and
    # calls _on_target_changed, which set both panels' target.
    assert isinstance(view.target_selector, TargetSelector)
    assert isinstance(view.capture_panel, CapturePanel)
    assert isinstance(view.enumerate_panel, EnumeratePanel)
    assert view.capture_panel.target == target_1
    assert view.enumerate_panel.target == target_1

    # Live wiring: adding a second Target publishes TargetAdded, which reaches
    # the selector over the queued pump signal (not a direct handler call).
    target_2 = engine.targets.add(BSSID_2, "Net2", 11, "House2")
    label_2 = view.target_selector._label_for(target_2)
    qtbot.waitUntil(lambda: label_2 in view.target_selector._targets_by_label, timeout=2000)

    engine.targets.remove(BSSID_2)
    qtbot.waitUntil(lambda: label_2 not in view.target_selector._targets_by_label, timeout=2000)


def test_deauth_confirm_dialog_defaults_to_rejected_and_uses_grid_watch_classes(qtbot, tmp_path):
    engine = _make_engine(tmp_path)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    dialog = _DeauthConfirmDialog(target)
    qtbot.addWidget(dialog)

    # Nothing clicked yet, so the default result is Rejected: no burst.
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert isinstance(dialog, Dialog)
    assert dialog._fire_button.property("class") == "danger"
    assert dialog._cancel_button.property("class") == "btn"


def test_deauth_confirm_fire_accepts_and_cancel_rejects_and_helper_returns_bool(qtbot, tmp_path, monkeypatch):
    engine = _make_engine(tmp_path)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")

    fired = _DeauthConfirmDialog(target)
    qtbot.addWidget(fired)
    fired._fire_button.click()
    assert fired.result() == QDialog.DialogCode.Accepted

    cancelled = _DeauthConfirmDialog(target)
    qtbot.addWidget(cancelled)
    cancelled._cancel_button.click()
    assert cancelled.result() == QDialog.DialogCode.Rejected

    # The confirm helper maps exec()'s code to the bool CapturePanel gates on.
    view = TargetActionsView(_FakeApp(engine))
    qtbot.addWidget(view)
    monkeypatch.setattr(_DeauthConfirmDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    assert view._confirm_deauth_dialog(target) is True
    monkeypatch.setattr(_DeauthConfirmDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    assert view._confirm_deauth_dialog(target) is False
