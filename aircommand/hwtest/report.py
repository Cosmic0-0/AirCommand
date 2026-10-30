"""The report writer: accumulates per-check results and the raw output of every
command, then writes one self-contained directory the operator hands back for
diagnosis.

Layout of ``<reports-dir>/<timestamp>/`` (created 0700):
    report.md     human/Claude-readable summary, per-check PASS/FAIL/SKIP + reasons
    results.json  machine-readable mirror of the same
    commands/     one file per command: NNNN-<label>.txt for the harness's own
                  read-only probes, NNNN-core-<tool>.txt for every command the
                  core spawned through its ProcRunner (airmon-ng, systemctl,
                  airodump-ng, aircrack-ng, iw)
    snapshots/    before-* and after-* read-only state captures
    versions.txt  kernel / python / tool / driver versions
    events.log    the engine's event stream during the run
    engine.log    the core's own log records (the `aircommand` logger)

The app's database and work directory (captures, airodump-ng CSVs) are never
in here; the harness only writes facts derived from them.

Redaction (``--redact``). Every file is written as soon as it exists, scrubbed
with what is known at that moment, and its raw text is kept in memory;
``finalize()`` then rewrites every file from that raw text with the complete
map, so an SSID first learned late in the run is still replaced in a file
written early. Replaced, each with a stable pseudonym:
  * MAC addresses with ':' or '-' separators (except all-zero and broadcast),
    and the MAC inside wlx<12 hex>/enx<12 hex> interface names, which maps to
    the same pseudonym as the address itself;
  * SSIDs: saved NetworkManager connection names (read before anything is
    written), `ssid` lines from `iw`, SSIDs heard by Discovery, and a capture
    Target's SSID; Target labels too;
  * this machine's hostname, your username and your home directory path;
  * IPv4 and IPv6 addresses, except loopback, unspecified and broadcast.
Not replaced: in free text, an SSID shorter than MIN_FREE_TEXT_LENGTH
characters or equal to a word the tools themselves print (RESERVED_WORDS); it
would otherwise rewrite unrelated output such as "type monitor". Those SSIDs
are still replaced in structured places (`ssid=`/`label=` fields in events.log,
`ssid` lines from iw). The pseudonym maps are never written out.
"""

from __future__ import annotations

import ast
import getpass
import ipaddress
import json
import logging
import platform
import re
import sys
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from aircommand.hwtest import probe

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# One separator style per address. ':' is excluded on both sides so a run of
# 2-digit groups inside a longer IPv6 address is not mistaken for a MAC; '-' is
# allowed after it because capture.py names its files "<BSSID>-<job id>-01.cap".
_MAC_RE = re.compile(r"(?<![0-9A-Fa-f:])[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])")
# systemd's predictable names for USB wifi/ethernet adapters embed the MAC with
# no separators (wlx5c628b9faa9d is 5c:62:8b:9f:aa:9d).
_IFACE_MAC_RE = re.compile(r"(?<![0-9A-Za-z])(wlx|enx)([0-9A-Fa-f]{12})(?![0-9A-Za-z])")
_IPV4_RE = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?!\w|\.\d)")
_IPV6_RE = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
# `ssid='...'` / `label='...'` as repr() prints them in events.log.
_REPR_FIELD_RE = re.compile(r"\b(ssid|label)=('(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\")")
# `iw dev` prints an associated interface's SSID on its own "\tssid <name>" line.
_IW_SSID_RE = re.compile(r"^([ \t]*ssid )(.+)$", re.M)

_UNREDACTED_MACS = {"00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"}

# In free text, an SSID shorter than this is left alone (see module docstring).
MIN_FREE_TEXT_LENGTH = 3
# Words the report's own tool output uses structurally. An SSID equal to one of
# these (case-insensitively) is left alone in free text, because replacing it
# would corrupt the very line the diagnosis depends on ("type monitor").
RESERVED_WORDS = frozenset({
    "monitor", "managed", "type", "station", "mesh", "ibss", "wifi", "wlan",
    "ethernet", "loopback", "bridge", "connected", "disconnected", "connecting",
    "unavailable", "unmanaged", "enabled", "disabled", "active", "inactive",
    "failed", "unknown", "addr", "channel", "ssid", "phy", "txpower", "none",
    "true", "false", "pass", "fail", "skip", "warn", "info", "linux",
})

# Placeholders keep one scrub() a single pass: every replacement is parked as a
# private-use character pair and expanded at the very end, so no later rule can
# rescan (and re-replace) a pseudonym an earlier rule inserted.
_PH_OPEN, _PH_CLOSE, _PH_BASE = "", "", 0xF0000
_PH_RE = re.compile(f"{_PH_OPEN}(.){_PH_CLOSE}", re.S)


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    WARN = "WARN"
    INFO = "INFO"


@dataclass
class CheckResult:
    tier: str
    name: str
    status: Status
    detail: str
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


def _unquote(literal: str) -> str:
    try:
        value = ast.literal_eval(literal)
    except (ValueError, SyntaxError):
        return literal[1:-1]
    return value if isinstance(value, str) else literal[1:-1]


class Redactor:
    """Maps the operator's identifiers to stable pseudonyms. A no-op passthrough
    unless ``--redact`` was given. See the module docstring for exactly what is
    and isn't replaced."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._literals: dict[str, str] = {}   # exact text form -> pseudonym
        self._ssids: dict[str, str] = {}      # raw SSID -> "SSID-001"
        self._labels: dict[str, str] = {}     # raw Target label / other connection -> "LABEL-001"
        self._macs: dict[str, str] = {}       # "AA:BB:..." -> "MAC-001"
        self._ips: dict[str, str] = {}        # compressed address -> "IPV4-001"/"IPV6-001"
        self._kept: set[str] = set()          # raw SSIDs left alone in free text
        self._pattern: Optional[re.Pattern] = None
        self._lock = threading.RLock()

    # -- registration ------------------------------------------------------------

    def register_identity(self, *, hostname: str, username: str, home: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            if home and home not in ("/", "/root"):
                self._add_literal(home.rstrip("/"), "~")
            for name in {hostname, hostname.split(".")[0]}:
                if len(name) >= 2 and name != "localhost":
                    self._add_literal(name, "HOST")
            if len(username) >= 2 and username != "root":
                self._add_literal(username, "USER")

    def note_ssid(self, ssid: Optional[str]) -> None:
        if not self.enabled or not ssid:
            return
        with self._lock:
            if ssid not in self._ssids:
                self._ssids[ssid] = f"SSID-{len(self._ssids) + 1:03d}"
            self._add_value_forms(ssid, self._ssids[ssid])

    def note_label(self, label: Optional[str]) -> None:
        """A Target label or a non-wifi connection name: free text the
        operator chose, so it may well name them or their network."""
        if not self.enabled or not label:
            return
        with self._lock:
            if label in self._ssids:
                return
            if label not in self._labels:
                self._labels[label] = f"LABEL-{len(self._labels) + 1:03d}"
            self._add_value_forms(label, self._labels[label])

    def harvest(self, text: str) -> None:
        """Registers the SSIDs and labels a text reveals in a structured way, so
        they are also replaced wherever they appear as free text elsewhere."""
        if not self.enabled or not text:
            return
        for match in _REPR_FIELD_RE.finditer(text):
            value = _unquote(match.group(2))
            (self.note_ssid if match.group(1) == "ssid" else self.note_label)(value)
        for match in _IW_SSID_RE.finditer(text):
            self.note_ssid(match.group(2).rstrip())
        for line in text.splitlines():
            # An airodump-ng CSV row (in a core log line, say): ESSID is field 13.
            if line.count(",") >= 13:
                fields = [f.strip() for f in line.split(",")]
                if _MAC_RE.fullmatch(fields[0]) and fields[13]:
                    self.note_ssid(fields[13])

    def _add_value_forms(self, raw: str, pseudonym: str) -> None:
        # The same value as repr() and json.dumps() print it, so an escaped quote,
        # backslash or control character still matches.
        for form in {raw, repr(raw)[1:-1], json.dumps(raw, ensure_ascii=False)[1:-1]}:
            stripped = form.strip()
            if len(stripped) < MIN_FREE_TEXT_LENGTH or stripped.lower() in RESERVED_WORDS:
                self._kept.add(raw)
                continue
            self._add_literal(form, pseudonym)

    def _add_literal(self, text: str, pseudonym: str) -> None:
        if text and self._literals.get(text) != pseudonym:
            self._literals[text] = pseudonym
            self._pattern = None

    # -- scrubbing ----------------------------------------------------------------

    def scrub(self, text: str) -> str:
        if not self.enabled or not text:
            return text
        with self._lock:
            self.harvest(text)
            slots: dict[str, str] = {}

            def hold(pseudonym: str) -> str:
                if pseudonym not in slots:
                    slots[pseudonym] = f"{_PH_OPEN}{chr(_PH_BASE + len(slots))}{_PH_CLOSE}"
                return slots[pseudonym]

            text = _REPR_FIELD_RE.sub(lambda m: self._repr_field(m, hold), text)
            text = _IW_SSID_RE.sub(lambda m: self._iw_ssid_line(m, hold), text)
            pattern = self._literal_pattern()
            if pattern is not None:
                text = pattern.sub(lambda m: hold(self._literals[m.group(0)]), text)
            text = _IFACE_MAC_RE.sub(lambda m: hold(f"{m.group(1)}-{self._mac(m.group(2))}"), text)
            text = _MAC_RE.sub(lambda m: self._mac_sub(m.group(0), hold), text)
            text = _IPV4_RE.sub(lambda m: self._ip_sub(m.group(0), hold), text)
            text = _IPV6_RE.sub(lambda m: self._ip_sub(m.group(0), hold), text)
            names = {placeholder: pseudonym for pseudonym, placeholder in slots.items()}
            return _PH_RE.sub(lambda m: names.get(m.group(0), ""), text)

    def _repr_field(self, match: re.Match, hold: Callable[[str], str]) -> str:
        name, value = match.group(1), _unquote(match.group(2))
        if not value:
            return match.group(0)
        pseudonym = self._ssids.get(value) if name == "ssid" else self._labels.get(value, self._ssids.get(value))
        return f"{name}='{hold(pseudonym)}'" if pseudonym else match.group(0)

    def _iw_ssid_line(self, match: re.Match, hold: Callable[[str], str]) -> str:
        pseudonym = self._ssids.get(match.group(2).rstrip())
        return match.group(1) + hold(pseudonym) if pseudonym else match.group(0)

    def _literal_pattern(self) -> Optional[re.Pattern]:
        if self._pattern is None and self._literals:
            alternatives = "|".join(re.escape(t) for t in sorted(self._literals, key=len, reverse=True))
            self._pattern = re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)")
        return self._pattern

    def _mac(self, raw: str) -> str:
        hexdigits = re.sub(r"[^0-9A-Fa-f]", "", raw).upper()
        key = ":".join(hexdigits[i:i + 2] for i in range(0, 12, 2))
        if key not in self._macs:
            self._macs[key] = f"MAC-{len(self._macs) + 1:03d}"
        return self._macs[key]

    def _mac_sub(self, raw: str, hold: Callable[[str], str]) -> str:
        key = raw.upper().replace("-", ":")
        return raw if key in _UNREDACTED_MACS else hold(self._mac(raw))

    def _ip_sub(self, raw: str, hold: Callable[[str], str]) -> str:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return raw
        if address.is_loopback or address.is_unspecified or str(address) == "255.255.255.255":
            return raw
        key = str(address)
        if key not in self._ips:
            kind = "IPV4" if address.version == 4 else "IPV6"
            count = sum(1 for v in self._ips.values() if v.startswith(kind)) + 1
            self._ips[key] = f"{kind}-{count:03d}"
        return hold(self._ips[key])

    def summary(self) -> dict:
        """Counts only, never values: for the report's redaction note."""
        with self._lock:
            return {"ssids": len(self._ssids), "labels": len(self._labels), "macs": len(self._macs),
                    "ip_addresses": len(self._ips), "kept_in_free_text": len(self._kept)}


class _ReportLogHandler(logging.Handler):
    """Copies the core's own log records (the `aircommand` logger) into
    engine.log: the CSV-still-absent warning, airmon-ng's non-zero exits,
    shutdown's "Failed to fully restore managed mode", and so on."""

    def __init__(self, reporter: "Reporter") -> None:
        super().__init__(logging.INFO)
        self._reporter = reporter
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._reporter.append_text("engine.log", self.format(record) + "\n")
        except Exception:
            self.handleError(record)


class Reporter:
    def __init__(self, report_dir: Path, *, redact: bool) -> None:
        self.dir = report_dir
        self.redactor = Redactor(redact)
        self.redactor.register_identity(hostname=platform.node(), username=_username(), home=str(Path.home()))
        self.results: list[CheckResult] = []
        self._lock = threading.RLock()
        self._command_index = 0
        self._raw: dict[str, list[str]] = {}   # path relative to self.dir -> raw text chunks
        self._recorders: list = []
        self._log_handler: Optional[_ReportLogHandler] = None
        self._previous_log_level = logging.NOTSET
        (self.dir / "commands").mkdir(parents=True, exist_ok=True)
        (self.dir / "snapshots").mkdir(parents=True, exist_ok=True)

    # -- results ---------------------------------------------------------------

    def add(self, tier: str, name: str, status: Status, detail: str, **data) -> CheckResult:
        result = CheckResult(tier=tier, name=name, status=status, detail=detail, data=data)
        with self._lock:
            self.results.append(result)
        return result

    def note_ssid(self, ssid: Optional[str]) -> None:
        self.redactor.note_ssid(ssid)

    def note_target(self, target) -> None:
        self.redactor.note_ssid(target.ssid)
        self.redactor.note_label(target.label)

    def preregister_connections(self, connections: list[tuple[str, str]]) -> None:
        """Saved NetworkManager connections, read before any file is written: a
        wifi connection's name is its SSID unless renamed; others are labels."""
        for name, kind in connections:
            if kind == "loopback":
                continue
            if kind == "802-11-wireless":
                self.redactor.note_ssid(name)
            else:
                self.redactor.note_label(name)

    # -- files -----------------------------------------------------------------

    def write_text(self, rel: str, raw: str) -> Path:
        """Write ``rel`` (scrubbed with the map as it stands) and keep the raw
        text so finalize() can rewrite it with the complete map."""
        with self._lock:
            self._raw[rel] = [raw]
            path = self.dir / rel
            path.write_text(self.redactor.scrub(raw), encoding="utf-8")
            return path

    def append_text(self, rel: str, raw: str) -> None:
        with self._lock:
            self._raw.setdefault(rel, []).append(raw)
            with open(self.dir / rel, "a", encoding="utf-8") as f:
                f.write(self.redactor.scrub(raw))

    def _next_command_path(self, label: str) -> str:
        with self._lock:
            self._command_index += 1
            slug = re.sub(r"[^a-zA-Z0-9]+", "-", self.redactor.scrub(label)).strip("-") or "command"
            return f"commands/{self._command_index:04d}-{slug}.txt"

    # -- external commands -----------------------------------------------------

    def record(self, result: probe.CommandResult, *, label: Optional[str] = None) -> Path:
        """Write one command's full detail to commands/NNNN-<label>.txt."""
        body = (
            f"argv: {' '.join(result.argv)}\n"
            f"returncode: {result.returncode}\n"
            f"duration_s: {result.duration_s:.3f}\n"
            f"timed_out: {result.timed_out}\n"
            f"error: {result.error or ''}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}\n"
        )
        with self._lock:
            return self.write_text(self._next_command_path(label or Path(result.argv[0]).name), body)

    def run_and_record(self, argv: list[str], *, label: str, timeout: float = 15.0) -> probe.CommandResult:
        result = probe.run_command(argv, timeout=timeout)
        self.record(result, label=label)
        return result

    def record_core_command(self, argv: list[str], *, privileged: bool, returncode: Optional[int],
                            duration_s: float, stdout_tail: list[str], stderr_tail: list[str],
                            note: str = "") -> Path:
        """One command the core spawned through its ProcRunner (see
        recording.py). stdout is whatever the core itself read, so it is empty
        for airodump-ng, whose output the core never reads (ADR-0008)."""
        words = [Path(argv[0]).name] + [a for a in argv[1:2] if not a.startswith("/")]
        body = (
            f"argv: {' '.join(argv)}\n"
            f"privileged: {privileged}{' (through sudo -n, ADR-0002)' if privileged else ''}\n"
            f"returncode: {returncode}\n"
            f"duration_s: {duration_s:.3f}\n"
            f"note: {note}\n"
            f"--- stdout (lines the core read, last {len(stdout_tail)}) ---\n" + "\n".join(stdout_tail) + "\n"
            "--- stderr (tail) ---\n" + "\n".join(stderr_tail) + "\n"
        )
        with self._lock:
            return self.write_text(self._next_command_path("core-" + "-".join(words)), body)

    def track_recorder(self, recorder) -> None:
        self._recorders.append(recorder)

    # -- snapshots & versions --------------------------------------------------

    def snapshot(self, phase: str, adapter: str) -> None:
        """Capture the read-only state set into snapshots/<phase>-*.txt."""
        for label, argv in probe.snapshot_commands(adapter):
            result = probe.run_command(argv, timeout=10.0)
            text = (
                f"$ {' '.join(argv)}\n(rc={result.returncode}, {result.duration_s:.2f}s)\n\n"
                f"{result.stdout}"
            )
            if result.stderr.strip():
                text += f"\n--- stderr ---\n{result.stderr}"
            self.write_text(f"snapshots/{phase}-{label}.txt", text)

    def write_versions(self, adapter: str) -> None:
        chunks = [f"python: {sys.version.splitlines()[0]}", f"platform: {platform.platform()}"]
        for label, argv in probe.version_commands(adapter):
            result = probe.run_command(argv, timeout=10.0)
            # First non-blank line once ANSI color codes are stripped (airodump-ng
            # --help opens with a bare color escape, for instance).
            lines = [_ANSI_RE.sub("", ln).strip() for ln in result.stdout.splitlines()]
            lines = [ln for ln in lines if ln]
            summary = lines[0] if lines else (result.error or f"(rc={result.returncode})")
            chunks.append(f"{label}: {summary}")
        self.write_text("versions.txt", "\n".join(chunks) + "\n")

    # -- event log and core log ------------------------------------------------

    def event_subscriber(self):
        """A callback for the Engine bus: appends one line per event to
        events.log. Never raises (the bus would log and skip it anyway)."""
        def _on_event(event) -> None:
            try:
                line = _format_event(event)
            except Exception as exc:  # never let formatting break the run
                line = f"<<event format error: {exc}>>"
            try:
                self.append_text("events.log", line + "\n")
            except Exception:
                pass
        return _on_event

    def attach_logging(self) -> None:
        logger = logging.getLogger("aircommand")
        self._log_handler = _ReportLogHandler(self)
        self._previous_log_level = logger.level
        logger.addHandler(self._log_handler)
        if logger.level == logging.NOTSET or logger.level > logging.INFO:
            logger.setLevel(logging.INFO)

    def detach_logging(self) -> None:
        if self._log_handler is not None:
            logger = logging.getLogger("aircommand")
            logger.removeHandler(self._log_handler)
            logger.setLevel(self._previous_log_level)
            self._log_handler = None

    # -- finalize --------------------------------------------------------------

    def finalize(self, meta: dict) -> None:
        """Write results.json and report.md, then rewrite EVERY file from its raw
        text with the final redaction map."""
        for recorder in self._recorders:
            recorder.flush_unfinished()
        with self._lock:
            for rel in ("events.log", "engine.log"):
                self._raw.setdefault(rel, [])
            for chunks in self._raw.values():
                self.redactor.harvest("".join(chunks))
            payload = {"meta": meta, "summary": self.counts(), "results": [r.as_dict() for r in self.results]}
            self._raw["results.json"] = [json.dumps(payload, indent=2, default=str, ensure_ascii=False) + "\n"]
            self._raw["report.md"] = [self._render_markdown(meta)]
            for rel, chunks in self._raw.items():
                (self.dir / rel).write_text(self.redactor.scrub("".join(chunks)), encoding="utf-8")

    def counts(self) -> dict:
        c = {s.value: 0 for s in Status}
        for r in self.results:
            c[r.status.value] += 1
        return c

    @property
    def has_failure(self) -> bool:
        return any(r.status == Status.FAIL for r in self.results)

    def _render_markdown(self, meta: dict) -> str:
        counts = self.counts()
        lines = [
            "# AirCommand real-hardware test report",
            "",
            f"- Generated: {meta.get('generated_at', '')}",
            f"- Adapter: `{meta.get('adapter', '')}`",
            f"- Host: {meta.get('host', '')}",
            f"- Harness args: `{meta.get('argv', '')}`",
            f"- App database: `{meta.get('db_path', '')}` and work directory `{meta.get('work_dir', '')}`. "
            + ("The radio tiers used these, the app's own: networks heard by Discovery are in that "
               "database, like any scan from the app. Neither is copied into this folder."
               if meta.get("app_data_used") else
               "Not opened: the radio tiers did not run."),
            f"- Redaction: {self._redaction_note()}",
            "",
            "## Summary",
            "",
            f"PASS {counts['PASS']} · FAIL {counts['FAIL']} · SKIP {counts['SKIP']} "
            f"· WARN {counts['WARN']} · INFO {counts['INFO']}",
            "",
        ]
        if meta.get("notes"):
            lines += ["### Run notes", ""]
            lines += [f"- {n}" for n in meta["notes"]]
            lines.append("")

        # Group results by tier, preserving first-seen tier order.
        tier_order: list[str] = []
        for r in self.results:
            if r.tier not in tier_order:
                tier_order.append(r.tier)
        for tier in tier_order:
            lines += [f"## {tier}", "", "| Check | Status | Detail |", "|---|---|---|"]
            for r in self.results:
                if r.tier != tier:
                    continue
                detail = r.detail.replace("\n", " ").replace("|", "\\|")
                lines.append(f"| {r.name} | {r.status.value} | {detail} |")
            lines.append("")

        lines += [
            "## Files in this report",
            "",
            "- `results.json`: machine-readable results",
            "- `commands/`: argv, exit code, stdout and stderr of every command. `NNNN-core-*` files are "
            "the commands the core spawned (airmon-ng, systemctl, airodump-ng, aircrack-ng, iw).",
            "- `snapshots/`: before/after `iw dev`, `ip link`, `ip addr`, `ip route`, `rfkill`, "
            "`nmcli device`, `nmcli radio`, `pgrep` for the three radio tools, and `iw dev <adapter> info`",
            "- `versions.txt`: kernel, python, tool and driver versions",
            "- `events.log`: the engine event stream during the run",
            "- `engine.log`: the core's own warnings and errors",
            "",
        ]
        return "\n".join(lines)

    def _redaction_note(self) -> str:
        if not self.redactor.enabled:
            return "off."
        s = self.redactor.summary()
        note = (f"on. Replaced {s['macs']} MAC address(es) (including wlx/enx interface names), "
                f"{s['ssids']} SSID(s) and saved wifi connection name(s), {s['labels']} label(s), "
                f"{s['ip_addresses']} IP address(es), the hostname, the username and the home path.")
        if s["kept_in_free_text"]:
            note += (f" {s['kept_in_free_text']} SSID(s) shorter than {MIN_FREE_TEXT_LENGTH} characters "
                     "or equal to a tool keyword were replaced only in `ssid=`/`label=` fields and "
                     "`iw` ssid lines, not in free text.")
        return note


def _username() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return ""


def _format_event(event) -> str:
    """One compact line per engine event for events.log. Includes a timestamp and
    the event's own fields (minus the noisy event_id)."""
    name = type(event).__name__
    fields = {}
    for key, value in vars(event).items():
        if key == "event_id":
            continue
        fields[key] = value
    stamp = fields.pop("occurred_at", datetime.now(timezone.utc))
    rendered = ", ".join(f"{k}={v!r}" for k, v in fields.items())
    return f"[{stamp}] {name}({rendered})"


def make_report_dir(reports_root: Path) -> Path:
    """<reports-root>/<UTC timestamp>/, created fresh and private (0700). A
    second run in the same second gets a "-2", "-3", ... suffix instead of
    failing."""
    reports_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for attempt in range(1, 100):
        path = reports_root / (stamp if attempt == 1 else f"{stamp}-{attempt}")
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            continue
        path.chmod(0o700)
        return path
    raise FileExistsError(f"could not create a fresh report folder under {reports_root}")
