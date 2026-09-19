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

**Phase 2 is complete except item 5.** Items 0, 1, 2, 3, and 4 (below) are all
done — `Engine.shutdown()` and `reconciliation.py` (items 2/3, ADR-0004)
landed in the same session that also confirmed real `aircrack-ng`/`hashcat`/
`nmap` and a real monitor-mode-capable adapter are now present on this machine
(see the constraints paragraph below, updated to match — it was stale relative
to item 1's own entry further down before this update). Item 5 is no longer
blocked on missing tools/hardware, but still needs the user driving it
hands-on (a real terminal for `sudo`, and an authorized Target network they
nominate themselves) — not something a future headless session can pick up
unattended. See "Phase 2" below for the authoritative, up-to-date state of
each item — this paragraph is a pointer, not a duplicate.

**This machine's real constraints, updated — CHANGED since this paragraph
originally shipped, don't trust an older copy:** real Linux Mint, not a VM.
`aircrack-ng`, `hashcat`, and `nmap` are now installed (`sudo apt install
aircrack-ng hashcat nmap`), and a real monitor-mode-capable USB adapter is now
plugged in (Ralink RT2870/RT3070, `rt2800usb` driver, shows up as
`wlx24050f7d7ae0` in managed mode; renames to `wlan0mon` under `airmon-ng
start` — see item 1's own entry below for the real bug this surfaced and
fixed). Real hands-on hardware validation is genuinely possible on this
machine now, not just headless `pytest`. `sudo` is still NOT passwordless
(`sudo -n true` fails), and Claude Code's `!`-prefixed inline shell mechanism
does not allocate a TTY, so `sudo` cannot prompt for a password through it —
any real privileged command needs the user to open an actual separate
terminal window themselves, run it there, and paste the output back. Items
0-4 needed none of that (all fully headlessly testable — see each item's own
entry below, including items 2/3's own new tests); item 5 is the one item
whose own scope IS real hands-on hardware validation, and it's now genuinely
actionable rather than categorically blocked. Say so explicitly whenever an
item needs real `sudo`/hardware and can't get it in-session, rather than
claiming something works that was only run against `FakeProcRunner`.

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

Everything in Phase 1 could be developed and tested with `FakeProcRunner` alone,
exactly like Discovery. This phase is where that stops being fully true — real
hardware (a real adapter, real root) is needed to validate some items beyond
`pytest`, though several items below turned out to be pure Python/SQLite/
threading work, fully headlessly testable in spite of being "Phase 2." Numbered
0-5 below — this numbering is now the canonical reference other docs/handoff
prompts use, so keep it stable rather than renumbering.

### 0. `privilege.py` (`SudoSession`) [DONE]

`start`/`run_privileged`/`stop`/keepalive loop implemented and tested headlessly
(`tests/test_privilege.py`) — no existing `ProcRunner`-style seam existed for
this, so the testing strategy had to be decided explicitly rather than reused
from elsewhere; see the test file itself for what was chosen. Real-`sudo`
end-to-end behavior is still unverified hands-on — this machine's `sudo` isn't
passwordless (see "Current state" above), so that path needs the user's own
validation on real hardware.

### 1. `procutil.py`'s real `SubprocessRunner.spawn` + `rf.py`'s real `airmon-ng` calls [DONE]

Implemented together, tested headlessly (`tests/test_procutil.py`,
`tests/test_rf.py`) — `RadioController.reserve()`/`release()` now really spawn
`airmon-ng start`/`stop <adapter>` through the injected `ProcRunner`, parsed via
`parse_airmon_monitor_interface`.

**The airmon-ng rename question is now hardware-confirmed, with a real bug
found and fixed** (once the user got a monitor-mode-capable adapter plugged in
and the aircrack-ng suite installed): `sudo airmon-ng start wlx24050f7d7ae0`
against a real Ralink RT2870/RT3070 (rt2800usb driver) DOES rename the
interface, matching the "classically... reportedly true for rt2800usb"
research — but not to `<original>mon`. That adapter's udev-persistent name
(`wlx24050f7d7ae0`, 15 characters) is already at Linux's IFNAMSIZ-1 limit, so
`<original>mon` (18 characters) doesn't fit; airmon-ng instead falls back to an
old-style short name (`wlan0mon`) and — not previously anticipated — drops the
"for `<original>`" clause from its announcement line entirely in that case:
`(mac80211 monitor mode vif enabled on [phy1]wlan0mon)`, no "for" clause at
all. `parse_airmon_monitor_interface`'s regex required that clause
unconditionally, so against this real output it silently fell all the way
through to `fallback` — the WRONG interface name — which would have broken
every subsequent real `airodump-ng`/`aireplay-ng` spawn (they'd target an
interface that no longer exists post-rename). Fixed by making the "for `\S+`"
clause optional in the regex; see `parse.py`'s own docstring and
`tests/test_parse.py`'s regression test, which keeps the real captured output
verbatim rather than a trimmed repro. The originally-researched "for X on Y"
shape (both clauses present) was never itself hardware-confirmed and may not
be real airmon-ng output at all — kept supported as a harmless superset, not
because it's confirmed.

Also confirmed, no code change needed: `airmon-ng start` warns about
NetworkManager/wpa_supplicant/avahi-daemon potentially interfering (channel
changes, forcing the interface back to managed mode) — exactly the scenario
`rf.py`'s own comment already anticipated when it deliberately decided NOT to
run `airmon-ng check kill` automatically (would risk killing the operator's own
network connection). This is expected, working-as-designed behavior, not a bug.

**Found only once item 4 (below) actually unblocked `Database` and let the
Discovery/Capture/Enumerate *acceptance* tests run for the first time** (they
build a whole `Engine`, so they'd never gotten past `Database.__init__` raising
`NotImplementedError` before now): those acceptance tests' `FakeProcRunner`
scripts predated this item's real `airmon-ng` spawn and had no `"airmon-ng"`
entry, so every one of them `KeyError`'d the moment `reserve()` actually ran.
Fixed by adding a no-rename-announcement `"airmon-ng"` script entry to each
(`tests/test_capture_acceptance.py`, `tests/test_discovery_acceptance.py`,
`tests/test_enumerate_acceptance.py`), matching the pattern `test_rf.py`
already established, plus guarding two `on_spawn` callbacks
(`_write_csv_on_spawn` in `test_discovery_acceptance.py`) that assumed they'd
only ever be called for `"airodump-ng"` and broke once `"airmon-ng"` became a
real, earlier spawn in the same test run. `tests/test_enumerate_acceptance.py`'s
own `_make_enumerator()` helper (bypasses `Engine`, constructs `Enumerator`
directly) also needed a `new_connection_scope` argument added — a real gap from
item 4's own constructor-signature change, not item 1's.

### 2. `Engine.shutdown()` [DONE]

Implemented exactly per the pinned TODO: cancel every active job
(`self._jobs.active_job_ids()` + `cancel()` each), wait up to
`SHUTDOWN_JOB_WAIT_TIMEOUT_S` (5.0s) for each to reach terminal, stop
`SightingBatcher` (final flush), release the adapter to managed mode, stop
`self.privilege` (after waiting for jobs, not before — a privileged job's own
cancellation path still needs `run_privileged` while being cancelled), close
the DB last. `tests/test_engine.py` (new): a real `Engine` + `FakeProcRunner`,
same style as the acceptance tests — one test starts a slow-scripted passive
Capture job, calls `shutdown()`, and asserts the job reached
`StopReason.CANCELLED`, the adapter was released to managed mode (a real
`airmon-ng stop` spawn observed via `on_spawn`), `self.privilege`'s real
keepalive thread (started via a module-level-patched `subprocess.run`, same
technique as `tests/test_privilege.py`) actually stopped, and
`engine._db._conn` is closed (`sqlite3.ProgrammingError` on a query against
it); a second test covers the "nothing running" case (`shutdown()` on a
freshly-constructed `Engine` completes promptly with no errors). No bugs found
implementing this one — the ordering was already fully decided by the pinned
TODO and its inline comments.

### 3. `reconciliation.py` (ADR-0004) [DONE]

`reconcile_orphaned_processes()` and `_send_signal_unprivileged()` implemented
exactly per their pinned TODOs — the fingerprint-checked find/signal/mark-
terminal loop, and a plain `os.killpg` for the unprivileged path respectively.
`tests/test_reconciliation.py` (new), following the roadmap's own testing
strategy rather than inventing one: a real `JobRegistry`/`Database`, a real
short-lived unprivileged subprocess (`python3 -c "import time; time.sleep(30)"`,
`start_new_session=True`) with its real pid/pgid/fingerprint recorded via
`jobs.record_process(...)` (matching what a real `_drive` does), then
`reconcile_orphaned_processes(...)` called for real — asserts the process is
actually dead (`popen.wait()`/`.returncode`) and the job row is gone
(`find_stale_jobs() == []`); no mocking needed for that path, since the
unprivileged `send_unprivileged` really is `os.killpg` against this test's own
process, not faked. The one path that can't be exercised with real root on
this machine — `PermissionError` → `send_privileged` fallback — is tested by
monkeypatching `_send_signal_unprivileged` to raise `PermissionError`, with the
injected `run_privileged` stand-in still performing a real `os.killpg` itself
so the process's actual death is proof the routing works, not just that a mock
recorded a call (same spirit as `test_procutil.py`'s own privileged-handle
test). Also covered: no-stale-jobs (still publishes
`StartupReconciliationCompleted` with zero counts), a stale row with no
recorded process at all (pid/pgid `None` — nothing to kill, only the row to
clear), and a stale row whose recorded pgid no longer exists (row still
cleared, `processes_terminated` stays 0). No new bugs found in this slice
itself — the two already-documented ones below (privileged-signal routing, the
`--` fix) were caught earlier, by item 1's own work, and the pinned TODO
already had both fixes baked in; this slice just implemented it literally.

**Load-bearing bug already found and fixed earlier, don't reintroduce it**:
signaling a privileged (root-owned) process group requires routing through
`run_privileged` (Unix signal permission is UID-based, not parent/child-based),
and the real `/usr/bin/kill` binary needs `--` before a negative PGID
(`kill -15 -- -<pgid>`) — omitting it silently exits 0 without signaling
anything. Both fixed in `procutil.py`'s shipped code; `reconciliation.py`'s
implementation (above) carries the same fix. See `procutil.py`'s
`_RealProcHandle` docstring for the full story.

Real hands-on validation of this module's *privileged* path (a real root-owned
orphan, actually killed via real `sudo`) is still unverified — same caveat as
every other Phase 2 item's real-`sudo` path (see the constraints paragraph
above). Not blocking: the routing logic itself is genuinely tested (see
above), and full end-to-end proof needs the user's own real-terminal `sudo`
session, same as item 5.

### 4. Give each job-driver thread its own SQLite connection [DONE]

`persistence/db.py`'s `Database._resolve_path`/`_connect`/`new_connection_scope`
and `ConnectionScope` (`__init__`/`close`), plus `persistence/sighting_batch.py`'s
`SightingBatcher.start`/`.stop`/`._flush_loop`, are implemented per their
already-pinned TODOs. Engine wiring, each `_drive()`'s own connection-scope
usage, and `jobs.py`'s `repo:` override parameter were already in place from
before this slice — see git history for exactly what landed when. Full suite:
**120 passed, 0 failed** (114 from finishing this item's own scope + fixing the
item-1 test-fixture gap above, plus 6 new tests: 5 for `ConnectionScope`/
`new_connection_scope` in `tests/test_persistence_db.py`, 1 genuine concurrency
stress test in `tests/test_crack_acceptance.py`).

`":memory:"` (this project's own test-database convention) is rewritten in
`_resolve_path` to a uniquely-named SQLite shared-cache URI
(`file:aircommand-<uuid>?mode=memory&cache=shared`) — verified empirically that
a bare `sqlite3.connect(":memory:")` gives each connection its own private,
disconnected database, exactly backwards from what `new_connection_scope()`
needs. A real file path passes through unchanged.

**A second real bug was found by writing the concurrency stress test the
original task asked for, not by reasoning about the design in the abstract**:
the shared-cache URI above uses SQLite's table-level locking (not WAL's normal
MVCC), because `PRAGMA journal_mode=WAL` against a shared-cache in-memory
database silently downgrades to `'memory'` mode. Table-level lock contention
raises `SQLITE_LOCKED` ("database table is locked"), a *different* error class
from `SQLITE_BUSY` — `PRAGMA busy_timeout` only ever retries `SQLITE_BUSY`;
`SQLITE_LOCKED` under shared-cache mode is normally cleared via SQLite's
separate unlock-notify API, which Python's stdlib `sqlite3` module doesn't
implement. Confirmed directly with a throwaway script (N threads, each its own
connection onto one shared-cache in-memory database, writing concurrently):
`"database table is locked"` reproduces reliably even with `busy_timeout=5000`
already set. Fixed with `_RetryingConnection` (`persistence/db.py`), a thin
`sqlite3.Connection` subclass used as every connection's `factory=` that
retries `execute`/`commit` on `"locked"` `OperationalError`s for up to 5s. This
is a **test-only concern in practice**: production never passes `":memory:"` as
a real `db_path`, so it never enters shared-cache mode and never hits
`SQLITE_LOCKED` this way — a real file-backed WAL connection's ordinary
`SQLITE_BUSY` contention was already covered by `busy_timeout`, and the retry
wrapper is a harmless no-op there. Without this fix, the stress test
(`test_concurrent_crack_jobs_all_complete_with_distinct_results_and_no_sqlite_errors`)
failed reliably, proving the fix does real work rather than just looking
plausible.

**A third, smaller bug found the same way**: `tests/test_jobs.py`'s
`test_wait_for_terminal_blocks_until_another_thread_marks_terminal` predated
this item and had its background thread call `registry.mark_terminal(job_id)`
directly against the *main*-connection repo (`check_same_thread=True`, which
this item made a real, enforced constraint) from a different thread — silently
raising `sqlite3.ProgrammingError` inside that thread on every run, caught only
as a `PytestUnhandledThreadExceptionWarning` (not a failure) because the test's
own assertion (`elapsed >= 0.2`, no upper bound) happened to still hold even
though the write never actually completed and `wait_for_terminal` was
timing out its full 5s rather than genuinely being woken. Fixed by having that
background thread open its own `ConnectionScope`, matching the contract every
real driver thread now follows.

### 5. Re-check Capture's handshake-detection assumption against real output — not categorically blocked, not yet done

No longer blocked on missing tools/hardware — `aircrack-ng` is installed and a
real monitor-mode-capable adapter is plugged in (see the constraints paragraph
above). Still not done, and still not something a headless session can pick up
unattended: it needs the user driving it hands-on, in a real terminal (not
Claude Code's `!`-prefixed inline shell, which has no TTY for `sudo` to prompt
through — see the constraints paragraph), against a Target network they
nominate themselves (Capture is gated — `engine.targets.add(...)` or whatever
the eventual GUI/CLI surfaces first). See the flag in Phase 1's `capture.py`
entry — the exact `aircrack-ng -b <bssid> -w /dev/null <cap_path>` invocation
was researched, not run for real.

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
