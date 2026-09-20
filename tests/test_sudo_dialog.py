"""SudoPasswordDialog against a real ctk.CTk()/CTkToplevel — see
docs/design/gui-structure.md 'Sudo password dialog'. Real Tk widgets, not
mocks (this environment has a real X display); every window constructed here
is destroyed in a finally so tests don't leak windows across the suite.
"""

from __future__ import annotations

import customtkinter as ctk

from aircommand.gui.sudo_dialog import SudoPasswordDialog


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
