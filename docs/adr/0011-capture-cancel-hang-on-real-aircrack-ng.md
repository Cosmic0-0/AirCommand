# Capture's one-shot handshake check must never block cancellation

**Status: implemented, fixed, needs real-hardware re-confirmation (see Consequences).**

A real-hardware bug, reported 2026-10-07: starting a passive Capture against a
real Target and clicking Cancel did nothing. The UI stayed on "Capturing…"
forever, the Start buttons never came back, and the only way out was closing
the whole program. `docs/roadmap.md`'s 2026-10-07 entry had previously closed
this out as "already fixed by ADR-0008, confirmed" — that confirmation only
ever exercised a standalone harness and `FakeProcRunner`, never the real GUI
Cancel button against a real `aircrack-ng`. We decided the earlier "fixed"
claim was wrong and re-opened it; see `docs/final-touches.md` item 2, which
already said as much before this investigation started.

## Root cause

`Capture._drive` (`capture.py`) periodically runs a one-shot check while a
Capture is running:

```python
check_handle = self._proc.spawn(
    ["aircrack-ng", "-b", str(target.bssid), "-w", "/dev/null", str(cap_path)],
    privileged=False)
output = "\n".join(check_handle.lines())   # BLOCKING
```

`check_handle.lines()` blocks until the spawned process's stdout reaches EOF.
The surrounding `while True:` loop's own `token.is_cancelled()` check only
runs at the *top* of each iteration — so if this one call never returns, the
loop can never get back around to notice a cancellation. This is exactly the
shape ADR-0008 already fixed for the main `airodump-ng` handle (a loop must
not block on a spawned tool's behavior it doesn't control) — it was never
applied to this second, one-shot handle.

**Confirmed directly against the real, installed `aircrack-ng` binary (1.7),
not assumed from docs or community reports:**

```
$ timeout 10 aircrack-ng -b AA:BB:CC:DD:EE:01 -w /dev/null empty.cap < <(sleep 30)
...
read(file header) failed: Success
Opening empty.cap

Quitting aircrack-ng...
[hangs — killed by timeout after 10s, exit 137]
```

`aircrack-ng` hangs *indefinitely* — well past any plausible "just slow"
threshold — whenever the `.cap` file it's given does not yet contain a
complete 24-byte pcap global header (confirmed by binary search on file
size: 1–23 bytes and a missing file both hang; 24 bytes and up do not). This
reproduces both on a genuinely missing file and on a 0-byte one. `strace -f`
on the hang shows why: `aircrack-ng` spawns a reader thread that detects the
bad header, writes its error line, and then calls the **raw `exit(0)` **
syscall (not `exit_group`/`pthread_exit`) — which only terminates that one
LWP. The main thread is left parked in a `futex` wait that nothing ever
wakes, so the process never actually exits despite having already printed
"Quitting aircrack-ng…". This is a real bug in `aircrack-ng` itself, not
something this codebase can fix — but a job driver depending on an external
tool's correctness here is exactly the assumption ADR-0008 already told us
not to make.

**Why this is easy to hit on real hardware, and essentially impossible to
hit under `FakeProcRunner`:** `DEFAULT_HANDSHAKE_CHECK_INTERVAL` is 4
seconds. `airodump-ng` needs to enter monitor mode, start capturing, and
actually flush its `-w` output to disk before the real `<bssid>-<job_id>-01.cap`
file has a complete header — on real hardware there is no guarantee that
happens inside the first 4-second window, especially right after Capture
starts. The very first handshake check can easily run against a file that
doesn't exist yet, hitting this hang on essentially every real Capture.
`FakeProcRunner`'s `on_spawn` hook (`_write_cap_file_on_spawn` in
`tests/test_capture_acceptance.py`) writes the whole fake `.cap` file
synchronously, before the fake `airodump-ng` handle is even returned — so
the file is always "fully written" from Capture's very first check, which is
exactly why a green `FakeProcRunner` suite never caught this.

## Decision

Two changes to `Capture._drive`'s handshake-check step, both scoped to this
one call site:

1. **Guard**: skip the check this round if `cap_path` doesn't exist yet or is
   smaller than a complete pcap global header (24 bytes). Avoids ever
   spawning `aircrack-ng` against input known to trigger the hang above, in
   the common case. Purely an optimization — (2) below is what actually
   fixes the user-visible bug.
2. **Bounded, cancellable wait**: replace the blocking
   `"\n".join(check_handle.lines())` with a poll loop on the same tick
   cadence the rest of `_drive` already uses (`ProcHandle.poll()`, the same
   idiom ADR-0008 established for the main handle). It checks
   `token.is_cancelled()` every tick — so Cancel stays responsive even while
   this one-shot check is in flight, regardless of why `aircrack-ng` might be
   slow or stuck — and `kill()`s the check process on cancellation or after a
   generous timeout (30s; `aircrack-ng` against a real, possibly large
   capture is not guaranteed fast, so this is a safety net for an
   *unattended* hang, not the thing that makes Cancel responsive — the
   per-tick cancellation check is).

Also fixed in the same pass, found while re-reading this exact loop for the
above (not the Cancel bug itself, but the same "Done means… no process left
behind" requirement): `_drive` called `handle.terminate()` on the main
`airodump-ng` process exactly once (on both the cancel path and the
handshake-found path) and never verified it actually exited or escalated to
`kill()`. `ProcHandle.kill()`'s own docstring already warns the
aircrack-ng-suite doesn't always honor SIGTERM — `procutil.py`'s
`terminate_process_group()` already uses a SIGTERM → grace period → SIGKILL
policy for startup orphan cleanup (ADR-0004); `_drive` now uses the identical
policy (3s grace, matching `terminate_process_group`'s own default) for the
live Cancel path too, on both termination sites.

## Considered options

- **Keep the blocking call, add a watchdog thread that kills it after N
  seconds.** Rejected for the same reason ADR-0008 rejected the equivalent
  option for `airodump-ng`'s stdout: still structurally depends on blocking
  as the normal path, just bolts a rescue mechanism onto it. More moving
  parts (a second thread) for less responsiveness than polling on the
  existing tick cadence gives for free.
- **Give the one-shot check its own always-non-blocking state machine,
  overlapping with the main loop's own airodump-ng polling** (spawn it,
  return to the main loop immediately, check progress on a later tick).
  Rejected — `_collect_bounded`'s own nested poll loop already gets
  cancellation responsiveness down to one tick (~0.5s), which is well within
  "a couple of seconds"; a fully overlapping state machine is meaningfully
  more complex for a difference that isn't user-visible.
- **(Chosen) A small, local bounded-wait helper, reusing the exact
  `ProcHandle.poll()` idiom ADR-0008 already established, plus the exact
  SIGTERM→grace→SIGKILL policy `procutil.py` already established for
  orphans.** No new abstraction — both fixes apply an already-decided
  pattern to one call site that was missed, matching this project's own
  stated preference (ADR-0009's Considered Options) for a local fix over new
  shared machinery when the local fix fully closes the gap.

## Consequences

- `Capture._drive` gains two new constructor-overridable tunables
  (`handshake_check_timeout`, default 30s; `cancel_grace_period`, default
  3s), matching the existing pattern for `handshake_check_interval`/
  `tick_interval` — overridable per-instance purely for test injectability.
- **Same shape, not yet fixed here, out of scope for this change**:
  `Discovery._drive` and `Crack._drive` call `handle.terminate()` once on
  cancellation with no escalation to `kill()`, same as `Capture._drive` did
  before this fix. Neither has a second, one-shot subprocess inside its loop
  the way Capture does, so neither shares the *hang* bug — but both share
  the "orphan if the tool ignores SIGTERM" gap. Worth a follow-up pass
  applying the same escalation helper there, not bundled into this change
  per the user's own scoping ("Out of scope: … unrelated refactors").
- **Two more real bugs found while verifying this, confirmed but NOT fixed
  here — both about handshake-detection *correctness*, not Cancel,
  genuinely out of scope for a Cancel-focused change, and both squarely what
  `docs/final-touches.md` item 1 ("re-check Capture's handshake-detection
  assumption against real output") already flagged as never having been run
  for real:**
  1. **Confirmed**: real `aircrack-ng` rejects `/dev/null` as a `-w`
     dictionary file outright (`ERROR: Processing dictionary file /dev/null
     (No such file or directory)` — it requires a seekable regular file, a
     character device doesn't qualify) and falls through to "Please specify
     a dictionary (option -w)" without ever attempting to crack or (per
     finding 2 below) list a handshake. `/dev/null` was always a
     placeholder for "we don't actually want to crack, just detect" — it
     does not work for that purpose against the real binary.
  2. **Suspected, not confirmed** (synthetic `.cap` crafting hit a wall —
     see below): with `-b <bssid>` given and an *unambiguous* single-BSSID
     match, real `aircrack-ng` appears to skip straight to attempting that
     AP (crack attempt, or `"Packets contained no EAPOL data…"`) without
     ever printing the per-network summary table that carries the
     `"… WPA (N handshake)"` text `parse_aircrack_handshake_check` greps
     for — that table only appeared, in testing, when `-b` was *omitted*
     (ambiguous-selection path). If that holds against a real captured
     handshake too, `parse_aircrack_handshake_check` would never see its
     expected text pattern via the exact invocation `capture.py` uses,
     regardless of finding 1. **This needs a real hardware session with a
     genuine captured handshake to confirm or deny** — a hand-crafted
     synthetic WPA handshake `.cap` (built and tested in this session,
     structurally valid per `tcpdump -v`: correct LLC/SNAP, EAPOL header,
     and all four Key Information values) was not recognized by
     `aircrack-ng` as containing EAPOL data at all, for reasons not
     root-caused (possibly a real-world detail of frame shape this
     synthetic attempt didn't reproduce) — not worth more synthetic-crafting
     effort instead of just using a real capture, matching what
     `docs/final-touches.md` item 1 already said was needed.
  3. If finding 2 holds, `capture.py`'s handshake-detection mechanism needs
     a real redesign (not a tweak) — not undertaken here; this is a design
     decision, deliberately left for its own session per CLAUDE.md's model
     tiering (architecture stays with the strongest model, not something to
     rush inside an unrelated Cancel fix).
- Tests: `tests/test_capture_acceptance.py` gets new `FakeProcRunner`-driven
  cases for the guard, for cancellation while the one-shot check is
  in-flight, and for kill-escalation when the main handle ignores
  `terminate()`. A new standalone real-subprocess test
  (`tests/test_capture_real_subprocess.py`) spawns the actual installed
  `aircrack-ng` against a real empty `.cap` file through the real
  `SubprocessRunner`/`_RealProcHandle` (no sudo, no wifi hardware needed —
  `aircrack-ng` itself never needs root) and confirms both halves directly:
  that the hang is real, and that `Capture`'s new bounded-wait mechanism
  detects and kills it within a bounded time against the *real* hung
  process, not a scripted fake.
- **Still needs, and does NOT yet have**: the user re-running the real GUI
  against real hardware (the `wlx24050f7d7ae0` adapter) and confirming
  Cancel now returns the UI to idle and leaves no orphaned process — see
  `docs/final-touches.md` item 2's own checklist entry, updated alongside
  this ADR to say exactly what is and isn't re-verified yet.
