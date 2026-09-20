# GUI internal structure

Scoped per `docs/roadmap.md` Phase 3: what views exist, how they're laid out, how
each subscribes to `GuiEventPump`, and what each seeds itself from at startup.
`docs/design/core-gui-boundary.md` (the core/GUI boundary) is **not** redesigned
here — every decision below is grounded against it as fixed input, the same way
that doc treated ADR-0001/0002 as fixed input. `gui/event_pump.py`'s
`GuiEventPump.on`/`_tick` are also fixed input (already precisely pinned as a
mechanical TODO) — not touched by this pass, only used as specified.

## Problem

`aircommand/gui/app.py` is a 49-line stub that constructs `Engine` and
`GuiEventPump`, then raises `NotImplementedError`. No widget exists — not the
networks table, target picker, capture panel, enumerate panel, audit log view,
crack panel, status bar, or sudo dialog, not even as stub files. This doc fixes
that: it specifies every view, its constructor's seed data, and its exact event
subscriptions, precisely enough that each view can be dispatched as a normal
mechanical implementation slice per CLAUDE.md's model tiering — the same
process Phase 1/2's core slices already went through.

Three real gaps surfaced while grounding this design against the actual shipped
code (not just the design doc's aspirational sketch) — each is specified exactly
and flagged separately in **Gaps found & required core-side fixes**, since none
of them are GUI files and this pass doesn't implement anything.

## Layout

One `ctk.CTk` root window: a `StatusBar` docked at the bottom (always visible,
independent of tab), and a `ctk.CTkTabview` above it with four tabs:

1. **Discovery & Targets** — `NetworksView` + `TargetPicker`
2. **Target Actions** — `TargetSelector` + `CapturePanel` + `EnumeratePanel`
3. **Crack** — `HandshakePicker` + `WordlistPicker` + progress/result display
4. **Audit Log** — `AuditLogView`

`docs/roadmap.md`'s Phase 3 paragraph names "networks table, target picker,
capture panel, audit log view, crack progress + wordlist picker" but doesn't
name Enumerate. Included it anyway: `Engine` fully wires `self.enumerate`, and
CONTEXT.md's own "Action" entry lists Enumerate as a first-class gated Action
alongside Capture — leaving it out would ship a GUI that can't drive a
fully-implemented core facade. Flagging this explicitly since it's a call I made,
not something pre-decided.

**Why Capture and Enumerate share one tab, not two.** Both gate on the same
`Target`, and both contend for the same single radio via `RadioController`
(`AdapterBusy`) — Discovery, Capture, and Enumerate can never run concurrently.
Putting Capture and Enumerate side by side under one `TargetSelector` makes that
contention visible (you can see both panels at once, and only one control set
will ever be doing something) rather than splitting them across tabs where the
radio conflict would just look like an unexplained error.

## Startup sequencing (`App.__init__`)

```python
class App(ctk.CTk):
    def __init__(self, db_path: Path, work_dir: Path, adapter: str) -> None:
        super().__init__()
        self.title("AirCommand")

        self.engine = Engine(db_path=db_path, work_dir=work_dir, adapter=adapter)

        password = self._ask_sudo_password_dialog()
        while True:
            try:
                self.engine.privilege.start(password)
                break
            except InvalidSudoPasswordError:
                password = self._ask_sudo_password_dialog(error="Incorrect password — try again")
                if password is None:      # dialog cancelled/closed
                    self.destroy()
                    raise SystemExit(0)   # privilege is required before ANY Action,
                                          # including Discovery — no degraded mode

        reconciliation = self.engine.reconcile_startup()   # sync return, see below —
        # nothing is subscribed to the pump yet, matching the existing comment in
        # the current stub.
        targets_by_id = {t.id: t for t in self.engine.targets.list()}
        self._interrupted_deauth_targets = [
            targets_by_id[tid] for tid in reconciliation.interrupted_deauth_target_ids
            if tid in targets_by_id
        ]  # tid may be absent if the Target was removed from the allowlist between
           # the crash and this launch — silently excluded, nothing left to flag it against.

        self.pump = GuiEventPump(self.engine, self)

        self.status_bar = StatusBar(self, self.engine.privilege.status,
                                     reconciliation, self._interrupted_deauth_targets)
        self.status_bar.pack(side="bottom", fill="x")
        self.pump.on(SudoKeepaliveFailed, self.status_bar.show_sudo_warning)
        self.pump.on(SudoKeepaliveRecovered, self.status_bar.clear_sudo_warning)

        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(fill="both", expand=True)
        self._build_discovery_targets_tab()
        self._build_target_actions_tab()
        self._build_crack_tab()
        self._build_audit_log_tab()   # gets self._interrupted_deauth_targets

        self.protocol("WM_DELETE_WINDOW", self.on_close)   # NOT wired in the
        # current stub — on_close() already exists but nothing calls it.

        self.pump.start()
        self._discovery_handle = self.engine.discovery.start()   # auto-starts;
        # see "Target Actions tab" below for how the user frees the radio.

    def _ask_sudo_password_dialog(self, error: Optional[str] = None) -> Optional[str]:
        raise NotImplementedError  # SudoPasswordDialog(self, error=error).result

    def on_close(self) -> None:
        self.engine.shutdown()
        self.destroy()   # missing from the current stub too — shutdown() alone
                          # stops the engine but doesn't close the Tk window
```

Every view is constructed and seeded **before** `pump.start()`, and every
`.on(...)` registration happens before `pump.start()` too — but this ordering is
convenience, not correctness. `GuiEventPump.__init__` already calls
`engine.subscribe(self._q.put)` at construction time (not at `.start()`), and
`queue.Queue.put` never blocks or drops, so any event published between pump
construction and `pump.start()` is queued, not lost, regardless of exactly when
each view registers its handler relative to `discovery.start()`.

## Sudo password dialog

`SudoPasswordDialog(ctk.CTkToplevel)`: a modal built with the standard
Tk pattern — `grab_set()` then the parent calls `wait_window(dialog)`, which
blocks *only* the call site (still on the mainloop thread; nothing else runs
until the dialog closes, which is fine, this is launch-time, before anything
else is live). One masked `CTkEntry(show="*")`, an optional error label (set
from the `error` param on retry), OK and Cancel buttons. OK sets
`self.result = entry.get()` and destroys itself; Cancel/window-close sets
`self.result = None`. `App` reads `.result` after `wait_window` returns.

Cancelling exits the app outright (see startup sequencing above) — there's no
partial/no-privilege mode, since even Discovery needs a raw-socket monitor-mode
adapter (ADR-0002; `discovery.py`'s `_drive` spawns `airodump-ng` with
`privileged=True`).

## Discovery & Targets tab

**`NetworksView`** — seeds from `engine.discovery.list_networks()` **before**
`discovery.start()` is called (per startup sequencing above — this is why the
view is constructed ahead of that last line, not after). This shows
previously-known networks (persisted across sessions in SQLite) immediately,
rather than an empty table until the first ~2s CSV poll cycle completes.
Subscribes, both unfiltered (no `job_id` on either event, so no `only_job`
possible or needed):

```python
self.pump.on(NetworkDiscovered, self.networks_view.upsert_row)
self.pump.on(NetworkSightingUpdated, self.networks_view.upsert_row)
```

One row per BSSID (columns: SSID, BSSID, channel, encryption, signal, last
seen), keyed by `network.bssid` — both event types carry the full `Network`
snapshot, so `upsert_row` is one method for both (per the core doc's own "event
payload doubles as the cache update" pattern). Each row has an **"Add as
Target"** button that opens a small inline form pre-filled with
`bssid`/`ssid`/`channel` from that row, asking only for a `label`, then calls
`engine.targets.add(...)`.

**`TargetPicker`** — seeds from `engine.targets.list()`. Subscribes (global, no
job filter — `TargetAdded`/`TargetRemoved` carry no `job_id`):

```python
self.pump.on(TargetAdded, self.target_picker.upsert_row)
self.pump.on(TargetRemoved, self.target_picker.remove_row)
```

Also has its own **"Add manually"** form (bssid/ssid/channel/label fields), for
a Target not currently visible in `NetworksView` — e.g. temporarily out of
range. This isn't a workaround; `Allowlist.add`'s own docstring says exactly
this is intentional ("doesn't force 'must already be Discovered' as a
precondition"), and the manual form is what actually exposes that. Each row has
a **"Remove"** button calling `engine.targets.remove(bssid)`.

## Target Actions tab

**`TargetSelector`** (reusable — also used by Audit Log's filter, below): a
dropdown seeded from `engine.targets.list()`, subscribing to the same
`TargetAdded`/`TargetRemoved` pair as `TargetPicker`. Selecting a Target drives
both `CapturePanel` and `EnumeratePanel` below it.

**Discovery vs. Capture/Enumerate — the radio-contention problem.** Discovery
auto-starts at launch and runs until stopped (per the core doc's own Usage
sketch, which this design doesn't change). Left running unconditionally,
Discovery holds `MONITOR_HOPPING` forever, so `Capture.start_*`/
`Enumerator.start_scan`'s own `rf.reserve()` call would raise `AdapterBusy`
every single time — the moment this tab exists, it'd be permanently unusable
unless something frees the radio. This isn't hypothetical; it falls directly
out of `rf.py`'s `reserve()` never queuing, combined with Discovery having no
natural stopping point. **Decided:** add a manual **Pause/Resume Discovery**
button to the Discovery & Targets tab (not this one, since it's Discovery's own
control) — Pause calls `self._discovery_handle.cancel()`; Resume calls
`self.engine.discovery.start()` again and replaces the stored handle. No core
changes needed; `JobHandle.cancel()` already does exactly this.

Considered and rejected: auto-yielding (App itself cancels Discovery and
re-starts the requested Action once it's confirmed stopped) — Discovery has no
terminal event to key off (see Open questions below), so "confirmed stopped"
isn't observable without inventing a new event and a resume-after-async-step
handler in the GUI layer, for a problem a one-click manual control already
solves transparently. Matches this project's operator-drives-it posture better
too — the user should know what's using the radio, not have it silently
juggled. Accepted rough edge: rapid Pause-then-immediately-Start-Capture can
still transiently race `AdapterBusy` against Discovery's own not-yet-completed
release, since there's no confirmation event; this is caught by the same
`except AdapterBusy` handling below, so the failure is visible, not silent —
same "accepted tradeoff" tier as several items already listed in the core
design doc.

**`CapturePanel`** — bound to the tab's selected `Target`. Two buttons:

```python
def on_start_passive_clicked(self) -> None:
    self._try_start(deauth=False)

def on_start_deauth_clicked(self) -> None:
    # ADR-0001: "extra confirmation friction... still used for deauth, which
    # stayed in scope" — this is that friction, not optional GUI polish.
    if not self._confirm_deauth_dialog(self.target):   # modal: "This will
        # actively transmit deauthentication frames at <ssid> (<bssid>) to
        # force a handshake. Every firing is logged. Continue?"
        return
    self._try_start(deauth=True)

def _try_start(self, deauth: bool) -> None:
    try:
        handle = (self.engine.capture.start_deauth_assisted(self.target) if deauth
                  else self.engine.capture.start_passive(self.target))
    except AdapterBusy as e:
        self.app.status_bar.show_error(f"Radio busy: {e.holder.value} is using the "
                                        f"adapter — free it first (see Discovery tab)")
        return
    self.active_handle = handle
    self.pump.on(HandshakeCaptured, self._on_handshake, only_job=handle.job_id)
    self.pump.on(CaptureStopped, self._on_stopped, only_job=handle.job_id)
    self.pump.on(DeauthFired, self._on_burst, only_job=handle.job_id)   # local
    # live "N bursts fired" counter — separate registration from the tab-global
    # audit-log one below; GuiEventPump.on supports multiple handlers per type.
```

`self.pump.on(DeauthFired, self.audit_log_view.append)` (no `only_job` — every
firing, across every job, per ADR-0001's "no exceptions") is registered once,
globally, at `App` build time as part of wiring `AuditLogView`, not per capture
start — see **Audit Log tab** below. (The core doc's own Usage sketch registers
it inside `on_start_capture_clicked` instead; that reads as per-job, which
would under-count firings from any capture the audit view wasn't already
watching. Registering it once, globally, at startup is the correct reading of
ADR-0001's requirement — noting the deviation since it's non-obvious.)

Seeds its handshake list from `engine.capture.list_handshakes(target)` when the
selected Target changes; appends locally on `HandshakeCaptured` rather than
re-querying.

**`EnumeratePanel`** — bound to the same selected `Target`. One "Start
Enumerate" button, a static hint ("Requires this adapter already associated to
the Target's network via your OS's normal wifi settings — AirCommand doesn't
join networks itself") matching `enumerate.py`'s own Option-A docstring, and a
results table (`EnumHost`: ip / hostname / open ports).

```python
def on_start_clicked(self) -> None:
    try:
        handle = self.engine.enumerate.start_scan(self.target)
    except AdapterBusy as e:
        self.app.status_bar.show_error(f"Radio busy: {e.holder.value} is using the adapter")
        return
    self.active_handle = handle
    self.pump.on(NmapScanCompleted, self._on_completed, only_job=handle.job_id)
    self.pump.on(EnumerationFailed, self._on_failed, only_job=handle.job_id)   # NEW —
    # see "Gaps found" below; this event doesn't exist yet.
```

Without `EnumerationFailed`, this panel has no event to reset on if the
operator hasn't actually joined the Target's network yet (the single most
likely real-world failure for exactly this Action) — see the gap writeup.

## Crack tab

**`HandshakePicker`** — seeds from `engine.capture.list_handshakes()` (no
Target filter — any handshake, from any Target, is crackable). Subscribes
globally (no `only_job` — `HandshakeCaptured` carries no `job_id` filterable
concept meaningful here, and any target's capture should appear):

```python
self.pump.on(HandshakeCaptured, self.handshake_picker.append)
```

Row label uses `handshake.bssid` + `kind` + `captured_at` directly — no Target
join needed, since `Handshake` already carries its own `bssid`.
`HandshakeKind.WPA3_SAE` exists in `domain.py` but nothing mints it yet
(`capture.py`'s only `handshakes.insert(...)` call hardcodes
`kind=HandshakeKind.WPA2_EAPOL`); the core doc's own Open Questions still has
"do we mint a Handshake for WPA3/SAE at all" as unresolved. No GUI-side handling
needed for that case yet — noting the dependency so it isn't rediscovered later
as if it were new.

**`WordlistPicker`** — a button opening `tkinter.filedialog.askopenfilename`,
storing the chosen `Path`. (CustomTkinter has no file dialog of its own; the
stdlib one is the right tool here, not a gap.)

**Start/Cancel + progress**, only enabled once both a handshake and a wordlist
are selected:

```python
def on_start_clicked(self) -> None:
    handle = self.engine.crack.start(self.selected_handshake, self.selected_wordlist)
    self.active_handle = handle
    self.pump.on(CrackProgress, self._on_progress, only_job=handle.job_id)
    self.pump.on(CrackResult, self._on_result, only_job=handle.job_id)
```

`_on_result` renders the sealed `CrackOutcome` (`Found(key)` / `Exhausted()` /
`Aborted()` — `isinstance` dispatch, matching how `persistence/db.py` already
distinguishes them) and refreshes past results for this handshake via
`engine.crack.list_results(handshake)`.

## Audit Log tab

**`AuditLogView`** — constructed with `interrupted_deauth_targets:
list[Target]` from `App` (the in-memory, seeded-once-at-startup list built in
"Startup sequencing" above). Seeds from `engine.capture.list_audit_log()` (all
Targets). Has a `TargetSelector`-driven filter ("All Targets" as the extra
default option) that re-queries `list_audit_log(target)` synchronously on
change — a one-shot re-query per user action, not polling, matching the core
doc's own "populating a dialog the user just opened" carve-out. Subscribes
globally, no job filter:

```python
self.pump.on(DeauthFired, self.audit_log_view.append)
```

If `interrupted_deauth_targets` is non-empty, shows a persistent banner at the
top of this tab: "Audit log for {SSID list} may be missing deauth firings from
an interrupted prior session (ADR-0004) — some bursts may have fired
unlogged before this reconciliation." This is seeded state, not event-driven —
it's fixed for the life of the session the instant `App.__init__` computes it,
same reasoning as why `StartupReconciliationCompleted` isn't subscribed to at
all (see below). A manual "Refresh" button is included as a low-cost
completeness affordance, not because `DeauthFired` alone is insufficient.

## Status bar

Seeded from `self.engine.privilege.status` (sync read) and the `reconciliation`
summary, both already computed by the time `StatusBar` is constructed (see
Startup sequencing). Shows:

- A privilege indicator (ACTIVE = normal; updates to a warning state on
  `SudoKeepaliveFailed`, back to normal on `SudoKeepaliveRecovered` — both
  subscribed globally, no `job_id` on either event).
- A **one-time, dismissible** reconciliation banner if
  `reconciliation.processes_terminated > 0`: "Cleaned up N leftover process(es)
  from a previous crash." Built once at construction from the synchronous
  return value `App.__init__` already has — **not** a `StartupReconciliationCompleted`
  subscription, matching the existing stub's own comment ("nothing is
  subscribed to the pump yet at this point in startup"). If
  `reconciliation.interrupted_deauth_target_ids` is non-empty, the banner also
  says "— see the Audit Log tab" as a pointer to the persistent detail there.
- A generic `show_error(message: str)` surface, the landing spot for every
  `except AdapterBusy`/other click-site exception across every tab (matches the
  core doc's own Usage-sketch call shape exactly).

## Event subscription table

| Event | job-scoped? | Subscriber(s) |
|---|---|---|
| `NetworkDiscovered` | no (no `job_id`) | `NetworksView` |
| `NetworkSightingUpdated` | no | `NetworksView` |
| `TargetAdded` | no | `TargetPicker`, `TargetSelector` (×2 instances) |
| `TargetRemoved` | no | `TargetPicker`, `TargetSelector` (×2 instances) |
| `CaptureStarted` | — | none (click site already holds the `JobHandle`) |
| `HandshakeCaptured` | yes, per active job in `CapturePanel`; **no** filter in `HandshakePicker` | `CapturePanel` (own target), `HandshakePicker` (all) |
| `CaptureStopped` | yes | `CapturePanel` |
| `DeauthFired` | yes, in `CapturePanel` (own job, live counter); **no** filter, global, in `AuditLogView` | `CapturePanel`, `AuditLogView` |
| `NmapScanCompleted` | yes | `EnumeratePanel` |
| `EnumerationFailed` *(new — see gaps)* | yes | `EnumeratePanel` |
| `CrackStarted` | — | none (click site already holds the `JobHandle`) |
| `CrackProgress` | yes | `CrackPanel` |
| `CrackResult` | yes | `CrackPanel` |
| `SudoSessionStarted` | — | none (status seeded synchronously right after `privilege.start()`, before the pump exists) |
| `SudoKeepaliveFailed` | no | `StatusBar` |
| `SudoKeepaliveRecovered` | no | `StatusBar` |
| `StartupReconciliationCompleted` | — | none (synchronous return value used instead — see Status bar) |

Every `only_job`-scoped `.on(...)` call is made fresh at each Start click and is
never explicitly unsubscribed — `GuiEventPump.on`'s pinned shape has no
"remove" path, and each job only ever fires its own terminal event once, so a
stale entry is inert, not wrong. Over a long session with many repeated
Capture/Crack/Enumerate runs, `GuiEventPump._handlers` grows by a small,
bounded amount per run (a few list entries) — noted here as an accepted
characteristic of the already-pinned pump shape, not something this pass
changes.

## Gaps found & required core-side fixes

Neither of these is a GUI file, and this pass doesn't implement either — they're
specified exactly enough to dispatch as their own small, mechanical slices
(same tier as `Pacer` or the `airmon-ng` regex fix), most likely folded into
whichever implementation slice needs them (the reconciliation-banner slice; the
`EnumeratePanel` slice) rather than done standalone.

### 1. `ReconciliationSummary`/`StartupReconciliationCompleted` carry no per-job detail

`reconcile_orphaned_processes()` (`core/reconciliation.py`) builds `stale =
jobs.find_stale_jobs()`, uses it only for aggregate counts, and discards it.
But `events.py`'s own docstring on `StartupReconciliationCompleted` already
claims "a CAPTURE_DEAUTH job among the cleaned-up ones surfaces via its own
`CaptureStopped` event with `reason=INTERRUPTED_PRIOR_SESSION`" — **this isn't
true of the shipped code**: `JobRegistry.mark_terminal()` never publishes a bus
event at all (confirmed against `jobs.py` — it only writes the DB row and sets
an in-memory `threading.Event`), and `reconcile_orphaned_processes` calls it
with no `reason` parameter, because `mark_terminal` doesn't take one — the
`jobs` table has no `stop_reason` column at all (it's pure orphan-cleanup
bookkeeping, deleted on completion, per its own schema comment). Without a fix,
this design's "flag audit log as possibly incomplete" requirement (explicitly
asked for in this pass's own scope) can't be implemented against anything the
core actually exposes today.

**Fix** — extend the summary with the Target ids of every reconciled
`CAPTURE_DEAUTH` job, computed from `stale` before it's discarded:

```python
# core/reconciliation.py
@dataclass(frozen=True)
class ReconciliationSummary:
    stale_job_count: int
    processes_terminated: int
    interrupted_deauth_target_ids: tuple[int, ...]
    # target_id of every StaleJob with kind == JobKind.CAPTURE_DEAUTH, regardless
    # of whether its orphaned process was still alive to kill — the flag is about
    # "was a deauth session in flight when we crashed", not "did we find a
    # process". ADR-0004 Consequences: an unknown, unlogged number of bursts may
    # have fired for these Targets between the crash and this reconciliation.

def reconcile_orphaned_processes(...) -> ReconciliationSummary:
    stale = jobs.find_stale_jobs()
    ...  # existing loop, unchanged
    interrupted_deauth_target_ids = tuple(
        job.target_id for job in stale
        if job.kind is JobKind.CAPTURE_DEAUTH and job.target_id is not None
    )
    summary = ReconciliationSummary(stale_job_count=len(stale), processes_terminated=terminated,
                                     interrupted_deauth_target_ids=interrupted_deauth_target_ids)
    bus.publish(StartupReconciliationCompleted(..., interrupted_deauth_target_ids=interrupted_deauth_target_ids))
    return summary
```

```python
# core/events.py
@dataclass(frozen=True)
class StartupReconciliationCompleted(DurableEvent):
    stale_job_count: int
    processes_terminated: int
    interrupted_deauth_target_ids: tuple[int, ...]   # mirrors ReconciliationSummary,
    # for symmetry with any future non-GUI subscriber — the GUI itself uses the
    # synchronous return value, not this event, at startup (see Status bar above).
```

Not ADR-worthy: this doesn't decide between real alternatives, it closes a gap
between an already-decided ADR-0004 consequence and code that doesn't yet
implement it.

### 2. `Enumerator._drive` has no failure event — `EnumeratePanel` can't detect a failed scan

`NmapScanCompleted` is published only on the success path, inside the `try`
block. `enumerate.py`'s own module docstring already flags this as a known,
deliberately deferred gap ("there's no dedicated 'enumeration failed' event...
out of scope" for Phase 1 item 3). It's load-bearing now: if `get_subnet`
raises `OSError` (the single most likely real-world case — the operator hasn't
actually joined the Target's network yet, Option A's own accepted tradeoff) or
anything else fails mid-`_drive`, the exception propagates out of an
un-caught `threading.Thread` target. `finally` still runs (`rf.release()`,
`mark_terminal()`, `db_scope.close()`), so the job row is cleaned up — but no
event fires, so `EnumeratePanel` has no way to leave its "Enumerating…" state,
ever, for that run. This differs from Capture and Crack, whose own `_drive`
`finally` blocks unconditionally publish a terminal event regardless of how the
`try` exited (`CaptureStopped`/`CrackResult`) — Enumerate is the one driver that
doesn't follow that pattern yet.

**Fix** — mirror Capture/Crack's own "always publish in finally" shape:

```python
# core/events.py — new event
@dataclass(frozen=True)
class EnumerationFailed(DurableEvent):
    job_id: JobId
    target_id: int
    error: str   # str(exc) — enough fidelity for display; the exception type
                 # itself isn't a stable cross-boundary contract worth exposing.
```

```python
# core/enumerate.py — Enumerator._drive, patched
def _drive(self, job_id, token, reservation, target, options) -> None:
    db_scope = self._new_connection_scope()
    try:
        subnet = self._get_subnet(reservation.adapter)
        argv = [...]
        handle = self._proc.spawn(argv, privileged=True)
        self._jobs.record_process(job_id, handle.pid, handle.pgid, f"nmap {target.bssid}",
                                   repo=db_scope.jobs)
        xml = b"".join(l.encode() for l in handle.lines())
        hosts = parse_nmap_xml(xml)
        db_scope.enum_results.insert(target_id=target.id, job_id=job_id, hosts=hosts)
        self._bus.publish(NmapScanCompleted(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                                             job_id=job_id, target_id=target.id, hosts=hosts))
    except Exception as exc:
        self._bus.publish(EnumerationFailed(event_id=uuid.uuid4(), occurred_at=datetime.now(),
                                             job_id=job_id, target_id=target.id, error=str(exc)))
        raise   # unchanged control flow otherwise — still logged via the default
                # threading excepthook, still reaches `finally` below
    finally:
        self._rf.release(reservation)
        self._jobs.mark_terminal(job_id, repo=db_scope.jobs)
        db_scope.close()
```

Not ADR-worthy either: it applies an already-established pattern (Capture's and
Crack's own `finally`-always-publishes shape) to the one driver that hadn't
gotten it yet, rather than choosing between real alternatives.

## Decisions made in this pass (and why neither is a new ADR)

- **Discovery auto-starts; a manual Pause/Resume control (not auto-yield) is
  what frees the radio for Capture/Enumerate.** See "Target Actions tab" above
  for the full reasoning. Real tradeoff, multiple options considered — but
  scoped entirely to this GUI's own internal control layout (which button does
  what), not a core-architecture decision the way ADR-0001–0004 are. Recorded
  here, not spun into a fifth ADR.
- **`DeauthFired` → `AuditLogView.append` is wired once, globally, at startup,
  not per-capture-click** (deviating from the core doc's own Usage sketch,
  which shows it wired inside `on_start_capture_clicked`). This is the correct
  reading of ADR-0001's "every firing, no exceptions" — per-click wiring would
  silently under-count firings from any capture that started before the audit
  view happened to be watching, which can't happen once it's wired once at
  startup instead. An implementation detail correcting the sketch to match its
  own governing ADR, not a new tradeoff.

## Open questions / deferred (not blocking implementation)

- **No `DiscoveryStopped`-equivalent event exists.** `EnumeratePanel`'s
  `TargetSelector`/Discovery's own Pause button can't observe "the radio is
  now actually free" — only send the cancel request. Acceptable per "Target
  Actions tab" above (the failure mode, if the race is lost, is a caught,
  visible `AdapterBusy`, not silence). Worth adding later purely as UX polish
  (e.g. graying out Start buttons while Discovery is mid-stop); not required
  for a correct v1.
- **WPA3/SAE handling** stays exactly as unresolved as `core-gui-boundary.md`
  already left it. No GUI-side gap today since nothing mints a
  `HandshakeKind.WPA3_SAE` handshake yet — see "Crack tab" above.
- **Is a one-shot "Refresh" button on `AuditLogView` worth building**, given
  `DeauthFired` already keeps it live? Included above as low-cost, not required.

## Module map

```
aircommand/gui/
  app.py                    # App — owns Engine, builds the tree per this doc
  event_pump.py              # GuiEventPump — pinned separately, unchanged here
  sudo_dialog.py               # SudoPasswordDialog
  status_bar.py                 # StatusBar
  target_selector.py              # TargetSelector — reusable, used by Target
                                   #  Actions tab and Audit Log's filter
  discovery_view.py                # NetworksView, TargetPicker
  target_actions_view.py            # composes TargetSelector + CapturePanel +
                                     #  EnumeratePanel; owns the deauth confirm dialog
  capture_view.py                    # CapturePanel
  enumerate_view.py                   # EnumeratePanel
  crack_view.py                        # HandshakePicker, WordlistPicker, CrackPanel
  audit_log_view.py                     # AuditLogView
```

## Next implementation step

Two small core-side fixes first (**Gaps found**, above) — each is a
self-contained, mechanical slice, dispatchable the normal way (CLAUDE.md's
model tiering: interface already pinned here, implementation goes to a
subagent, read the diff back). Then the GUI views themselves, in an order that
keeps each slice's own tests meaningful without earlier slices being stubs:
`GuiEventPump` (already pinned, do this first since everything else depends on
it) → `SudoPasswordDialog` + `StatusBar` + startup sequencing in `app.py` →
`NetworksView`/`TargetPicker` (Discovery & Targets tab) → `TargetSelector` +
`CapturePanel` + `EnumeratePanel` (Target Actions tab, needs the two core fixes
landed first) → `HandshakePicker`/`WordlistPicker`/`CrackPanel` → `AuditLogView`.
Not scoped further here per this session's own checkpoint boundary — that's a
follow-up session's job, same as `docs/roadmap.md`'s existing Phase 3 note says.
