"""SudoPasswordDialog for the Qt port. See
docs/design/gui-port/05-boundary-design.md "Sudo dialog" and
06-implementation-plan.md "Test strategy for Qt": App tests monkeypatch the
dialog away, so these drive the widget directly with qtbot (no exec(), which
would block) and assert on .result and the accepted/rejected state.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit

from aircommand.gui.sudo_dialog import SudoPasswordDialog


def test_buttons_carry_the_grid_watch_classes(qtbot):
    """Under the one app stylesheet, colors come from the class the QSS selects
    on: OK is the accent primary, Cancel the flat outline."""
    dialog = SudoPasswordDialog()
    qtbot.addWidget(dialog)
    assert dialog._ok_button.property("class") == "primary"
    assert dialog._cancel_button.property("class") == "btn"


def test_the_password_field_masks_its_input(qtbot):
    dialog = SudoPasswordDialog()
    qtbot.addWidget(dialog)
    assert dialog._entry.echoMode() == QLineEdit.EchoMode.Password


def test_no_error_param_gives_an_empty_error_label(qtbot):
    dialog = SudoPasswordDialog()
    qtbot.addWidget(dialog)
    assert dialog._error_label.text() == ""


def test_error_param_is_shown_in_the_error_label(qtbot):
    dialog = SudoPasswordDialog(error="Incorrect password")
    qtbot.addWidget(dialog)
    assert dialog._error_label.text() == "Incorrect password"


def test_ok_sets_result_to_the_typed_password_and_accepts(qtbot):
    dialog = SudoPasswordDialog()
    qtbot.addWidget(dialog)
    accepted: list[bool] = []
    dialog.accepted.connect(lambda: accepted.append(True))

    qtbot.keyClicks(dialog._entry, "hunter2")
    qtbot.mouseClick(dialog._ok_button, Qt.MouseButton.LeftButton)

    assert dialog.result == "hunter2"
    assert accepted == [True]


def test_cancel_sets_result_to_none_and_rejects(qtbot):
    dialog = SudoPasswordDialog()
    qtbot.addWidget(dialog)
    rejected: list[bool] = []
    dialog.rejected.connect(lambda: rejected.append(True))

    qtbot.keyClicks(dialog._entry, "hunter2")
    qtbot.mouseClick(dialog._cancel_button, Qt.MouseButton.LeftButton)

    assert dialog.result is None
    assert rejected == [True]


def test_closing_the_dialog_sets_result_to_none(qtbot):
    dialog = SudoPasswordDialog()
    qtbot.addWidget(dialog)

    dialog.reject()   # what the window close button and Escape route to

    assert dialog.result is None
