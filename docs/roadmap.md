# Roadmap

Written for a fresh session picking this project up cold. Read CLAUDE.md, CONTEXT.md,
`docs/adr/`, and `docs/design/core-gui-boundary.md` first — this doc assumes all of
that and doesn't repeat it. It tells you what's done, what's left, what order to do it
in, and — for the parts that aren't actually scoped yet, just stubbed — says so
explicitly rather than pretending a TODO comment is a finished design.

## Current state

Committed, in order: core scaffolding + GUI shell + ADRs/design doc — `domain.py` +
`events.py` — the headless Discovery flow end to end (schema, `NetworkRepository`,
`JobRepository`, `SightingBatcher`, `JobRegistry`, `RadioController`'s in-memory
arbitration, the airodump-ng CSV parser, `Discovery` itself) — `Allowlist` +
`TargetRepository`. 76 tests passing. Nothing has touched a real subprocess, real
`sudo`, or real hardware yet — every slice so far runs against `FakeProcRunner`.

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
- **Mint-restricted types** (`Target` done, `Handshake` next): the repository mints
  (imports the `_*_MINT` sentinel, constructs the frozen dataclass directly), the
  facade stays a thin delegation. **Flagged below**: the original `capture.py` TODO
  sketch shows the opposite (facade mints from a raw `row`) — resolve this
  deliberately when you scope Capture, don't just copy one pattern blindly.
- **"Preserve the original on upsert"**: `Network.first_seen` and `Target.date_added`
  both mean "the first time this became true," not "the last time this row was
  written" — excluded from the `UPDATE` SET list on purpose, with a comment saying so.
  Watch for the same shape elsewhere (e.g. would a `Handshake`'s `captured_at` ever
  need this treatment? Probably not, since handshakes aren't upserted — but check).
- **`DurableEvent` fires only for a real write.** `Allowlist.remove()` on a bssid
  that was never a Target publishes nothing — there's no state transition to
  describe. Apply the same check-before-publish discipline anywhere an operation
  might be a no-op.
- **Exception safety in driver threads.** `Discovery._drive` wraps spawn-through-loop
  in `try/finally` so `rf.release()`/`jobs.mark_terminal()` always run, even if
  parsing a malformed line from a real external tool raises mid-loop — otherwise the
  RF reservation leaks forever and the app can never run another Discovery/Capture/
  Enumerate without a restart. Every other `_drive` (Capture, Enumerate) needs the
  same guarantee; nothing about it is Discovery-specific.
- **Thin facade, fat repository.** Facade methods are 1-4 line delegations; the
  repository holds the actual SQL and constructs the actual domain object. Keep
  following this rather than letting SQL creep into `capture.py`/`crack.py`/etc.
- **Record real decisions where the project already records them, not in a new
  place.** When you resolve one of the gaps flagged below (or find a new one),
  don't just fix the code and move on — if it's a genuine architectural
  tradeoff (multiple real options, a reason one was chosen), write it as an ADR
  in `docs/adr/`, matching ADR-0001 through 0004. If it's smaller than that,
  update this roadmap or `docs/design/core-gui-boundary.md` in place. Either way
  it needs to be committed (see above) — a decision that only exists in one
  session's chat transcript doesn't exist for the next one.

## Phase 1 — core engine, headless-testable (do these via FakeProcRunner, same as Discovery)

### 1. `capture.py` — next, and the biggest slice so far

The only `Handshake` mint site; drives two tools instead of Discovery's one; is
where ADR-0001's audit requirement actually bites (`DeauthFired` must be written to
`audit_log` *before* the event publishes — the TODO already shows this ordering,
keep it). Depends on `Allowlist` (done) and needs `HandshakeRepository` +
`AuditLogRepository` implemented in `persistence/db.py` (currently bare stubs).

**Not actually scoped yet — resolve before implementing, don't guess:**

- **How is a handshake actually detected?** The stub's `parse_airodump_handshake_flag(csv_block: str) -> bool` in `parse.py` assumes airodump-ng's own CSV output carries a handshake indicator. This is an *external* fact (about aircrack-ng-suite's real behavior), not an internal one — you can make real progress on it without hardware, by reading aircrack-ng's actual documentation/source/community references (that's research, not "reading the stub cold" — it can genuinely reduce the uncertainty). The more common real-world approach is checking the *captured `.cap` file itself* (e.g. via `aircrack-ng` against it, or inspecting EAPOL message pairs), not the live CSV stream. But full confidence may not be reachable until Phase 2, when real hardware exists to check actual tool output against — if research alone doesn't settle it, implement your best-researched version now, mark it explicitly (in a code comment and in this doc) as "unverified against real tool output, re-check in Phase 2," and don't let implementation pressure turn a marked assumption into a silently-trusted fact.
- **Mint site for `Handshake`**: repository-mints (matching `Target`'s precedent) or facade-mints (matching the existing `capture.py` TODO's own pseudocode)? Pick one deliberately and say why in a comment, the way `TargetRepository`'s docstring does.
- Needs realistic `FakeProcRunner` fixtures for *two* tools in one flow: airodump-ng (streamed lines, same shape as Discovery's) and aireplay-ng (fire-and-wait, probably no meaningful `.lines()` output to parse — mostly just needs `.wait()`).

### 2. `crack.py`

Depends on a real `Handshake` — since `Handshake` is mint-restricted, you can't
hand-construct one for an isolated Crack unit test, so this naturally wants Capture
done first and its own test fixtures reused to produce a real `Handshake` to feed
Crack's tests. Needs `parse_hashcat_status_line`/`HashcatStatus` in `parse.py` —
verify hashcat's actual `--status-json` field names/shape before implementing
rather than guessing at JSON keys. `CrackResultRepository` needs implementing
(same mint-in-repository question as above, for `CrackResultRow` — though note
`CrackResultRow` itself isn't mint-restricted, so this one's lower-stakes).

### 3. `enumerate.py`

Independent of Capture/Crack (only needs `Allowlist`), but has a real open design
gap, not just an implementation detail:

**Not actually scoped yet — this one needs the user, don't resolve it solo:** the
`_drive` TODO references a `target_subnet` that's never defined anywhere. A `Target`
only carries a wifi `bssid`/`ssid` — nmap needs an IP/subnet, and nothing in the
current design says how to get from one to the other. Unlike Capture's gap above,
this one doesn't need hardware or research — it's a genuine design fork, answerable
today by thinking it through, but it's bigger than it first looks and touches
security-sensitive territory, so ask rather than deciding solo:

- **Option A**: AirCommand never joins a network itself. It assumes the operator
  already associated to the target network through their OS's normal wifi settings
  before clicking "Enumerate," and just reads whatever subnet the currently-active
  interface is on (stdlib-only — no new domain fields, no credential storage).
  Smaller scope, matches this project's minimal-scope posture, but means Enumerate
  silently finds nothing if the operator hasn't manually joined first.
  Recommended for exactly that reason.
- **Option B**: AirCommand manages the join itself — which means storing a
  network's credentials somewhere (a new field on `Target`? a separate secret
  store?) and giving `RadioController`'s `AdapterMode.MANAGED` a real "associate to
  this specific network" operation it doesn't have today. Real scope increase
  across `domain.py`/`allowlist.py`/`rf.py`, and "should this tool store wifi
  passwords at rest" is a security-posture decision worth an ADR of its own, not a
  quiet default.

Whichever way the user goes, write it down — as an ADR if it's Option B (real
tradeoff, real scope), as a note in this doc if it's Option A (confirms an
assumption, doesn't add anything new).

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
