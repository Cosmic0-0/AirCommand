"""Suite-wide test setup."""

import os

import pytest

# Qt tests run headless. Select the offscreen platform plugin before any PySide6
# import (pytest-qt builds the QApplication through qtbot), so no display is
# needed and Qt does not abort. spikes/gui-port/checks.py:61 does the same.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _no_real_monitor_interfaces(monkeypatch):
    """rf._is_monitor_interface reads the host's real sysfs. Test adapter names
    like wlan0 can exist on a developer's machine (the Kali VM's USB adapter
    usually is wlan0), and one left in monitor mode would change what a test
    sees. Every interface reads as not-monitor unless a test patches it."""
    monkeypatch.setattr("aircommand.core.rf._is_monitor_interface", lambda interface: False)
