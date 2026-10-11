"""SudoPasswordDialog against a real ctk.CTk()/CTkToplevel — see
docs/design/gui-structure.md 'Sudo password dialog'. Real Tk widgets, not
mocks (this environment has a real X display); every window constructed here
is destroyed in a finally so tests don't leak windows across the suite.
"""

from __future__ import annotations

import time

import customtkinter as ctk

from aircommand.gui.sudo_dialog import SudoPasswordDialog
from aircommand.gui.theme import PALETTE


def test_dialog_background_and_buttons_use_grid_watch_tokens():
    """Regression for the reported "pop-ups not done properly": the toplevel
    background is PALETTE["bg"] (not CTk's light-theme near-white default) and
    OK/Cancel use the accent-fill / outline recipes, not CTk's default blue."""
    root = ctk.CTk()
    try:
        dialog = SudoPasswordDialog(root)
        try:
            assert dialog.cget("fg_color") == PALETTE["bg"]
            assert dialog._ok_button.cget("fg_color") == PALETTE["accent"]
            assert dialog._ok_button.cget("text_color") == PALETTE["bg"]
            assert dialog._cancel_button.cget("fg_color") == "transparent"
            assert dialog._entry.cget("fg_color") == PALETTE["bg"]
        finally:
            dialog.destroy()
    finally:
        root.destroy()


def test_no_error_param_gives_an_empty_error_label():
    root = ctk.CTk()
    try:
        dialog = SudoPasswordDialog(root)
        try:
            assert dialog._error_label.cget("text") == ""
        finally:
            dialog.destroy()
    finally:
        root.destroy()


def test_error_param_is_shown_in_the_error_label():
    root = ctk.CTk()
    try:
        dialog = SudoPasswordDialog(root, error="Incorrect password")
        try:
            assert dialog._error_label.cget("text") == "Incorrect password"
        finally:
            dialog.destroy()
    finally:
        root.destroy()


def test_dialog_maps_even_when_the_parent_window_is_withdrawn():
    """Launch-hang regression. App.__init__ withdraw()s the main window and then
    opens this dialog *before* the main window is ever shown -- the sudo prompt
    is the first UI at launch. apply_chrome() used to mark the dialog
    transient-for its parent unconditionally; a transient window whose master is
    withdrawn/unmapped is never mapped by the X window manager, so the prompt
    never appeared and App._ask_sudo_password_dialog's wait_window() on it
    blocked forever with zero output -- the exact silent deadlock reported. The
    fix (dialog.apply_chrome) skips the transient hint while the parent is
    unmapped, so the dialog maps normally and can be acted on (and closed,
    unblocking wait_window).

    This is the path no existing test covered: test_app.py monkeypatches
    _ask_sudo_password_dialog away in every test, so the real dialog is never
    built over App's withdrawn root; the other tests here build it over a root
    that is never withdrawn and only read cget() attributes, which an unmapped
    window still answers.

    Asserts on visibility, NOT by driving wait_window(): a reintroduced bug must
    FAIL this test, not hang the whole suite. update() pumps the WM's MapNotify
    without blocking on input the way wait_window() would, and the loop is
    bounded, so an unmappable dialog ends as a failed assertion within seconds,
    never a hang. (An after()-scheduled destroy would not do: it fires regardless
    of mapping, so wait_window() would return even with the bug present.)
    """
    root = ctk.CTk()
    root.withdraw()   # exactly what App.__init__ does before the sudo prompt
    try:
        dialog = SudoPasswordDialog(root)
        try:
            deadline = time.monotonic() + 5.0
            while not dialog.winfo_viewable() and time.monotonic() < deadline:
                dialog.update()   # process pending WM events; returns at once
                time.sleep(0.01)
            assert dialog.winfo_ismapped(), "sudo dialog never mapped over a withdrawn parent"
            assert dialog.winfo_viewable(), "sudo dialog never became viewable over a withdrawn parent"
        finally:
            dialog.destroy()
    finally:
        root.destroy()


def test_ok_button_sets_result_to_typed_password_and_destroys_dialog():
    root = ctk.CTk()
    try:
        dialog = SudoPasswordDialog(root)
        dialog._entry.insert(0, "hunter2")

        dialog._ok_button.invoke()

        assert dialog.result == "hunter2"
        assert not dialog.winfo_exists()
    finally:
        root.destroy()


def test_cancel_button_sets_result_to_none_and_destroys_dialog():
    root = ctk.CTk()
    try:
        dialog = SudoPasswordDialog(root)
        dialog._entry.insert(0, "hunter2")

        dialog._cancel_button.invoke()

        assert dialog.result is None
        assert not dialog.winfo_exists()
    finally:
        root.destroy()


def test_window_close_sets_result_to_none_and_destroys_dialog():
    root = ctk.CTk()
    try:
        dialog = SudoPasswordDialog(root)

        dialog._on_cancel()  # what WM_DELETE_WINDOW is wired to

        assert dialog.result is None
        assert not dialog.winfo_exists()
    finally:
        root.destroy()
