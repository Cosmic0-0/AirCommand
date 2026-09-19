# Roadmap

Written for a fresh session picking this project up cold. Read CLAUDE.md, CONTEXT.md,
`docs/adr/`, and `docs/design/core-gui-boundary.md` first — this doc assumes all of
that and doesn't repeat it. It tells you what's done, what's left, what order to do it
in, and — for the parts that aren't actually scoped yet, just stubbed — says so
explicitly rather than pretending a TODO comment is a finished design.

## Current state — read this whole section before doing anything

**Phase 1 is complete.** Committed, in order: core scaffolding + GUI shell +
ADRs/design doc — `domain.py` + `events.py` — the headless Discovery flow end to
end — `Allowlist` + `TargetRepository` (including the `Target.channel` field
Capture needed) — Discovery's on-disk-CSV-poll bug fix (Phase 1 item 0) —
`Capture` (item 1) — `Crack` (item 2) — `Enumerator` (item 3, Option A subnet
source). 93 tests pass. Nothing has touched a real subprocess, real `sudo`, or
real hardware yet — every slice so far runs against `FakeProcRunner`; that's
exactly what Phase 2 is for. See each Phase 1 item below for what was
actually decided/found while implementing it — left in place as rationale for
Phase 2, not deleted now that the item is done.

**Also fixed this session, in already-committed code from before Phase 1 started
(not part of any Phase 1 item, found incidentally while testing Crack):**
`JobRegistry.mark_terminal()` (`jobs.py`) was setting the in-memory event that
unblocks `JobHandle.wait_for_test()` *before* its own DB delete actually
committed, against the single sqlite3 connection every driver thread shares
unsynchronized. Starting a second job immediately after a first job's
`wait_for_test()` returns — exactly Crack's own usage shape (capture a
Handshake, then immediately crack it) — could race the two threads' writes on
that shared connection (`sqlite3.OperationalError: cannot commit - no
transaction is active`, reproduced directly). Fixed by reordering
`mark_terminal()` so the DB write completes first. This closes that specific
race; it does **not** fix the deeper issue that every driver thread still
shares one unsynchronized connection (see `persistence/db.py`'s `Database`
docstring) — that's real, pre-existing, and still open, see Phase 2 below.

**Next: Phase 2** — first real subprocess/hardware code. Nothing below this
point has been started.

## The process (already in CLAUDE.md — restated briefly because it matters)

For each remaining core slice:

1. Read the stub file(s) plus whatever ADR/design-doc section covers them.
2. Before dispatching anything, work out any interface gaps yourself and pin them
   **directly in the code as TODOs** (not just in your own head or in chat) — exact
   signatures, exact semantics for anything non-obvious (who mints a value, what
   gets preserved on an upsert, when an event fires vs. doesn't). This repo has
   already hit real gaps this way twice (`AdapterReservation` had nowhere to carry
   the adapter name; `Allowlist.add` had nowhere to receive an `ssid`) — expect more,
   especially in the slices flagged "not actually scoped" below.
3. Dispatch the mechanical implementation to a subagent (Sonnet) with a
   self-contained prompt: exact files, exact scope, exact TODOs to follow, exact
   out-of-scope list.
4. When it reports back, **read the actual diff yourself** — don't take the summary
   on faith. Independently rerun the full test suite. Fix small things directly;
   redispatch anything that got the design wrong.
5. Give a commit message and the exact `git add`/`git commit` command — never run
   them yourself (see the "Dev workflow: commits and pushes" rule in CLAUDE.md).

If you're running through several slices in one sitting without the user round-tripping
on each commit: that's fine, keep going — but stage each slice's `git add` against
only the files *that slice* touched, and if a later slice ends up editing a file an
earlier slice's commit already listed, don't present two commits that'd conflict —
either commit the earlier one's files first (mentally, in the message you hand back)
or fold them into one honest commit that says what it actually contains. Present
every pending commit at the end if you don't stop earlier. If you hit something only
the user can decide (see the flagged items below), stop and ask rather than guessing.

Make sure this doc (and any ADR/design-doc update you make) is actually committed
before a session ends or a new chat starts — a fresh session's only way to inherit
what you learned is by reading what's committed. An update sitting uncommitted in
the working tree might as well not exist to the next chat.

**Why this is one doc updated in place, not a fully-detailed plan written far in
advance:** this exact question — "should we front-load full detail for every
remaining phase in one big planning chat before touching any of it?" — was run past
a 5-advisor LLM council. Unanimous verdict: no. The reasoning: every real interface
gap found in this project so far (see the entries below) surfaced only once someone
was hands-on with the actual code, immediately before dispatching it — never from
reading stubs or docs cold, however carefully. A planning-only chat, by definition,
only reads cold. Worse than being merely useless, a confidently-written phase spec
around a gap nobody's actually verified reads as *decided* to whoever executes it
later, making the error harder to catch, not easier. So: keep this doc thin, resolve
each slice's real gaps immediately before dispatching *that* slice (step 2 above),
and don't try to pre-solve Phase 2 or Phase 3 problems from inside a Phase 1 mindset.

## Conventions established so far (not written down anywhere else — follow them)

- **SQLite**: `MacAddress`/`BSSID` → `str(x)` to store, `BSSID(value=row["..."])` back.
  Enums → `x.value` to store, `EnumType(row["..."])` back. `datetime` → `x.isoformat()`
  to store, `datetime.fromisoformat(row["..."])` back. `JobId`/UUIDs → `str(x)` to
  store, `uuid.UUID(row["..."])` back. `conn.row_factory` is already `sqlite3.Row`.
- **Mint-restricted types**: the repository mints (imports the `_*_MINT` sentinel,
  constructs the frozen dataclass directly), the facade stays a thin delegation.
  `Target` (`TargetRepository`) and `Handshake` (`HandshakeRepository`) both follow
  this now — `Handshake`'s was a deliberate choice, not a default: the original
  `capture.py` TODO sketch showed the opposite (facade mints from a raw `row`), but
  that sketch was never actually implemented, so there was no shipped code to
  reconcile. If a future mint-restricted type shows up, mint it in the repository.
- **"Preserve the original on upsert"**: `Network.first_seen` and `Target.date_added`
  both mean "the first time this became true," not "the last time this row was
  written" — excluded from the `UPDATE` SET list on purpose, with a comment saying so.
  Confirmed this doesn't recur for `Handshake.captured_at`: handshakes are pure
  `INSERT`s (`HandshakeRepository.insert`), never upserted, so there's no second
  write that could ever overwrite it.
- **`DurableEvent` fires only for a real write.** `Allowlist.remove()` on a bssid
  that was never a Target publishes nothing — there's no state transition to
  describe. Apply the same check-before-publish discipline anywhere an operation
  might be a no-op.
- **Exception safety in driver threads.** Every `_drive` (Discovery, Capture, Crack,
  Enumerator) wraps spawn-through-loop in `try/finally` so `rf.release()`
  (where applicable) and `jobs.mark_terminal()` always run, even if something raises
  mid-loop — otherwise a reservation or a `jobs` row leaks forever and the app can
  never run another job of that kind without a restart.
- **Terminal ordering: publish the terminal `DurableEvent`, then `mark_terminal()`
  last, always.** `mark_terminal()` is what unblocks `JobHandle.wait_for_test()`
  (and, in spirit, any future external "is this job done" signal) and does its own
  DB write — a caller waking on it must be able to trust both the terminal event and
  that write already happened, not race either. Got this backwards once (`jobs.py`
  itself, plus an inherited TODO sketch for `capture.py`/`crack.py`) and it caused a
  real, reproduced `sqlite3.OperationalError` once a second job could start
  immediately after a first one's `wait_for_test()` returned — see "Current state"
  above. Fixed everywhere it existed; keep new `_drive`s in this shape from the start.
- **Thin facade, fat repository.** Facade methods are 1-4 line delegations; the
  repository holds the actual SQL and constructs the actual domain object. Keep
  following this rather than letting SQL creep into `capture.py`/`crack.py`/etc.
- **Poll a clean on-disk artifact after the fact; don't trust a live
  stdout/terminal stream for a correctness-critical fact.** This recurs three
  times now: Discovery's network data (read the CSV file airodump-ng writes, not
  its stdout), Capture's handshake detection (check the `.cap` file via a separate
  `aircrack-ng` invocation, not airodump's live "WPA handshake:" display line), and
  Crack's found-key (check hashcat's `--outfile` after it exits, not the numeric
  `status` field in a live `--status-json` tick). A tool's live interactive display
  is for humans watching a terminal, not for a subprocess-driven caller to parse —
  assume that's true again for anything not yet verified (nmap's `-oX -` streaming
  clean XML to stdout is a confirmed exception, not the default to assume).
- **Record real decisions where the project already records them, not in a new
  place.** When you resolve one of the gaps flagged below (or find a new one),
  don't just fix the code and move on — if it's a genuine architectural
  tradeoff (multiple real options, a reason one was chosen), write it as an ADR
  in `docs/adr/`, matching ADR-0001 through 0004. If it's smaller than that,
  update this roadmap or `docs/design/core-gui-boundary.md` in place. Either way
  it needs to be committed (see above) — a decision that only exists in one
  session's chat transcript doesn't exist for the next one.

## Phase 1 — core engine, headless-testable (do these via FakeProcRunner, same as Discovery)

### 0. Fix Discovery — confirmed bug, do this before anything else in this phase [DONE]

`airodump-ng --write-csv <prefix>` writes CSV data to **files on disk**
(`<prefix>-01.csv` etc.) — confirmed against the aircrack-ng manual and independent
discussion of the same problem from another tool's maintainers. It does **not**
write CSV to stdout. Airodump-ng's stdout carries its live, redrawing interactive
display instead (a `CH 6 ][ Elapsed: ... ][ WPA handshake: ...` header over a
constantly-updated table) — not comma-separated data, and not reliably line-by-line
parseable the way the current code assumes.

`Discovery._drive` (shipped, tested, committed) reads `handle.lines()` — the
process's **stdout** — and feeds each line to `parse_airodump_csv_line()`. Against
real airodump-ng this receives interactive-display text, not CSV rows, and would
successfully parse essentially nothing. All existing tests pass only because
`FakeProcRunner`'s fixtures hand-construct CSV-looking lines and feed them in as
stdout, which isn't what the real tool actually does. Discovery currently works in
tests and would discover nothing against real hardware.

**The fix**: spawn airodump-ng with `--write-csv <path>` as today, but stop reading
`handle.lines()` for network data. Instead, on a timer (decoupled from stdout
entirely — a `Pacer`-style periodic check, e.g. every 2-3s), read and re-parse
`<path>-01.csv`'s *current full contents* from disk — airodump-ng rewrites the file
in place each refresh cycle rather than appending, so each read should replace the
previously-known network set for this scan cycle, not accumulate duplicates.
`parse_airodump_csv_line()` itself is still correct and reusable (it's a pure
function over one CSV line; the bug is entirely in *what stream `_drive` reads it
from*, not in the parser). `FakeProcRunner`'s fixtures need to change to match:
either give it a way to also fake a file being written to `work_dir` (simplest:
`FakeProcRunner`/`_FakeProcHandle` could write the scripted CSV content straight to
the expected file path on `spawn()`, since the test doesn't need to simulate
real-time incremental writes to prove the read-and-reparse loop works), or inject
the file path/read mechanism as its own seam. Still keep `handle.lines()` in the
loop for what it's actually good for: detecting cancellation and detecting that the
underlying process died (the for-loop ends naturally either way) — just don't trust
its *content* for network data anymore.

This is the same "poll a well-defined on-disk artifact instead of scraping a live
terminal" pattern Capture needs below for handshake detection — fix them with the
same idiom, and note that idiom as a convention (see "Conventions established so
far") once both are done, since it'll likely recur (nmap doesn't have this problem —
`-oX -` genuinely streams clean XML to stdout — but any other aircrack-ng-suite tool
driven this way should be checked against real behavior before assuming stdout
carries what you'd expect).

Update `tests/test_discovery_acceptance.py` accordingly — it currently scripts
`FakeProcRunner` with CSV lines as scripted stdout, which will need to change to
match whatever the fixed `_drive` actually reads.

### 1. `capture.py` [DONE]

The only `Handshake` mint site; drives **three** tools, not two (see below); is
where ADR-0001's audit requirement actually bites (`DeauthFired` must be written to
`audit_log` *before* the event publishes). Depends on `Allowlist` (done — including
the now-available `Target.channel`) and needs `HandshakeRepository` +
`AuditLogRepository` implemented in `persistence/db.py` (currently bare stubs).

**Handshake detection — researched, mostly resolved, one residual gap flagged
below.** The original stub's premise (`parse_airodump_handshake_flag(csv_block)` —
a handshake flag inside airodump's CSV) is confirmed wrong: there is no such flag
in the CSV. Two real mechanisms exist, verified against the aircrack-ng manual,
docs, and community references (not hardware — this was resolvable via research
alone, as Phase 1's own gap-triage predicted):

1. Airodump-ng's own **stdout**, while running interactively, redraws a header line
   `CH <n> ][ Elapsed: <t> ][ <timestamp> ][ WPA handshake: <BSSID>` once it detects
   one. Rejected as the primary mechanism: it's part of the same
   constantly-redrawing live display discussed in the Discovery fix above, with the
   same fragility, and mixing "is this a real capture event" with "scrape a TUI" is
   exactly the failure mode that bit Discovery.
2. **Post-hoc check against the `.cap` file itself**, using a *separate* one-shot
   `aircrack-ng` invocation — the standard, well-documented approach. Run
   `aircrack-ng -b <target.bssid> -w /dev/null <cap_path>` (`-b` targets the BSSID
   directly, skipping the interactive network-selection prompt that appears with
   ambiguous capture files; `-w /dev/null` gives it an instantly-exhausted wordlist
   so the process completes non-interactively regardless of whether a handshake is
   present — genuinely confirmed live-tested behavior would still be worth a Phase 2
   check, since this specific flag combination wasn't verified by running it, only
   researched). Its stdout prints a table row ending in `(1 handshake)` when a valid
   handshake is present, or `No valid WPA handshakes found` when not. Since `-b`
   restricts output to one BSSID, a plain substring check for `"handshake)"` in the
   full output is enough — no need to parse the table structure.

   **This changes `_drive`'s shape**: handshake detection is no longer something
   checked per airodump CSV line (there's nothing useful to check per line for this
   purpose anymore, matching Phase 1 item 0's finding that CSV-stream content isn't
   trustworthy for per-event facts anyway) — it's a periodic check, same `Pacer`
   idiom as the deauth timer, e.g. every 3-5s: spawn the `aircrack-ng` check,
   `.wait()` for it (it's fast and one-shot, no need to stream `.lines()`), inspect
   its output. Add a pure parser in `parse.py` for this, e.g.
   `parse_aircrack_handshake_check(output: str) -> bool`, keeping the same
   "pure function over tool output text" shape as the CSV parser.

**Mint site for `Handshake`**: **repository-mints**, matching `Target`'s
established precedent (`HandshakeRepository.insert` imports `_HANDSHAKE_MINT` and
constructs the full `Handshake` itself; `capture.py` stays a thin caller) — the
original `capture.py` TODO's own pseudocode showed the opposite (facade-mints from
a raw `row`), which was never actually implemented, so there's no shipped code to
reconcile; just follow the pattern that's already real and tested.

**`FakeProcRunner` fixtures needed for three tools**, not two:
airodump-ng (still spawned to write the `.cap` file — no longer read line-by-line
for handshake detection, same file-writing pattern as Phase 1 item 0's Discovery
fix, just producing a `.cap` capture instead of a `.csv`), aireplay-ng
(fire-and-wait per deauth burst, no meaningful output to parse, just `.wait()`),
and now aircrack-ng (one-shot, `.wait()`, check stdout text per above).

### 2. `crack.py` [DONE]

Depends on a real `Handshake` — since `Handshake` is mint-restricted, you can't
hand-construct one for an isolated Crack unit test, so this naturally wants Capture
done first and its own test fixtures reused to produce a real `Handshake` to feed
Crack's tests. `CrackResultRepository` needs implementing (same mint-in-repository
question as above, for `CrackResultRow` — though note `CrackResultRow` itself isn't
mint-restricted, so this one's lower-stakes).

**`parse_hashcat_status_line`/`HashcatStatus` — researched, real field shape found**
(via a third-party typed Go binding built against real hashcat output, cross-checked
against hashcat's own GitHub issue discussion — not hand-run against real hashcat,
so treat the exact field names below as strong-confidence research, still worth a
quick sanity check against a real run before fully trusting it). Each `--status-json`
line is one JSON object, not wrapped in an array or newline-delimited differently
than one-object-per-line:

```json
{
  "session": "...", "status": <int status code>, "target": "...",
  "progress": [<current>, <total>],
  "recovered_hashes": [<recovered>, <total>], "recovered_salts": [<r>, <t>],
  "rejected": <int>, "restore_point": <int>,
  "guess": {"guess_base": "...", "guess_base_percent": <float>, "...": "..."},
  "devices": [{"device_id": <int>, "device_name": "...", "speed": <int H/s>, "util": <int>, "temp": <int>}],
  "time_start": <unix ts>, "estimated_stop": <unix ts>
}
```

Map to `CrackProgress`: `percent` = `progress[0] / progress[1] * 100` when
`progress[1] > 0`; `hashrate` = format `sum(d["speed"] for d in devices)` as a
human string (raw value is H/s as an int — needs unit scaling, e.g. `12.3 MH/s`);
`eta` = `timedelta(seconds=estimated_stop - time.time())` when `estimated_stop` is
present and in the future, else `None`. The exact integer meaning of `status` codes
(e.g. which value means "cracked" vs "exhausted" vs "running") wasn't confirmed with
full confidence — **don't branch outcome logic on the numeric `status` value**.
Instead, pass `--outfile <path>` explicitly to hashcat and, after the process exits,
check whether that file has content: non-empty means `Found(key)` (read the
plaintext from it), empty means `Exhausted` (if the process ran to completion) or
`Aborted` (if cancelled) — this avoids needing to trust an unverified enum and
matches the same "check a clean on-disk artifact after the fact, not a live stream
value" idiom used for Discovery's CSV and Capture's handshake check above.

### 3. `enumerate.py` [DONE]

Independent of Capture/Crack (only needs `Allowlist`). Had a real open design gap
(not just an implementation detail): the `_drive` TODO referenced a `target_subnet`
that was never defined anywhere. A `Target` only carries a wifi `bssid`/`ssid` —
nmap needs an IP/subnet, and nothing in the original design said how to get from
one to the other. Two options were put to the user rather than decided solo, since
it touches security-sensitive territory (whether this tool stores wifi credentials
at rest):

- **Option A**: AirCommand never joins a network itself. It assumes the operator
  already associated to the target network through their OS's normal wifi settings
  before clicking "Enumerate," and just reads whatever subnet the currently-active
  interface is on (stdlib-only — no new domain fields, no credential storage).
  Smaller scope, matches this project's minimal-scope posture, but means Enumerate
  fails if the operator hasn't manually joined first.
- **Option B**: AirCommand manages the join itself — which means storing a
  network's credentials somewhere (a new field on `Target`? a separate secret
  store?) and giving `RadioController`'s `AdapterMode.MANAGED` a real "associate to
  this specific network" operation it doesn't have today. Real scope increase
  across `domain.py`/`allowlist.py`/`rf.py`, and "should this tool store wifi
  passwords at rest" is a security-posture decision worth an ADR of its own, not a
  quiet default.

**Decided: Option A** (confirms an assumption, per the note above doesn't need its
own ADR). `enumerate.py` now has `get_interface_subnet(interface) -> str`, a
stdlib-only (`socket`/`fcntl`/`struct`) raw-ioctl read of whatever IPv4 subnet the
adapter currently has an address on, injected into `Enumerator` as a `get_subnet`
constructor param (same testability-seam pattern as `ProcRunner` — production
default is the real ioctl function, tests inject a canned subnet). Accepted
tradeoff: if the operator hasn't actually joined the target network yet,
`get_interface_subnet` raises `OSError` and the scan job ends via the same
"let an unexpected error propagate past the `finally` cleanup" path every other
driver already uses — there's no dedicated failure event for this in `events.py`,
and adding one was treated as out of scope for this decision rather than a quiet
default of its own.

## Phase 2 — first real subprocess/hardware code

Everything above can still be developed and tested with `FakeProcRunner` alone,
exactly like Discovery. This phase is where that stops being true — do it as one
connected unit, and expect to need actual hardware (a real adapter, real root) to
validate it, not just `pytest`.

- **`privilege.py`** (`SudoSession`): `start`/`run_privileged`/`stop`/keepalive loop.
  Decide the testing strategy explicitly before implementing — there's no existing
  seam like `ProcRunner` for this; you'll likely need `unittest.mock.patch` on
  `subprocess` directly, or a passwordless-sudo test environment. Don't leave this
  implicit.
- **`procutil.py`'s real `SubprocessRunner.spawn`** and **`rf.py`'s real
  `airmon-ng` invocation inside `reserve()`/`release()`** (both currently
  deliberately deferred, with comments saying so) — go together, since
  `SubprocessRunner` is what `RadioController` would eventually spawn through.
- **`Engine.shutdown()`** — still `raise NotImplementedError`. Do it once
  `privilege.py` exists (it needs to call `self.privilege.stop()`): cancel live
  jobs, wait briefly for their terminal events, stop `SightingBatcher`, stop the
  keepalive, close the DB.
- **`reconciliation.py`** (ADR-0004) — needs `privilege.py`, the real
  `SubprocessRunner`, and realistically `capture.py` (the "unlogged deauth bursts"
  scenario it exists for doesn't mean anything without Capture). Also needs
  `procutil.py`'s `is_process_group_alive`/`terminate_process_group`/
  `_send_signal_unprivileged` (Linux `/proc` parsing — already well-specified via
  existing TODOs, lower risk than the items above).
- **Re-check Capture's handshake-detection assumption against real output**, once
  real hardware exists to check it against — see the flag in Phase 1's `capture.py`
  entry. If it was implemented from research alone without full confidence, this is
  where that gets settled for real, not guessed at again.
- **Give each job-driver thread its own SQLite connection**, per `persistence/db.py`'s
  `Database` docstring — already flagged as provisional there, now with a concrete
  reason it's not just theoretical: see "Current state" above. `JobRegistry.mark_terminal`'s
  own ordering bug is fixed, but every driver thread still writes through one shared,
  unsynchronized `sqlite3.Connection`; a genuinely concurrent write from two real
  driver threads (not just the wait_for_test()-mediated handoff that surfaced this)
  is still an open risk once Phase 2 makes threads/timing real instead of
  `FakeProcRunner`-fast.

## Phase 3 — GUI

**Do not treat this as a normal slice to scope-and-dispatch.** `gui/event_pump.py`
is actually fine to implement normally — `GuiEventPump.on`/`_tick` are already
precisely specified (dict-append, drain-and-dispatch-with-job-filtering,
self-rescheduling via `root.after`) and can go through the same process as any core
slice. But `gui/app.py` is a 49-line stub whose entire widget tree — the networks
table, target picker, capture panel, audit log view, crack progress + wordlist
picker, status bar, the sudo password dialog — doesn't exist anywhere, not even as
stub files. `docs/design/core-gui-boundary.md` deliberately only covers the
core/GUI *boundary*; it was never meant to specify the GUI's own internal structure.

This needs its own architecture pass first — run `/architect` (or an equivalent
planning conversation) scoped to "the GUI's internal structure: what views exist,
how they're laid out, how each subscribes to `GuiEventPump`, what state each one
seeds itself from at startup" — the same way the original core/GUI boundary got a
dedicated design pass before any core implementation started. Only scope
implementation slices out of GUI work once that design exists and is recorded
(likely as a new `docs/design/gui-*.md` alongside the existing one, plus an ADR if
it involves a real tradeoff). Don't let implementation pressure turn this into
ad-hoc widget-by-widget improvisation.

## What "done" looks like

Phase 1 and Phase 2 complete: the whole core engine is implemented, tested, and can
run for real against actual hardware with actual `sudo` — not just headlessly. Phase
3 has at least a recorded design and, ideally, a working GUI wired to the real
`Engine`. If you stop partway through any of this, leave the working tree in a state
where `git status`/recent commit messages make it obvious exactly what's done, what's
mid-flight, and what's next — the next session (a review pass, per the user) needs to
be able to reconstruct that without you there to ask.
