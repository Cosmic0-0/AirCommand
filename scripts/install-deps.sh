#!/usr/bin/env bash
# install-deps.sh -- install AirCommand's system packages and set up the venv.
#
# Linux + apt only (Linux Mint, Kali). It prints the sudo apt command before
# running it, and it is safe to run more than once: apt install is idempotent,
# an existing .venv is left alone, and pip install -e . just re-syncs.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGES="aircrack-ng hashcat nmap hcxtools iw libxcb-cursor0 python3-venv python3-pip"

if [ "$(uname -s)" != "Linux" ]; then
  echo "AirCommand runs on Linux only. Aborting." >&2
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This script supports apt-based distros (Linux Mint, Kali)." >&2
  echo "No apt-get found. Install these packages with your package manager," >&2
  echo "then create the venv yourself (see the README's Install & run):" >&2
  echo "  $PACKAGES" >&2
  exit 1
fi

echo "This will install system packages with sudo:"
echo "  sudo apt update"
echo "  sudo apt install $PACKAGES"
echo
sudo apt update
sudo apt install -y $PACKAGES

VENV="$REPO_ROOT/.venv"
if [ -d "$VENV" ]; then
  echo "Virtual environment already exists at $VENV; leaving it as is."
else
  echo "Creating virtual environment at $VENV"
  python3 -m venv "$VENV"
fi

echo "Installing AirCommand into the venv (pip install -e .)"
"$VENV/bin/pip" install -e "$REPO_ROOT"

echo
echo "Done. Activate the venv and run AirCommand as your normal user:"
echo "  source .venv/bin/activate"
echo "  aircommand --adapter wlan0"
