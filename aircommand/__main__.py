"""Launcher entry point for AirCommand's GUI.

Constructs the ``App`` (see ``aircommand/gui/app.py``) and runs its Tk
mainloop. This is a thin CLI wrapper only — no config-file support, logging
setup, or sudo-elevation handling belongs here; see docs/final-touches.md
item 0 and docs/usage.md for the launch story this fills in.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from aircommand.gui.app import App

DEFAULT_DB_PATH = Path("~/.aircommand/aircommand.db").expanduser()
DEFAULT_WORK_DIR = Path("~/.aircommand/work").expanduser()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aircommand", description="Launch the AirCommand GUI."
    )
    parser.add_argument(
        "--adapter",
        required=True,
        help="Wifi interface name to use (e.g. wlan0). See `ip link`.",
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

    app = App(db_path=db_path, work_dir=work_dir, adapter=args.adapter)
    app.mainloop()


if __name__ == "__main__":
    main()
