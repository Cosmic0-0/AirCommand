"""SudoPasswordDialog: the launch-time modal that collects the sudo password
Engine.privilege.start() needs (ADR-0002).

See docs/design/gui-port/05-boundary-design.md "Sudo dialog". QDialog.exec()
returning accepted or rejected replaces Tk's grab_set plus wait_window, and the
.result protocol the startup retry loop reads stays: OK sets result to the typed
text, Cancel or closing the window (Escape, the window close button) sets it to
None. The dialog has no parent and centers on screen, which is what the Tk
transient-to-a-withdrawn-root code effectively gave, so it still maps while
App's main window is hidden at launch. It is built from aircommand/gui/dialog.py's
themed QDialog base and button factories, so it matches the pages under the one
app stylesheet.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from aircommand.gui.dialog import Dialog, error_label, outline_button, primary_button, themed_entry


class SudoPasswordDialog(Dialog):
    def __init__(self, parent: Optional[QWidget] = None, *, error: Optional[str] = None) -> None:
        super().__init__("AirCommand sudo password", parent)
        self.result: Optional[str] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        prompt = QLabel("AirCommand needs your sudo password to control the adapter.")
        prompt.setWordWrap(True)
        layout.addWidget(prompt)

        self._entry = themed_entry(show_password=True)   # show_password masks it: this is a password
        self._entry.returnPressed.connect(self._on_ok)
        layout.addWidget(self._entry)

        self._error_label = error_label(error or "")
        layout.addWidget(self._error_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        # Cancel is the outline recipe, OK the accent primary -- the pages' own
        # button vocabulary, applied by the shared factories.
        self._cancel_button = outline_button("Cancel", self._on_cancel)
        self._ok_button = primary_button("OK", self._on_ok)
        self._ok_button.setDefault(True)
        buttons.addWidget(self._cancel_button)
        buttons.addWidget(self._ok_button)
        layout.addLayout(buttons)

    def _on_ok(self) -> None:
        self.result = self._entry.text()
        self.accept()

    def _on_cancel(self) -> None:
        self.reject()

    def reject(self) -> None:
        # Escape and the window close button both route here through QDialog, so
        # every non-OK exit leaves result None.
        self.result = None
        super().reject()
