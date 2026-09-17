# Core / GUI module boundary

Synthesized from two independently-designed candidates (`architect` Phase B, adapted — see [Synthesis decision](#synthesis-decision)). Decision recorded at `docs/adr/0003-core-architecture-event-driven.md`.

## Problem

AirCommand's core (discovery, capture, cracking, allowlist enforcement) must be GUI-agnostic and fully testable without root or a running GUI, while a CustomTkinter GUI needs live, streaming progress and the ability to cancel long-running subprocess-backed operations — all without blocking Tk's single-threaded mainloop. Four constraints make the shape non-obvious rather than a straightforward "wrap subprocess, return a result":

1. **Discovery and Capture are unbounded background operations** (run until stopped), not request/response calls — a plain function-call interface has no way to stream results back.
2. **Two orthogonal gates, not one.** *Privilege* (a cached sudo credential, ADR-0002) and *authorization* (the Target allowlist, ADR-0001) are independent. Discovery needs privilege but no authorization; Capture and gated Enumeration need both; Crack needs neither sudo nor a live allowlist check — it inherits authorization from the Handshake's provenance.
3. **The allowlist gate must be structurally unbypassable and every deauth firing individually audit-logged** — the gate and the audit trail can't be a check that's easy to forget at a new call site.
4. **One radio, contended.** A single wifi adapter in monitor mode can't run Discovery (channel-hopping) and Capture (channel-locked) at once; nmap enumeration needs the adapter in managed mode. RF-contending operations must serialize on the device — a constraint one candidate caught and the other missed entirely, so it's called out here explicitly.

Fixed inputs from the existing docs: core is a testable-without-GUI package (CLAUDE.md); SQLite for structured data + a working directory for `.cap`/wordlist artifacts; sudo primed once at launch with a 1–2 min keepalive, keepalive failure must surface clearly (ADR-0002); WEP/WPS/evil-twin-creation permanently out of scope (ADR-0001); reporting is v2, but v1 must persist results so a generator can be bolted on later without re-architecture.

## Usage (caller's view)

```python
# aircommand/gui/app.py
from aircommand.core import Engine, Target, Handshake
from aircommand.core.events import (
    NetworkDiscovered, NetworkSightingUpdated, TargetAdded,
    CaptureStarted, HandshakeCaptured, CaptureStopped, DeauthFired,
    CrackProgress, CrackResult, SudoKeepaliveFailed, SudoKeepaliveRecovered,
)

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.engine = Engine(db_path=Path("~/.aircommand/aircommand.db").expanduser(),
                              work_dir=Path("~/.aircommand/work").expanduser(),
                              adapter="wlan0")

        password = self._ask_sudo_password_dialog()
        self.engine.privilege.start(password)          # one prompt at launch, ADR-0002
        self.status_bar.set_privilege(self.engine.privilege.status)   # sync read, seeds the banner

        self.pump = GuiEventPump(self.engine, self)     # thread-safe-queue -> Tk mainloop bridge
        self.pump.on(NetworkDiscovered, self.networks_view.upsert_row)
        self.pump.on(NetworkSightingUpdated, self.networks_view.refresh_signal)
        self.pump.on(SudoKeepaliveFailed, self.status_bar.show_sudo_warning)
        self.pump.on(SudoKeepaliveRecovered, self.status_bar.clear_sudo_warning)
        self.pump.start()

        self.engine.discovery.start()                   # returns a JobHandle; runs until stopped

    def on_add_target_clicked(self, bssid_str: str, ssid: str, label: str):
        self.engine.targets.add(BSSID.parse(bssid_str), ssid, label)
        # TargetAdded arrives via the pump; the picker updates from that event, not this return value.
        # ssid comes from the Network row the user clicked (or a manual-entry field) —
        # Allowlist doesn't look it up itself, so a Target can be added without
        # requiring the bssid to already be a Discovered Network.

    def on_start_capture_clicked(self, target: Target, deauth: bool):
        try:
            handle = (self.engine.capture.start_deauth_assisted(target)
                      if deauth else self.engine.capture.start_passive(target))
        except AdapterBusy as e:
            self.status_bar.show_error(f"Radio busy: {e.holder} is using the adapter")
            return
        self.active_capture_job = handle
        self.pump.on(HandshakeCaptured, self._on_handshake, only_job=handle.job_id)
        self.pump.on(CaptureStopped, self._on_capture_stopped, only_job=handle.job_id)
        self.pump.on(DeauthFired, self.audit_view.append)   # every firing, live, per ADR-0001

    def on_cancel_capture_clicked(self):
        self.active_capture_job.cancel()                # returns immediately; CaptureStopped follows on the bus

    def on_start_crack_clicked(self, handshake: Handshake, wordlist_path: Path):
        handle = self.engine.crack.start(handshake, wordlist_path)   # no allowlist re-check — Handshake IS the proof
        self.pump.on(CrackProgress, self.crack_view.update_progress, only_job=handle.job_id)
        self.pump.on(CrackResult, self.crack_view.show_result, only_job=handle.job_id)

    def on_close(self):
        self.engine.shutdown()                          # stops keepalive, cancels live jobs, closes DB
```

### Headless call site (no GUI, no root, no hardware — proves the core is testable per CLAUDE.md)

```python
def test_discovery_persists_and_publishes_networks():
    engine = Engine(db_path=":memory:", work_dir=tmp_path, adapter="wlan0",
                     proc=FakeProcRunner(script=AIRODUMP_3_NETWORKS))
    seen: list[Network] = []
    engine.subscribe(lambda e: seen.append(e.network), NetworkDiscovered)

    handle = engine.discovery.start()
    handle.wait_for_test(timeout=2.0)          # test-only helper: block until N events or timeout

    assert {n.bssid for n in seen} == EXPECTED_BSSIDS
    assert {n.bssid for n in engine.discovery.list_networks()} == EXPECTED_BSSIDS
```

What the caller imports: `Engine` and the frozen domain/event types from `aircommand.core`. What it calls: five sub-facades hanging off `Engine`, plus `subscribe`/`cancel`/`shutdown`. What comes back: either a `JobHandle` (progress and completion arrive as events) or a small synchronous value (`Target`, `list[Network]`, `PrivilegeStatus`) for operations that are just reads/writes with no subprocess involved. The GUI never touches SQLite, never touches a subprocess handle, and never asks "what's the state now?" for anything long-running — it seeds a view once from a `list_*`/`status` call, then updates it from events.

## Shape

### Data structures first

Value objects (`core/domain.py`), validated at the boundary, trusted everywhere after — same discipline both candidates converged on independently:

```python
@dataclass(frozen=True)
class MacAddress:
    value: str  # canonical "AA:BB:CC:DD:EE:FF"
    @staticmethod
    def parse(raw: str) -> "MacAddress": raise NotImplementedError  # normalize, validate, raise ValueError

BSSID = MacAddress

class EncryptionType(Enum):
    OPEN = "open"; WEP = "wep"; WPA = "wpa"; WPA2 = "wpa2"; WPA3 = "wpa3"
    # WEP tracked for *display* only — no WEP cracking path exists, per ADR-0001.

@dataclass(frozen=True)
class Network:
    bssid: BSSID; ssid: str; channel: int; encryption: EncryptionType
    last_signal_dbm: int; first_seen: datetime; last_seen: datetime
```

`Target` and `Handshake` are **mint-restricted types** — the load-bearing move for both gate enforcement and provenance. Both candidates arrived at this independently; the synthesis keeps the more rigorously enforced version (a real `__post_init__` guard, not just a discouraging field name):

```python
_MINT = object()   # module-private sentinel; not exported from aircommand.core

@dataclass(frozen=True)
class Target:
    """A Network on the allowlist. Only Allowlist can construct one. Holding a Target
    is evidence it was authorized as of mint time; gated modules re-derive a fresh
    Target from Allowlist at the moment an Action actually starts (see Allowlist gate
    below) rather than trusting an old one blindly."""
    id: int; bssid: BSSID; ssid: str; label: str; date_added: datetime
    _proof: object = field(default=None, repr=False, compare=False)
    def __post_init__(self) -> None:
        if self._proof is not _MINT: raise TypeError("Target is only constructible by Allowlist")

@dataclass(frozen=True)
class Handshake:
    """Output of a successful gated Capture. target_id is stamped once, at capture time,
    from the Target the Capture module itself validated — never re-supplied by a caller.
    This is the provenance Crack trusts without a separate allowlist check."""
    id: int; target_id: int; bssid: BSSID; capture_job_id: "JobId"
    cap_file_path: Path; cap_file_sha256: str; captured_at: datetime
    _proof: object = field(default=None, repr=False, compare=False)
    def __post_init__(self) -> None:
        if self._proof is not _MINT: raise TypeError("Handshake is only constructible by Capture")
```

*Named honestly*: Python has no true nominal sealing; `_MINT` is convention-strength (stops accidental misuse — e.g. a future contributor hand-building a `Target` to unblock a call), not a boundary against deliberately hostile code in the same process. Acceptable for an in-process desktop app.

```python
class StopReason(Enum):
    COMPLETED = "completed"; CANCELLED = "cancelled"; ERROR = "error"
    INTERRUPTED_PRIOR_SESSION = "interrupted_prior_session"   # startup reconciliation, see Open questions

class JobKind(Enum):
    DISCOVERY = "discovery"; CAPTURE_PASSIVE = "capture_passive"; CAPTURE_DEAUTH = "capture_deauth"
    NMAP_SCAN = "nmap_scan"; CRACK = "crack"

JobId = NewType("JobId", uuid.UUID)

@dataclass(frozen=True)
class AuditLogEntry:   # every deauth firing, append-only, independent of capture outcome
    id: int; target_id: int; capture_job_id: JobId
    client_mac: Optional[MacAddress]; fired_at: datetime; frame_count: int

@dataclass(frozen=True)
class CrackResultRow:
    id: int; handshake_id: int   # -> handshakes.target_id -> targets.id: full provenance chain
    found_key: Optional[str]; wordlist_path: Path
    started_at: datetime; finished_at: Optional[datetime]; stop_reason: StopReason
```

**Dominant access patterns traced through this structure** (per runner-prompt discipline — no "add an index later"): the network table is seeded once via `discovery.list_networks()`, then upserted in place from `NetworkDiscovered`/`NetworkSightingUpdated` events, which each carry the *full* `Network` snapshot — the event payload doubles as the cache update, no separate cache layer. Same pattern for the target picker (`targets.list()` + `TargetAdded`/`TargetRemoved`) and the handshake picker (`capture.list_handshakes()` + `HandshakeCaptured`). A crack progress bar filters the event stream by the `job_id` already on the `JobHandle` it holds — no lookup needed. The audit log review screen is a plain one-shot `capture.list_audit_log()` query, not event-driven — "no polling" applies to state a view must track continuously, not to populating a dialog the user just opened.

### Event bus

```python
@dataclass(frozen=True)
class Event:
    event_id: uuid.UUID
    occurred_at: datetime

class DurableEvent(Event):
    """Published only after the state transition it describes has already been
    committed to SQLite in the same call. An observer that sees a DurableEvent can
    trust the fact happened and is safe to query back (e.g. after HandshakeCaptured,
    engine.capture.list_handshakes() will include it)."""

class TelemetryEvent(Event):
    """High-frequency, best-effort. Durability is eventually-consistent (batched,
    on the order of a few seconds) — never used to prove authorization, provenance,
    or an audit fact. NetworkSightingUpdated and CrackProgress live here."""

class Subscription:
    def unsubscribe(self) -> None: raise NotImplementedError

class EventBus:
    """Dispatch is synchronous on the publisher's thread: publish() does not return
    until every subscriber has been called, in subscription order — this is what
    guarantees an event is never observed before the write it describes has already
    committed, since publish() is only ever invoked by the function that just wrote.
    A subscriber that raises is caught, logged, and skipped: one bad subscriber must
    never stop persistence or any other subscriber from seeing the event. Subscribers
    must be fast (near-O(1)); anything that needs real work offloads to its own
    thread from inside the callback and only does an in-memory append inline."""
    def subscribe(self, callback: Callable[[Event], None], event_type: type | None = None) -> Subscription:
        raise NotImplementedError
    def publish(self, event: Event) -> None:
        raise NotImplementedError
        # TODO: snapshot subscriber list under a short lock, invoke callbacks outside
        # the lock (so a slow subscriber can't block subscribe() from other threads);
        # wrap each callback in try/except, log on failure.
```

### SQLite and the event stream — hybrid, by risk tier

Two tiers, matching `DurableEvent`/`TelemetryEvent` above:

1. **Correctness-critical transitions write synchronously, then publish** a `DurableEvent`: `NetworkDiscovered` (first sighting), `TargetAdded`/`TargetRemoved`, `CaptureStarted`, `DeauthFired`, `HandshakeCaptured`, `NmapScanCompleted`, `CrackStarted`, `CrackResult`. Each is produced by a function that performs one SQLite write in one transaction, then calls `bus.publish(...)` with the value that write produced. This matters concretely for `DeauthFired` (the write happens before the event exists, so no subscriber can ever observe a firing that failed to log) and `HandshakeCaptured` (no window where the GUI shows a handshake the DB doesn't have).
2. **High-frequency/low-stakes telemetry publishes a `TelemetryEvent` without a synchronous write per occurrence.** `NetworkSightingUpdated` (signal refresh on an already-known beacon) and `CrackProgress` (hashrate/ETA ticks) are handled by `SightingBatcher` (`core/persistence/sighting_batch.py`), a genuine bus subscriber whose callback just appends to an in-memory deque (fast, keeps `publish()` non-blocking); a separate timer thread flushes accumulated last-known-values every few seconds in one batched `UPDATE`. A crash loses at most a few seconds of freshness — identity and terminal results are tier 1, never at risk.

Each job-driver thread owns its own `sqlite3` connection (WAL mode, short `busy_timeout`) and writes only its own tables — per-actor state with SQLite as the merge point at the read boundary, not one shared connection guarded by an app-level lock.

### Allowlist gate — structural, not a scattered `if`

`Allowlist` (`core/allowlist.py`) is the only module that can mint a `Target`:

```python
class Allowlist:
    def add(self, bssid: BSSID, ssid: str, label: str) -> Target: raise NotImplementedError  # idempotent upsert
    def remove(self, bssid: BSSID) -> None: raise NotImplementedError                  # idempotent if absent
    def list(self) -> list[Target]: raise NotImplementedError
    def require_target(self, bssid: BSSID) -> Target:
        raise NotImplementedError
        # TODO: SELECT by bssid; raise NotATargetError(bssid) if absent; else return the
        # (already-minted, by TargetRepository) Target row
```

Every gated entry point takes a `Target` object, never a bare BSSID/str: `Capture.start_passive(target, ...)`, `Capture.start_deauth_assisted(target, ...)`, `Enumerator.start_scan(target, ...)`. Because `Target.__post_init__` rejects construction without `Allowlist`'s private sentinel, there is no code path into a gated method that didn't originate from `Allowlist` — that answers "before any Action fires" structurally, not via a checklist of call sites to remember.

Freshness: a caller can hold a `Target` fetched minutes ago; the allowlist could have changed since. `Capture`/`Enumerator` internally call `require_target(target.bssid)` again the moment the Action actually starts, and use *that* fresh result for the DB write and the `Handshake`/`AuditLogEntry` produced. SQLite's `targets` table stays the single source of truth for "is this currently a Target"; the type discipline is what makes bypass structurally hard, not what's trusted for freshness. (Whether a Target removed *during* an already-running Capture should interrupt it is open — see Open questions.)

### RF / single-radio reservation — grafted, missing from one candidate entirely

One wifi adapter cannot be simultaneously channel-hopping (Discovery), channel-locked (Capture), and in managed mode (Enumeration). `core/rf.py` owns a small reservation state machine:

```python
class AdapterMode(Enum): MONITOR_HOPPING = "monitor_hopping"; MONITOR_LOCKED = "monitor_locked"; MANAGED = "managed"

class AdapterBusy(Exception):
    def __init__(self, requested: AdapterMode, holder: JobKind): ...

@dataclass(frozen=True)
class AdapterReservation:
    """Carries the adapter name too, not just mode/holder: RadioController is the
    only thing that knows the interface string (Discovery/Capture/Enumerator never
    take one directly), and each driver's _drive() needs it to build argv for the
    tool it spawns. Added during the headless-Discovery-flow slice, after the
    original sketch below turned out to have no way for a driver to learn its
    interface name at all."""
    mode: AdapterMode
    holder: JobKind
    adapter: str

class RadioController:
    """Single writer for adapter mode. reserve() is synchronous and either succeeds
    immediately (mode switch via airmon-ng, blocking, ~1s) or raises AdapterBusy —
    it never queues. release() is called by the job-driver thread on any StopReason.
    As of the headless-Discovery-flow slice, reserve()/release() only do the
    in-memory arbitration below — the real airmon-ng mode switch is still deferred
    until SubprocessRunner exists (see 'Next implementation step')."""
    def reserve(self, mode: AdapterMode, holder: JobKind) -> AdapterReservation: raise NotImplementedError
    def release(self, reservation: AdapterReservation) -> None: raise NotImplementedError
```

`discovery.start()`, `capture.start_passive()`/`start_deauth_assisted()`, and `enumerate.start_scan()` each call `reserve()` before spawning anything; `AdapterBusy` is raised synchronously to the caller (the job never starts, no event fires) rather than surfacing as a job failure — the GUI call site above catches it directly, per the Usage section.

### Handshake provenance for Crack — no separate check

`Crack.start(handshake: Handshake, wordlist_path: Path)` takes a `Handshake` value, not a path or BSSID string. Since `Handshake` is mint-restricted to `Capture`, and `Capture` only produces one after a fresh `require_target` succeeded, `target_id` on the `Handshake` is proof Crack can trust without re-querying `Allowlist` — matching CONTEXT.md exactly. The SQLite FK `handshakes.target_id REFERENCES targets.id` is a referential-integrity constraint, a different concern from the authorization check itself, worth naming so it isn't mistaken for a second, redundant gate.

### Cancellation — a command in, an event out

```python
class CancellationToken:
    def cancel(self) -> None: raise NotImplementedError        # sets an internal threading.Event
    def is_cancelled(self) -> bool: raise NotImplementedError

@dataclass(frozen=True)
class JobHandle:
    job_id: JobId; kind: JobKind
    def cancel(self) -> None: raise NotImplementedError         # -> engine.cancel(job_id); returns immediately
```

`engine.cancel(job_id)` looks up the job's `CancellationToken`, signals it, sends the driving subprocess a termination signal (SIGTERM, escalating to SIGKILL after a grace period — aircrack-ng-suite tools don't always honor SIGINT cleanly), and returns without waiting for cleanup. The job-driver thread observes the token, performs cleanup (drain remaining output, mark the job row terminal, release its RF reservation), and *that* thread publishes the terminal `DurableEvent` (`CaptureStopped(reason=CANCELLED)`, etc.). The request to cancel is a command with an immediate return; the fact that cancellation completed is an event, observed identically to any other state transition. `cancel()` on an already-terminal or unknown job is a no-op.

### Startup reconciliation — orphaned processes from a prior crash

Decided in ADR-0004: yes, AirCommand cleans these up automatically, not just marks their DB rows stale. Every job-driver thread calls `jobs.record_process(job_id, pid, pgid, fingerprint)` right after `proc.spawn()` succeeds. `Engine.reconcile_startup()` — called by the GUI right after `privilege.start()` succeeds, **not** during `Engine.__init__`, since terminating a root-owned orphan needs that same privilege — finds job rows still `RUNNING` from a prior process (`JobRegistry.find_stale_jobs()`), verifies via `/proc/<pid>/cmdline` that whatever still holds the recorded PGID actually matches the recorded fingerprint (a PGID can be reused by an unrelated process after a reboot), and if so sends SIGTERM, escalating to SIGKILL after a grace period, before marking the row `StopReason.INTERRUPTED_PRIOR_SESSION`. See `core/reconciliation.py` and `core/procutil.py`.

This closes exposure going forward; it cannot retroactively account for what an orphaned deauth loop fired between the crash and the cleanup, since the process that would have written those audit rows is exactly what crashed. Treat `CaptureStopped(reason=INTERRUPTED_PRIOR_SESSION)` on a deauth-assisted job as "an unknown, unlogged number of deauth bursts may have fired here" wherever it's surfaced (GUI, audit review).

### Privilege — one choke point, pushed events plus a synchronous read

`core/privilege.py`'s `SudoSession` is the only thing that launches a privileged subprocess (airodump-ng, aireplay-ng, raw-socket nmap scans):

```python
class PrivilegeStatus(Enum): UNSET = "unset"; ACTIVE = "active"; LOST = "lost"

class SudoSession:
    status: PrivilegeStatus   # synchronous read — seeds a freshly-opened screen; kept current by the events below
    def start(self, password: str) -> None: raise NotImplementedError
    # TODO: `sudo -k` then `sudo -v` once via stdin; raise InvalidSudoPasswordError on
    # failure; on success spawn the keepalive thread and publish SudoSessionStarted.
    def run_privileged(self, argv: list[str]) -> subprocess.Popen: raise NotImplementedError
    # `sudo -n <argv>` — -n so a lapsed cache fails fast instead of hanging on a prompt with no tty.
    def stop(self) -> None: raise NotImplementedError   # stops keepalive; called from Engine.shutdown
```

The keepalive thread runs `sudo -v` every 60–90s; on failure it publishes `SudoKeepaliveFailed(consecutive_failures)` (a `DurableEvent` — this is precisely what ADR-0002's "surface clearly, not silently" requirement is for) and `SudoKeepaliveRecovered()` once a later attempt succeeds; both update `.status` as they fire. Individual jobs are not proactively cancelled when keepalive fails (see Open questions) — the job's next privileged call simply fails and surfaces its own `StopReason.ERROR`, one failure-reporting path instead of two.

### Testability — the seam that makes "no GUI, no root" true

```python
class ProcRunner(Protocol):
    """The only way core touches a subprocess. Production: SubprocessRunner (real
    Popen, routes privileged argv through SudoSession). Tests: FakeProcRunner
    replays a scripted line stream with no process, no sudo, no adapter."""
    def spawn(self, argv: list[str], *, privileged: bool) -> "ProcHandle": raise NotImplementedError

class Engine:
    def __init__(self, db_path: Path, work_dir: Path, adapter: str,
                 proc: ProcRunner | None = None) -> None: ...   # proc defaults to SubprocessRunner
```

Every runner (`discovery.py`, `capture.py`, `crack.py`, `enumerate.py`) takes its `ProcRunner` via `Engine`, never constructs one itself — this is what makes the headless call site in [Usage](#usage-callers-view) real rather than aspirational. Output parsing (`airodump` CSV → `Network`/progress, `hashcat --status` → `CrackProgress`, `nmap -oX` → hosts) lives in `core/parse.py` as pure functions with no I/O, independently unit-testable against captured tool output.

### Module map

```
aircommand/core/
  __init__.py       # exports Engine + domain/event types — the GUI's entire import surface
  engine.py          # Engine: composition root. .targets/.discovery/.capture/.enumerate/.crack,
                     #  subscribe()/cancel()/shutdown()/reconcile_stale_jobs(). No domain logic of its own.
  domain.py           # Network, Target, Handshake, CrackResultRow, AuditLogEntry, value objects, enums
  events.py            # Event, DurableEvent, TelemetryEvent, concrete events, EventBus, Subscription
  allowlist.py          # Allowlist — the only Target mint site
  rf.py                  # RadioController, AdapterMode, AdapterBusy — single-radio serialization
  discovery.py            # Discovery — wraps airodump-ng discovery mode
  capture.py               # Capture — wraps airodump-ng (+aireplay-ng); the only Handshake mint site
  enumerate.py              # Enumerator — wraps nmap, gated
  crack.py                   # Crack — wraps hashcat, trusts Handshake.target_id
  privilege.py                # SudoSession — priming, keepalive, run_privileged() choke point
  jobs.py                      # JobRegistry, CancellationToken, JobHandle plumbing shared by all drivers
  procutil.py                   # ProcRunner protocol, SubprocessRunner, FakeProcRunner — the test seam
  parse.py                       # PURE parsers: airodump/hashcat/nmap output -> domain types. No I/O.
  reconciliation.py                # reconcile_orphaned_processes() — startup orphan cleanup, ADR-0004
  persistence/
    db.py                         # schema, connection-per-thread helpers, one repository per aggregate
    sighting_batch.py              # SightingBatcher — the tier-2 async DB subscriber
aircommand/gui/
  app.py                            # owns the single Engine instance
  event_pump.py                      # GuiEventPump — thread-safe-queue-to-Tk-mainloop bridge
```

Tracing "GUI clicks start capture" end to end: `App` → `engine.capture.start_passive` → (`rf.reserve`, `allowlist.require_target`, `jobs.new_job` + `privilege.run_privileged`) → DB write + `bus.publish`. Three hops past the GUI, all within `core/`.

### Interface depth

Public surface: `Engine` plus five sub-facades of 2–4 methods each, plus `subscribe`/`cancel`/`shutdown` — roughly 15 methods to learn to drive four wrapped tools, enforce an allowlist, serialize one radio, and get live progress. Hidden behind it: subprocess supervision and signal handling, SQLite schema/transactions/WAL, the sudo keepalive, cancellation-token plumbing, RF-mode arbitration, and the mint-restriction that makes the gate unbypassable. A caller never sees a `Popen`, a SQL statement, a wire/CSV parsing detail, or a thread. What's deliberately still exposed: the typed result shapes (`Network`/`Target`/`Handshake`/`CrackResultRow`) and the fact that operations are asynchronous (`JobHandle` + events) — the GUI's whole job is rendering exactly that shape and that asynchrony; hiding it would just move the problem into a vaguer method.

What core deliberately does not do: render or format anything for display; manage wordlists (v1 takes a user-supplied path); implement WEP/WPS/evil-twin (permanently out, ADR-0001); auto-retry a failed subprocess launch (surfaces as `StopReason.ERROR`); build the v2 report generator — but every result is stored with the relational keys and timestamps a report generator would need, queryable read-only without touching `Engine`.

## Synthesis decision

Base: the event-driven candidate. Its multi-subscriber model is the better fit for a GUI with several panels (network table, target picker, audit log, crack progress) all wanting to react to the same underlying facts independently — the polling candidate's `Job.poll()` drains its own buffer per call, so two independent readers of one job would silently split a shared feed rather than each seeing everything. That gap, not a stylistic preference, is why the poll/facade shape lost as the primary interface.

Grafted from the polling candidate:
- **RF/single-radio reservation** (`core/rf.py`, `AdapterMode`, `AdapterBusy`) — a real, load-bearing domain constraint the event-driven candidate missed entirely. Added as its own module rather than folded into `discovery`/`capture`, since three different modules (Discovery, Capture, Enumerate) all need to arbitrate against it.
- **Explicit testability seam** (`ProcRunner`/`FakeProcRunner`, the headless call site) — the event-driven candidate implied testability but never demonstrated it as concretely as the polling candidate's injected-`proc` constructor and headless test. Required outright now, not left implicit, since "core is testable without a GUI" is a fixed constraint, not a nice-to-have.
- **`core/parse.py`** as pure, I/O-free parsing functions — the polling candidate separated this cleanly; folded in as its own module for the same reason.
- **A synchronous `SudoSession.status` read** alongside the pushed privilege events — the polling candidate's `privilege_status()` property flagged a real gap (a freshly opened screen has nothing to render until the next event); resolved inside the event-driven shape by seeding from a sync read, same pattern as Networks/Targets/Handshakes.

Formalized (not strictly "from" either candidate, but converges both): the polling candidate's explicit split between coalescible `progress` and must-not-miss `events` within one job's stream is, in effect, the same insight as the event-driven candidate's sync-write/async-batch persistence tiering, just expressed at different layers. The synthesis makes this one concept — `DurableEvent` vs `TelemetryEvent` as actual base classes — so any future subscriber can `isinstance()`-check durability instead of relying on a side list of which event names are which tier.

Rejected: the polling candidate's single generic `Job[R]` + `poll()`/`subscribe()` as the GUI's *primary* read path (kept internally as `JobHandle`/`JobRegistry` bookkeeping, not exposed as the way to observe results). The event-driven candidate's implicit "testability follows from clean composition" assumption was tightened into an explicit, required seam rather than trusted to fall out naturally.

## Tradeoffs accepted

- We accept that a hard crash between "subprocess launched" and "job row committed" can leave a subprocess orphaned and untracked, in exchange for keeping each state write a single synchronous local SQLite transaction instead of a two-phase commit across the process/subprocess boundary.
- We accept that `NetworkSightingUpdated` and `CrackProgress` are lossy across a crash (at most a few seconds of staleness on reload), in exchange for not hitting SQLite on every beacon frame or hashcat tick.
- We accept a convention-strength (`_MINT` sentinel), not cryptographically sealed, constructor restriction on `Target`/`Handshake` — acceptable for an in-process desktop app, not acceptable if this code ever became a library boundary between mutually distrusting components.
- We accept every subscriber running synchronously on the publisher's thread (subscribers must stay fast) in exchange for the ordering guarantee that an event is never observed before its write is durable.
- We accept serialized RF operations on one adapter (Discovery and Capture cannot run concurrently; `AdapterBusy` is raised) in exchange for never corrupting device state — no multi-adapter scheduler in v1.
- We accept that "is this Network currently a Target" is re-checked only at the moment an Action starts, not continuously through a long-running Capture, in exchange for not adding revalidation overhead to a session that might run for hours (flagged below).
- We accept that crack authorization is provenance-based and historical, not a live allowlist check — revoking a Target does not retroactively forbid cracking a handshake already captured under authorization. This is the domain rule (CONTEXT.md: "cracking is not separately gated"), stated here so it doesn't read as a missing check.

## Alternatives considered

- **The polling-facade candidate in full** (synchronous `AirCommand.start_*()` returning a pollable `Job[R]`). Lost primarily on multi-consumer fit — see Synthesis decision — despite real strengths (testability clarity, the progress/events split) that were grafted in rather than lost.
- **Explicit callback parameters per operation** (`start_discovery(on_network_found=..., on_error=...)`) instead of a shared bus. Every long-running method would grow a callback parameter per event type it can produce, and adding a second listener (persistence) means threading a list-of-callbacks through every call site or duplicating wrapped-tool logic. The bus reduces this to one `subscribe()` surface reused by every listener, present or future.
- **SQLite as the only integration point** (GUI polls the DB directly or via a file-watcher, no in-process bus). SQLite has no LISTEN/NOTIFY equivalent, so this just relocates polling from "poll an object" to "poll a file" while leaking table shapes into the GUI layer.
- **asyncio as core's concurrency model.** CustomTkinter's mainloop has no first-class asyncio integration, so bridging asyncio↔Tk needs the same queue/pump pattern as the thread-based design anyway — a second concurrency model on top of the threads subprocess I/O already needs, for no net simplification.

## Decided since the first synthesis

- **Target removed mid-Capture:** the Capture runs to completion (or user-cancel) rather than being interrupted. This was already the design's default (re-check only at Action *start* — see Tradeoffs) and is now confirmed, not just assumed.
- **Orphaned processes from a crash:** cleaned up automatically at next launch, not left running. See ADR-0004 and 'Startup reconciliation' above.

## Open questions and risks

- Is `sha256` of the completed `.cap` file the right handshake dedup key, or do aircrack-ng's "handshake found" heuristic and hashcat's actual input requirement (full 4-way vs. message pairs sufficient for PMKID-style attacks) disagree on what counts as "a Handshake"? Affects exactly when `HandshakeCaptured` should fire.
- Do we mint a `Handshake` for WPA3/SAE in v1 (and let Crack report `Unsupported`, since SAE isn't dictionary-crackable the way WPA2 EAPOL is), or refuse the capture up front?
- Is a proactive job-cancel on sustained `SudoKeepaliveFailed` worth the complexity, or is "the next privileged call fails naturally" (current design) acceptable — a job could sit idle for up to ~90s before its next privileged call reveals the problem?
- Is the ~50ms GUI drain tick the right value, or does it need tuning once real CustomTkinter widget-update cost is measured?
- Is one adapter (serialize-and-raise-`AdapterBusy`) acceptable for v1, or should a second adapter be supported so Discovery and Capture can overlap?

## Next implementation step

Done: `core/domain.py` and `core/events.py` (with the `EventBus` unit test suite), and the headless `Discovery` flow end to end (`FakeProcRunner` → parse → `NetworkDiscovered` → SQLite), per the Usage section's headless call site — including the full SQLite schema (`persistence/db.py`), which this design doc left as a TODO and which turned out to need `JobRepository` alongside `JobRegistry` so ADR-0004's orphan detection has somewhere durable to read from. Still no real subprocess or sudo code exists anywhere in the tree.

Next: `core/allowlist.py` — small (four methods: `add`/`remove`/`list`/`require_target`) and fully headless-testable like the last two slices, but it's the shared prerequisite both remaining gated facades need (`Capture`/`Enumerator` both call `require_target` at Action-start, per 'Allowlist gate' above). Do it before either of them rather than letting one implement a throwaway stand-in.

After that: `core/capture.py` — the next real milestone, not another small slice. It's the only `Handshake` mint site, drives two tools (`airodump-ng` + `aireplay-ng`) instead of Discovery's one, and is where ADR-0001's audit requirement actually bites (`DeauthFired` must be logged before the event fires, per 'SQLite and the event stream' above) — worth planning its own headless test fixtures (scripted deauth-then-handshake CSV output) before dispatching implementation, the same way the Discovery slice's CSV field-layout contract was pinned up front rather than left to whoever implemented it. `core/crack.py` naturally follows once `Capture` can produce a real `Handshake` to feed it. `core/enumerate.py` only depends on `Allowlist`, so it can happen in either order relative to `Capture`/`Crack`. `core/privilege.py` (real `sudo` invocation) and `core/reconciliation.py` stay last — both need genuine subprocess/sudo code, which every prior slice has deliberately deferred, and reconciliation specifically needs `Capture` to exist first for its "unlogged deauth bursts" scenario to mean anything.
