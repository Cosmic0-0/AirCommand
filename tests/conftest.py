"""Suite-wide test setup."""

import os

# Qt tests run headless. Select the offscreen platform plugin before any PySide6
# import (pytest-qt builds the QApplication through qtbot), so no display is
# needed and Qt does not abort. spikes/gui-port/checks.py:61 does the same.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
