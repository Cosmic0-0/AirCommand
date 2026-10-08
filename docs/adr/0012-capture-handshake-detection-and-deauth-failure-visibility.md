# Capture: fix real handshake detection, surface deauth burst failures honestly

**Status: implemented, fixed, confirmed on real hardware 2026-10-08 (see Consequences).**

Two real-hardware bugs reported together, 2026-10-08, on the Target Actions
tab's Capture panel: passive capture against the user's own network (join /
leave / rejoin to force a real handshake) showed no handshake, with no way to
tell if one had actually landed; deauth-assisted capture didn't visibly kick
anyone off, with no indication of what happened or whether it even tried.
`docs/final-touches.md` item 1 and `docs/adr/0011`'s own Consequences section
had already flagged the handshake-detection half as unconfirmed pending a
REAL captured handshake — this is that session.

## Root cause — handshake detection (Thread A)

Both things ADR-0011 suspected but couldn't confirm without a real handshake
are now confirmed, against a genuinely captured one (own network, own device
forced through a join/leave/rejoin cycle; `aircrack-ng <file>` with no flags
showed `WPA (1 handshake)` in the summary table, the real ground truth these
findings are checked against):

1. **`-b <bssid>`, not just `/dev/null`, is what breaks this.** With `-b`
   matching exactly one BSSID — every real call `capture.py` makes — real
   `aircrack-ng` skips the entire per-network summary table (where
   `"N handshake)"` lives) and goes straight to "1 potential targets",
   regardless of whether a real handshake is present. Confirmed both ways on
   the same real handshake file: `-b` given → table never appears, even with
   a real dictionary instead of `/dev/null`; `-b` omitted → table appears
   every time, correctly showing `WPA (1 handshake)`.
2. **`/dev/null` as `-w` is a red herring for this specific bug.** It does
   error (`ERROR: Processing dictionary file /dev/null (No such file or
   directory)`), confirming ADR-0011's own finding — but that error happens
   *before* packet reading and does not suppress the table. With `-b`
   dropped, the table prints regardless of whether `-w` is `/dev/null`, a
   real wordlist, or omitted. No dummy wordlist file was ever needed.
3. **A second, independent bug, found only once (2) made the table
   reachable at all**: `parse_aircrack_handshake_check`'s old check —
   `"handshake)" in output` — is a plain substring test, so it matches
   `"0 handshake)"` exactly as much as `"1 handshake)"`. Real output
   legitimately prints `(0 handshake)` for a network with none yet
   (confirmed on multiple real `.cap` files, including several of
   `capture.py`'s own prior runs against the user's real Target). Once (1)
   is fixed and the table becomes reachable, this would have false-positived
   on every single check until a handshake exists, not just once one does.
4. **Exit code carries no signal either way**: 0 regardless of handshake
   presence (confirmed on both a real 1-handshake file and a real
   0-handshake file). Stdout text parsing remains the only option.
5. **Dropping `-b` is safe for this codebase's actual capture files**:
   `airodump-ng`'s own `--bssid <target.bssid>` filter (capture.py's
   airodump-ng invocation) already guarantees the `.cap` file it writes
   holds exactly one network. Confirmed directly against 6 real `.cap` files
   produced by actual `Capture` runs on this machine: every one shows
   exactly "1 potential targets", auto-selected ("Choosing first network as
   target.") — never the interactive multi-network prompt dropping `-b`
   would otherwise risk.
6. **Unexpected, found while writing a regression test for this fix**:
   dropping `-b` also appears to eliminate the ADR-0011 Cancel-hang itself
   for the missing-file and 0-byte-file cases specifically — confirmed via
   the exact same bounded-thread-join idiom that ADR-0011 test used to
   originally demonstrate it. With `-b`, both cases still hang exactly as
   ADR-0011 found; without it, both now return promptly ("No networks
   found, exiting."). This is NOT taken as proof the hang can never
   recur without `-b` for some other input shape — `aircrack-ng` is still a
   C program with its own bugs, and `-w /dev/null` still errors on every
   call — so ADR-0011's guard (`_PCAP_GLOBAL_HEADER_BYTES`) and
   `_collect_bounded`'s bounded-wait/kill mechanism are both kept exactly as
   they were, as cheap, general-purpose defense-in-depth for a one-shot
   subprocess call this infrequent, not removed on the strength of one
   narrower finding.

## Decision — handshake detection: a fix, not the redesign ADR-0011 flagged as likely

ADR-0011 flagged finding 2 above as probably needing a real redesign (e.g.
parsing EAPOL message 1-4 frames directly from the `.cap`, sidestepping
`aircrack-ng`'s CLI entirely) if it held against a real handshake. It does
hold — but the actual fix needed turned out to be much smaller than that
worry anticipated:

- `capture.py`: drop `-b <bssid>` from the check invocation. Keep
  `-w /dev/null` (confirmed harmless for this purpose — see finding 2).
- `parse.py`: `parse_aircrack_handshake_check` now extracts the handshake
  count via `\((\d+)\s+handshakes?\)` and requires it to be > 0, instead of
  a bare substring test.

No EAPOL-frame-parsing redesign, no new dependency, no change to what
`capture.py` spawns beyond removing one flag. The heavier redesign stays
off the table unless a *future* finding reopens it — this one didn't.

### Considered options

- **Supply a real (empty or placeholder) wordlist file instead of
  `/dev/null`, keep `-b`.** Rejected — tested directly (finding 1): `-b`
  alone suppresses the table regardless of the wordlist. Would not have
  fixed anything on its own.
- **Parse EAPOL frames directly from the `.cap`, drop `aircrack-ng` from
  the detection path entirely.** This is the redesign ADR-0011 flagged as
  likely necessary. Rejected now that real data is in hand: the actual fix
  (drop one flag, fix one regex) fully resolves both confirmed bugs. A
  frame-parsing redesign would trade a working, minimal fix for a new
  dependency surface (hand-rolled 802.11/EAPOL parsing) for no remaining
  problem it would solve. Worth revisiting only if a *future* real-hardware
  finding shows the current fix insufficient.
- **(Chosen) Drop `-b`, fix the substring check.** Both changes are
  independently confirmed necessary and sufficient against real captured
  output, not synthetic guesses.

## Root cause — deauth burst failure visibility (Thread B)

Unlike Thread A, this was never root-caused before this session — two leads
from reading `capture.py`'s `deauth_pacer` block, checked in order:

1. **Confirmed, and was the real gap**: `self._proc.spawn([...]).wait()`
   discarded `aireplay-ng`'s exit code and stderr entirely.
   `AuditLogEntry`/`DeauthFired` were written/published unconditionally, so
   a failed injection (driver/permission/channel issue) was *indistinguishable*
   from a real burst in both the audit log and every GUI subscriber — there
   was no way, short of re-running `aireplay-ng` by hand, to tell which had
   happened.
2. **Checked and ruled out for this specific target, not a general
   guarantee**: Protected Management Frames (802.11w/PMF) make classic
   deauth frames cryptographically ineffective regardless of whether
   injection itself succeeds — a protocol limitation, not a bug. Checked
   directly against this target's own real beacon frames (hand-parsed the
   RSN information element's capabilities field from two independently
   captured real `.cap` files, one from 2026-09-29 and one from today) —
   MFPC and MFPR both False on both. PMF is OFF on this specific network, so
   it does not explain the reported symptom here. Kept as a documented
   caveat (capture.py's own comment, and this ADR) for future "deauth did
   nothing" reports against a *different* target, not built into the
   product as an automated check — no PMF capture/storage/display feature
   was requested or added; that would be new scope, not this fix.

Sanity-checking injection working on this adapter at all (a live deauth
burst against a real client, confirmed by watching it actually disconnect)
is explicitly **not done in this session** — it requires transmitting real
RF frames and a physical device to watch, which needs the user's own hands
at the keyboard and the target client, not something achievable from this
tool-calling session. The exit-code/stderr fix below makes that
confirmation meaningful (a real failure will now be visible rather than
silently reported as success) but does not replace it — see Consequences.

## Decision — deauth: capture and surface the real outcome, still log every attempt

- `capture.py`'s deauth_pacer block now captures `aireplay-ng`'s exit code
  and, on failure, a `summarize_stderr()` hint — the exact pattern
  `CaptureStopped`/`DiscoveryStopped`'s own `error_detail` already
  established, applied to this one-shot call too.
- `AuditLogEntry` and `DeauthFired` both gain `succeeded: bool = True` and
  `error_detail: Optional[str] = None`. **Every attempt is still logged and
  published, success or failure** — ADR-0001's "every firing, no
  exceptions" covers the attempt, not only a confirmed-successful one;
  weakening that guarantee was never on the table. The fields are additive
  and defaulted, so no existing call site's meaning changes.
- `audit_log` gains the same two columns (`SCHEMA_VERSION` 1 → 2), migrated
  in place via a `PRAGMA table_info`-guarded `ALTER TABLE` —
  `CREATE TABLE IF NOT EXISTS` alone is a no-op against the table this
  machine's own existing database already has from before this change.
- GUI: `CapturePanel`'s live burst counter and `AuditLogView`'s rows both
  now distinguish a failed attempt from a real burst, instead of reporting
  them identically.

### Considered options

- **A separate `DeauthFailed` event type, alongside `DeauthFired`.** Left
  open as a possibility by the originating brief. Rejected — `DeauthFired`
  already means "an attempted firing happened, no exceptions" per its own
  docstring; splitting it into two types would mean every existing and
  future subscriber needs to register for both to see the full picture,
  for no benefit over one type with an added field that defaults to
  "nothing to report." Simpler, and keeps ADR-0001's "no exceptions"
  framing literally true at the type level too.
- **Build an automated PMF-detection-and-warning feature** (capture the
  RSN IE during Discovery, store it on `Target`, surface it in the GUI).
  Rejected as out of scope for this fix — the brief asked to check whether
  PMF explains the reported symptom here (it was checked and ruled out,
  by hand, for this one target), not to ship a permanent detection
  feature. A real feature request, if wanted later, is a separate scoped
  task.
- **(Chosen) Extend `DeauthFired`/`AuditLogEntry` additively; migrate the
  schema in place; keep the PMF finding as documentation, not code.**

## Consequences

- Tests: `tests/test_parse.py` gains fixture-based unit tests for
  `parse_aircrack_handshake_check` using the real observed stdout shape
  (genericized BSSID/SSID — the actual captured `.cap` files and their real
  home-network BSSID/SSID are deliberately **not** committed to this repo;
  see below). `tests/test_capture_acceptance.py` gains a `FakeProcRunner`
  case for a failed deauth burst. `tests/test_persistence_db.py` gains a
  migration test that builds a genuine pre-ADR-0012 `audit_log` table by
  hand and confirms both the schema upgrade and the pre-existing row's data
  survive it. `tests/test_capture_real_subprocess.py`'s three existing
  hang-reproduction tests keep `-b` deliberately (see finding 6 above and
  each test's own updated docstring — they document the raw upstream bug
  and `_collect_bounded`'s general kill mechanism, not capture.py's current
  argv) and gain one new real-subprocess test confirming the new no-`-b`
  shape exits promptly, not hangs, against the one boundary case
  `capture.py`'s own header-size guard lets through.
- **Deliberately not committed to this repo**: the real `.cap` files this
  session's ground-truth testing used (the user's own home network's real
  handshake capture, plus several real `Capture`-produced work-dir files)
  live only on the user's own machine, never staged or committed — committing
  them would put a real, identifying BSSID/SSID into permanent git history
  on a repo with a GitHub remote and existing PRs. Test fixtures use the
  real *shape* of the output with placeholder values instead.
- **Confirmed 2026-10-08, by the user, on real hardware**: both a real
  passive Capture and a real deauth-assisted Capture against their own
  Target worked, re-running the actual GUI (not just this session's
  standalone `aircrack-ng`/`aireplay-ng` hand-testing or the FakeProcRunner
  suite). This closes the "still needs real-GUI re-confirmation" gap this
  ADR originally left open — `docs/final-touches.md` item 2's Target
  Actions checklist entry updated accordingly.
- `docs/final-touches.md` item 1 updated to reflect this as resolved; item
  2's Target Actions checklist entry updated with what's fixed versus what
  the user still needs to confirm on real hardware.
