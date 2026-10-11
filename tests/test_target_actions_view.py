"""TargetActionsView's container color and the themed deauth-confirm dialog,
against real ctk widgets + a real Engine/FakeProcRunner -- same idiom as
tests/test_capture_view.py (real Tk, real Engine, destroy in a finally).

Two things are pinned here:
  1. The outer TargetActionsView frame is transparent, not CTk's light-theme
     default (gray86) -- that default was what painted the Capture page's "odd
     white areas" through its all-transparent children.
  2. The deauth confirmation is a themed modal returning the same bool the old
     tkinter.messagebox.askyesno did, and it defaults to False so an accidental
     keypress or a closed window never fires a burst (ADR-0001's mandatory
     confirmation friction, unchanged).
"""

from __future__ import annotations

import customtkinter as ctk

from aircommand.core.domain import MacAddress
from aircommand.core.engine import Engine
from aircommand.core.procutil import FakeProcRunner
from aircommand.gui.event_pump import GuiEventPump
from aircommand.gui.target_actions_view import TargetActionsView, _DeauthConfirmDialog
from aircommand.gui.theme import PALETTE

BSSID_1 = MacAddress(value="AA:BB:CC:DD:EE:01")


class _FakeStatusBar:
    def show_error(self, msg: str) -> None:
        pass


class _FakeApp:
    def __init__(self, engine: Engine, root) -> None:
        self.engine = engine
        self.pump = GuiEventPump(engine, root)
        self.status_bar = _FakeStatusBar()


def _make_engine(tmp_path) -> Engine:
    return Engine(db_path=":memory:", work_dir=tmp_path, adapter="wlan0", proc=FakeProcRunner(script={}))


def test_view_container_is_transparent_not_the_ctk_light_default(tmp_path):
    engine = _make_engine(tmp_path)
    root = ctk.CTk()
    try:
        view = TargetActionsView(root, _FakeApp(engine, root))
        # Was ['gray86', 'gray17'] (CTk's default, resolving to near-white in
        # the app's actual Light appearance mode) -- the "white areas" source.
        assert view.cget("fg_color") == "transparent"
    finally:
        root.destroy()


def test_deauth_confirm_dialog_defaults_to_false_and_is_themed(tmp_path):
    engine = _make_engine(tmp_path)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    root = ctk.CTk()
    try:
        d = _DeauthConfirmDialog(root, target)
        try:
            assert d.result is False  # nothing clicked yet -> no burst
            assert d.cget("fg_color") == PALETTE["bg"]
            assert d._fire_button.cget("fg_color") == PALETTE["danger"]
            assert d._fire_button.cget("text_color") == PALETTE["bg"]
            assert d._cancel_button.cget("fg_color") == "transparent"
        finally:
            d.destroy()
    finally:
        root.destroy()


def test_deauth_confirm_fire_returns_true_cancel_returns_false(tmp_path):
    engine = _make_engine(tmp_path)
    target = engine.targets.add(BSSID_1, "Test-SSID", 6, "My house")
    root = ctk.CTk()
    try:
        fired = _DeauthConfirmDialog(root, target)
        fired._fire_button.invoke()
        assert fired.result is True
        assert not fired.winfo_exists()

        cancelled = _DeauthConfirmDialog(root, target)
        cancelled._cancel_button.invoke()
        assert cancelled.result is False
        assert not cancelled.winfo_exists()
    finally:
        root.destroy()
