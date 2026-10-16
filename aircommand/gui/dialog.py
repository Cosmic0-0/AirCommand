"""Shared Grid Watch dialog pieces for the PySide6 port: a themed QDialog base
and the entry, label, and button factories every dialog needs (sudo prompt,
Add-as-Target, Add-manually, deauth confirm).

The old CustomTkinter module set PALETTE colors by hand because CTk ran in its
light default. Qt applies the one app-wide stylesheet (theme.py) to dialogs too,
so these factories only set the `class` property the QSS selects on. Centering
and modality that apply_chrome / make_modal handled in Tk become QDialog's own
exec() and parentless show, per 05-boundary-design.md "Sudo dialog".
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QPushButton, QWidget


class Dialog(QDialog):
    """A themed modal dialog base. The app stylesheet styles `QDialog`, so this
    only sets the title and modality; subclasses build the body and the buttons
    and call exec()."""

    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)


def themed_entry(show_password: bool = False, parent: Optional[QWidget] = None) -> QLineEdit:
    """A dark, hairline-bordered input (the mockup's .field input).
    `show_password=True` masks it, replacing CTk's show='*'."""
    entry = QLineEdit(parent)
    if show_password:
        entry.setEchoMode(QLineEdit.EchoMode.Password)
    return entry


def field_label(text: str, parent: Optional[QWidget] = None) -> QLabel:
    """A muted field label (the mockup's .field label)."""
    label = QLabel(text, parent)
    label.setProperty("class", "fieldLabel")
    return label


def error_label(text: str = "", parent: Optional[QWidget] = None) -> QLabel:
    """Danger-colored validation text, empty until there is something to say."""
    label = QLabel(text, parent)
    label.setProperty("class", "errorLabel")
    return label


def primary_button(
    text: str,
    on_click: Optional[Callable[[], None]] = None,
    *,
    danger: bool = False,
    parent: Optional[QWidget] = None,
) -> QPushButton:
    """An accent-fill primary button, or a danger button when `danger=True`
    (the deauth confirm's Fire button)."""
    button = QPushButton(text, parent)
    button.setProperty("class", "danger" if danger else "primary")
    if on_click is not None:
        button.clicked.connect(lambda: on_click())
    return button


def outline_button(
    text: str,
    on_click: Optional[Callable[[], None]] = None,
    parent: Optional[QWidget] = None,
) -> QPushButton:
    """A flat-bordered outline button, used for every dialog's Cancel."""
    button = QPushButton(text, parent)
    button.setProperty("class", "btn")
    if on_click is not None:
        button.clicked.connect(lambda: on_click())
    return button
