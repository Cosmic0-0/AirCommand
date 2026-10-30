#!/usr/bin/env bash
# Launcher for AirCommand's real-hardware test harness.
#
#   scripts/hw_test.sh <interface> [extra flags...]
#
# Example (your own adapter, full radio run):
#   scripts/hw_test.sh wlx5c628b9faa9d --allow-network-drop
#
# Safe first run (read-only, nothing through sudo, no radio):
#   scripts/hw_test.sh wlx5c628b9faa9d --preflight-only
#
# The harness runs as your NORMAL user and prompts for sudo once, in-process,
# per ADR-0002. Do NOT run this script with `sudo`; both this script and the
# harness refuse to run as root. It picks the project venv if present, then
# falls back to python3 on PATH. See docs/hardware-testing.md.
set -euo pipefail

usage() {
  echo "usage: scripts/hw_test.sh <interface> [extra flags passed to python -m aircommand.hwtest]"
  echo "  e.g. scripts/hw_test.sh wlan0 --preflight-only"
  echo "       scripts/hw_test.sh wlan0 --allow-network-drop"
}

# Repo root is the parent of this script's directory.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"

if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PY="${REPO_ROOT}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo "No python interpreter found (looked for ${REPO_ROOT}/.venv/bin/python and python3)." >&2
  exit 1
fi

if [[ $# -ge 1 && ( "$1" == "-h" || "$1" == "--help" ) ]]; then
  usage
  echo
  cd -- "${REPO_ROOT}"
  exec "${PY}" -m aircommand.hwtest --help
fi

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  echo "Refusing to run as root: AirCommand runs as your normal user and elevates per call (ADR-0002)." >&2
  echo "Run this without sudo; it will prompt for your sudo password itself when a radio tier needs it." >&2
  exit 2
fi

IFACE="$1"; shift

cd -- "${REPO_ROOT}"
exec "${PY}" -m aircommand.hwtest --adapter "${IFACE}" "$@"
