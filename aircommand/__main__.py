"""Launcher entry point for AirCommand's GUI.

Creates the QApplication, registers the bundled fonts and applies the one app
stylesheet, constructs the ``App`` (see ``aircommand/gui/app.py``), and runs the
Qt event loop. The QApplication is created before ``App`` because App's sudo
dialog needs it (docs/design/gui-port/05-boundary-design.md "Launch order").
This is a thin CLI wrapper only: no config-file support, logging setup, or
sudo-elevation handling belongs here; see docs/final-touches.md item 0 and
docs/usage.md for the launch story this fills in.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from aircommand.gui import theme
from aircommand.gui.app import App

DEFAULT_DB_PATH = Path("~/.aircommand/aircommand.db").expanduser()
DEFAULT_WORK_DIR = Path("~/.aircommand/work").expanduser()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aircommand", description="Launch the AirCommand GUI."
    )
    parser.add_argument(
        "--adapter",
        help="Wifi interface name to pre-select (e.g. wlan0). See `ip link`. Optional: "
        "if omitted, pick one on the Management page once the app is open.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to the SQLite database (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=DEFAULT_WORK_DIR,
        help=f"Path to the working directory for artifacts (default: {DEFAULT_WORK_DIR})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = args.db_path.expanduser()
    work_dir = args.work_dir.expanduser()

    work_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    qt_app = QApplication(sys.argv)
    theme.register_fonts()               # needs the QApplication, so after it is built
    qt_app.setStyleSheet(theme.STYLESHEET)

    app = App(db_path=db_path, work_dir=work_dir, adapter=args.adapter)   # keep a reference
    raise SystemExit(qt_app.exec())


if __name__ == "__main__":
    main()
