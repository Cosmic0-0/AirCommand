"""AirCommand: a wifi auditing orchestrator for networks you own and control."""

from pathlib import Path

# The app's own data locations. Defined here, not in the GUI launcher
# (aircommand/__main__.py), so the real-hardware harness (aircommand/hwtest/) can
# point its Engine at the same database and work directory without importing
# PySide6.
DEFAULT_DB_PATH = Path("~/.aircommand/aircommand.db").expanduser()
DEFAULT_WORK_DIR = Path("~/.aircommand/work").expanduser()
