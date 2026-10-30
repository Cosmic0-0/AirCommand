"""Read-only system probes for the hardware harness: run an external command and
capture its exact argv / exit code / stdout / stderr / duration, the snapshot and
version command sets, and a few sysfs reads.

Everything here is UNPRIVILEGED and read-only (``iw``, ``ip``, ``rfkill list``,
``nmcli``, ``systemctl is-active``, ``ethtool -i``, ``lsusb``, ``uname``,
``pgrep``, sysfs). Privileged and radio-changing operations are NOT run from
here: the radio tiers go through the real ``Engine``/``RadioController`` in
checks.py, so the harness exercises the app's own code path, and the one
privileged step the harness runs itself (the Final state repair) is spelled
out in checks.py with ``sudo -n``.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class CommandResult:
    """One external command run: exactly what was invoked and what came back."""

    argv: list[str]
    returncode: Optional[int]  # None if the command could not be started or timed out
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    error: Optional[str] = None  # set when the command could not even be spawned


def run_command(argv: list[str], *, timeout: float = 15.0) -> CommandResult:
    """Run ``argv`` and capture everything. Never raises: a missing binary, a
    non-zero exit, a timeout or undecodable output all come back as a
    CommandResult the caller records verbatim. Output is decoded with
    errors="replace" for the same reason ADR-0014 gave the core's drain
    threads: an SSID or a device name is an arbitrary byte string, and one bad
    byte must not turn into an exception. stdin is /dev/null so nothing can stop
    at a prompt."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return CommandResult(argv, None, "", "", time.monotonic() - start, error=str(exc))
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        return CommandResult(argv, None, out, err, time.monotonic() - start, timed_out=True)
    except (OSError, ValueError) as exc:
        return CommandResult(argv, None, "", "", time.monotonic() - start, error=str(exc))
    return CommandResult(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - start)


# -- Snapshot command set -------------------------------------------------------
#
# Captured before AND after the radio tiers so a reviewer can diff the machine's
# state. All read-only.

def snapshot_commands(adapter: str) -> list[tuple[str, list[str]]]:
    """(label, argv) pairs for the before/after state snapshots."""
    return [
        ("iw-dev", ["iw", "dev"]),
        ("ip-link", ["ip", "link"]),
        ("ip-addr", ["ip", "addr"]),
        ("ip-route", ["ip", "route", "show"]),
        ("rfkill", ["rfkill", "list"]),
        ("nmcli-device", ["nmcli", "device", "status"]),
        ("nmcli-radio", ["nmcli", "radio"]),
        ("pgrep-airodump", ["pgrep", "-a", "airodump-ng"]),
        ("pgrep-aireplay", ["pgrep", "-a", "aireplay-ng"]),
        ("pgrep-aircrack", ["pgrep", "-a", "aircrack-ng"]),
        ("iw-dev-adapter", ["iw", "dev", adapter, "info"]),
    ]


def version_commands(adapter: str) -> list[tuple[str, list[str]]]:
    """(label, argv) pairs for the tool/kernel/driver version block."""
    return [
        ("kernel", ["uname", "-a"]),
        ("aircrack-ng", ["aircrack-ng", "--help"]),
        ("airodump-ng", ["airodump-ng", "--help"]),
        ("iw", ["iw", "--version"]),
        ("hashcat", ["hashcat", "--version"]),
        ("hcxpcapngtool", ["hcxpcapngtool", "--version"]),
        ("nmap", ["nmap", "--version"]),
        ("ethtool-driver", ["ethtool", "-i", adapter]),
        ("lsusb", ["lsusb"]),
    ]


# Binaries the app expects on PATH (docs/usage.md Prerequisites + core drivers).
REQUIRED_BINARIES = [
    "airmon-ng",
    "airodump-ng",
    "aireplay-ng",
    "aircrack-ng",
    "iw",
    "ip",
    "rfkill",
    "nmcli",
    "hashcat",
    "hcxpcapngtool",
    "nmap",
    "sudo",
    "pgrep",
]

# Nice-to-have, reported but not required (used only for the diagnostic report).
OPTIONAL_BINARIES = ["ethtool", "lsusb", "systemctl"]


def which(binary: str) -> Optional[str]:
    return shutil.which(binary)


# -- Running processes ----------------------------------------------------------

# `pgrep -f` pattern (extended regex, matched against the whole command line)
# for a running AirCommand GUI: `python -m aircommand ...` or the `aircommand`
# console script. It deliberately misses `python -m aircommand.hwtest` (this
# harness) and paths that merely contain "aircommand/" (an editor with a source
# file open).
_GUI_PGREP_PATTERN = r"(^|/)aircommand( |$)|-m aircommand( |$)"


@dataclass
class ProcessSweep:
    """`pgrep -a` lines for the radio tools and the AirCommand GUI. Empty lists
    mean nothing is running."""

    airodump: list[str] = field(default_factory=list)
    aireplay: list[str] = field(default_factory=list)
    aircrack: list[str] = field(default_factory=list)
    gui: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not (self.airodump or self.aireplay or self.aircrack or self.gui)

    def as_dict(self) -> dict:
        return {"airodump": self.airodump, "aireplay": self.aireplay,
                "aircrack": self.aircrack, "gui": self.gui}

    def pids(self) -> set[int]:
        return {pid for lines in self.as_dict().values() for pid in map(_pid_of, lines) if pid is not None}

    def new_since(self, baseline: "ProcessSweep") -> "ProcessSweep":
        """Only the processes that were not already running in ``baseline``."""
        known = baseline.pids()

        def keep(lines: list[str]) -> list[str]:
            return [ln for ln in lines if _pid_of(ln) not in known]

        return ProcessSweep(keep(self.airodump), keep(self.aireplay), keep(self.aircrack), keep(self.gui))

    def describe(self) -> str:
        parts = [f"{name}: {len(lines)}" for name, lines in self.as_dict().items() if lines]
        return ", ".join(parts) if parts else "none"


def _pid_of(line: str) -> Optional[int]:
    head = line.split(maxsplit=1)[0] if line.strip() else ""
    return int(head) if head.isdigit() else None


def _pgrep_lines(argv: list[str]) -> list[str]:
    result = run_command(argv, timeout=5.0)
    own = {os.getpid(), os.getppid()}
    return [ln for ln in result.stdout.splitlines() if ln.strip() and _pid_of(ln) not in own]


def sweep_tool_processes() -> ProcessSweep:
    """`pgrep -a` for the three long-running radio tools, plus a running
    AirCommand GUI. Lines are returned verbatim so a reviewer can see exactly
    which process is alive."""
    return ProcessSweep(
        airodump=_pgrep_lines(["pgrep", "-a", "airodump-ng"]),
        aireplay=_pgrep_lines(["pgrep", "-a", "aireplay-ng"]),
        aircrack=_pgrep_lines(["pgrep", "-a", "aircrack-ng"]),
        gui=_pgrep_lines(["pgrep", "-a", "-f", _GUI_PGREP_PATTERN]),
    )


def airmon_ng_running() -> bool:
    """True while an `airmon-ng` (a shell script, so its process name is the
    script name) is still running, e.g. one left mid-switch by a Ctrl+C."""
    return run_command(["pgrep", "-x", "airmon-ng"], timeout=5.0).returncode == 0


# -- Network state ---------------------------------------------------------------

def default_route_devices() -> list[str]:
    """Interfaces currently carrying a default route, parsed from `ip route show
    default` (e.g. ['wlo1']). Empty if there is no default route or `ip` is
    missing. Used by the only-connection guard, see checks.preflight."""
    result = run_command(["ip", "-o", "route", "show", "default"], timeout=5.0)
    devs: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if "dev" in parts and parts.index("dev") + 1 < len(parts):
            dev = parts[parts.index("dev") + 1]
            if dev not in devs:
                devs.append(dev)
    return devs


def service_state(unit: str) -> str:
    """`systemctl is-active <unit>`'s one-word answer ("active", "inactive",
    "failed", ...), or "unknown" if systemctl gave nothing."""
    result = run_command(["systemctl", "is-active", unit], timeout=5.0)
    return result.stdout.strip() or "unknown"


def saved_connection_names() -> list[tuple[str, str]]:
    """(name, type) for every saved NetworkManager connection, from `nmcli -t -f
    NAME,TYPE connection show`. Read only to feed --redact (a wifi connection's
    name defaults to its SSID), never written to the report. Empty if nmcli is
    missing or fails."""
    result = run_command(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], timeout=10.0)
    pairs = []
    for line in result.stdout.splitlines():
        fields = _split_nmcli_terse(line)
        if len(fields) >= 2 and fields[0]:
            pairs.append((fields[0], fields[-1]))
    return pairs


def _split_nmcli_terse(line: str) -> list[str]:
    """nmcli -t joins fields with ':' and escapes a literal ':' or '\\' inside a
    value with a backslash."""
    fields, current, escaped = [], [], False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


# -- sysfs ----------------------------------------------------------------------

def phy_of(interface: str) -> Optional[str]:
    """The phy behind ``interface`` ("phy4"), read from sysfs the same way
    rf.py's _phy_name does, or None."""
    try:
        with open(f"/sys/class/net/{interface}/phy80211/name") as f:
            return f.read().strip() or None
    except OSError:
        return None


def interfaces_on_phy(phy: str) -> list[str]:
    """Every network interface currently backed by ``phy``. After airmon-ng
    renames the adapter (wlan0mon) the original name is gone, so the Final
    state tier looks the phy up instead of trusting either name."""
    names = []
    for path in sorted(glob.glob("/sys/class/net/*/phy80211/name")):
        try:
            with open(path) as f:
                if f.read().strip() == phy:
                    names.append(path.split("/")[4])
        except OSError:
            continue
    return names


def rfkill_state(interface: str) -> Optional[tuple[bool, bool]]:
    """(soft_blocked, hard_blocked) for the rfkill switch of ``interface``'s phy,
    from sysfs, or None if the phy has no rfkill entry (or the interface is
    absent). Scoped to this adapter, unlike `rfkill list`, which covers every
    radio on the machine."""
    soft = hard = False
    found = False
    for entry in glob.glob(f"/sys/class/net/{interface}/phy80211/rfkill*"):
        for name in ("soft", "hard"):
            try:
                with open(os.path.join(entry, name)) as f:
                    value = f.read().strip() == "1"
            except OSError:
                continue
            found = True
            if name == "soft":
                soft = soft or value
            else:
                hard = hard or value
    return (soft, hard) if found else None
