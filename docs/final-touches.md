# Final touches — what's left before v1 is actually finished

Phase 1 (core engine), Phase 2 (real subprocess/hardware code), and Phase 3
(GUI) are all complete per `docs/roadmap.md`: 201 tests pass (194 headless via
`FakeProcRunner` as of Phase 3's own completion, plus 7 added since while
fixing the real-hardware bugs item 2 below found — see its own entry), and an
independent review pass (2026-09-20) re-verified the Phase 3 GUI commits
against `docs/design/gui-structure.md` line by line, reproduced and re-fixed
the one flaky test found, and found no spec drift or security gaps. One stale
comment (`aircommand/core/reconciliation.py`) was corrected as part of that
pass.

Nothing below is a design question. Everything here is either a small
mechanical gap or a hands-on validation step — there is no more "should this
be built" left to decide before v1, only "does it actually work."

## 0. There is no way to launch AirCommand yet — do this first [DONE]

Found while first drafting this doc, not previously flagged anywhere: `App`
(`aircommand/gui/app.py`) was a fully-implemented `ctk.CTk` subclass that
nothing in the codebase ever constructed and called `.mainloop()` on — no
`aircommand/__main__.py`, no `[project.scripts]` entry, no launcher anywhere.
The 194 tests never needed one (they construct `App`/panels directly and
drive `pump._tick()` by hand instead of a real mainloop, which is correct for
headless testing), so the gap was invisible until someone actually tried to
run the app. This had blocked every item below — there's no "run the real
GUI" without it.

**Fixed** (dispatched per CLAUDE.md's model tiering — the interface was
already fully pinned, so this was routine implementation, not a design
decision): `aircommand/__main__.py` (new) parses `--adapter` (required),
`--db-path`/`--work-dir` (both defaulted per
`docs/design/core-gui-boundary.md`'s own Usage sketch —
`~/.aircommand/aircommand.db` and `~/.aircommand/work`, created if missing),
constructs `App(...)`, and calls `.mainloop()`. Also registered as a console
script (`pyproject.toml`'s `[project.scripts]`,
`aircommand = "aircommand.__main__:main"`). `docs/usage.md`'s "Launching"
section now shows the real command (`python -m aircommand --adapter <name>`,
or `aircommand --adapter <name>` once installed) instead of the old interim
`python -c` snippet.

Verified: all 194 existing tests still pass unchanged; `--help` exits
cleanly; a real launch (`timeout 5 .venv/bin/python -m aircommand --adapter
wlan0`) gets past `App` construction into the blocking sudo-dialog/mainloop
as expected, rather than crashing.

## 1. Re-check Capture's handshake-detection assumption against real output

`docs/roadmap.md` Phase 2 item 5. The `aircrack-ng -b <bssid> -w /dev/null
<cap_path>` invocation `capture.py` uses to detect a completed handshake was
researched against the aircrack-ng manual and community docs, but never run
against a real `.cap` file. Needs: real `aircrack-ng` installed (it already
is, per the roadmap), a real capture file with and without a genuine
handshake in it, run by hand in a real terminal.

What to check: does `"handshake)"` really appear in stdout exactly the way
assumed, does `-b` really suppress the interactive network-selection prompt
for an unambiguous single-BSSID capture, does the exit code/stdout shape hold
up across a couple of real access points (not just one).

**Partially answered 2026-10-07, while debugging the Cancel hang (ADR-0011)
— two real problems CONFIRMED, this item is NOT closed:**

- **Confirmed**: `-b` does suppress the interactive network-selection prompt
  for an unambiguous single-BSSID capture — but it suppresses the *entire*
  per-network summary table along with it, not just the prompt.
  `parse_aircrack_handshake_check`'s `"handshake)"` text only appeared, in
  testing, in that table — and only when `-b` was *omitted* (letting
  `aircrack-ng` show its own selection menu). With `-b` given and matching
  exactly one BSSID (every real call `capture.py` makes), `aircrack-ng`
  skips straight to attempting that AP instead of printing the table.
  **Not yet confirmed against a REAL captured handshake** (only against
  synthetic `.cap` files without one, and a hand-crafted synthetic 4-way
  handshake that `aircrack-ng` itself didn't recognize as EAPOL data for
  reasons not root-caused — see ADR-0011's own Consequences) — but if it
  holds, `parse_aircrack_handshake_check` may never see its expected text
  via the exact invocation this codebase uses, regardless of the next bullet.
- **Confirmed**: real `aircrack-ng` rejects `/dev/null` outright as a `-w`
  dictionary file (`ERROR: Processing dictionary file /dev/null (No such
  file or directory)` — it needs a seekable regular file, a character
  device doesn't qualify) and falls through to "Please specify a dictionary
  (option -w)" without attempting to crack or list anything further.
  `/dev/null` was always meant as a "we don't actually want to crack, just
  detect" placeholder — it doesn't work that way against the real binary.

**Resolved 2026-10-08, against a REAL captured handshake (own network, own
device join/leave/rejoin) — see `docs/adr/0012` for the full writeup.** Both
suspicions above held, plus a second, independent bug found only once the
first was fixed enough to expose it:

- `-b <bssid>` (not `/dev/null`) is what suppresses the summary table —
  confirmed on the real handshake file both ways. Dropped from `capture.py`'s
  invocation; confirmed safe because `airodump-ng`'s own `--bssid` filter
  already guarantees exactly one network per `.cap` file.
- `parse_aircrack_handshake_check`'s old `"handshake)" in output` substring
  check would have false-positived on a real `"(0 handshake)"` table row
  (confirmed on real `.cap` files with zero handshakes) the moment the table
  became reachable. Fixed to require the parsed count to be > 0.
- The heavier redesign (EAPOL frame parsing, bypassing `aircrack-ng`'s CLI)
  this item previously expected turned out NOT to be needed — the real fix
  was two small, surgical changes. See ADR-0012's Decision/Considered
  options for why.

Still needs the real GUI re-run on real hardware to confirm a found
handshake now actually shows up in the Capture panel — see item 2's own
checklist entry below.

## 2. Drive the real GUI end-to-end, for real

Once item 0 exists: launch AirCommand for real, with your real sudo password,
against a real monitor-mode-capable adapter, against a network **you
personally own and administer** (per ADR-0001 — this is a hard requirement,
not a suggestion). Concretely:

- [ ] Sudo dialog: enter your password, confirm it accepts a correct one and
      retries cleanly on a wrong one.
- [x] Discovery & Targets tab: confirm real networks populate the table (not
      just your own — any nearby beacon, per CONTEXT.md's "Discovery is open
      to any Network"), signal/channel/encryption columns look sane, "Add as
      Target" works against a network you own. **Done — found and fixed three
      independent, stacked real-hardware bugs to get here (none visible to any
      headless test run): a sudo credential-cache bug that made every
      privileged call fail regardless of password correctness; a literal
      invalid CLI flag (`--write-csv` isn't real airodump-ng syntax); and
      Discovery's own driver loop silently going inert whenever airodump-ng's
      stdout stalled under `sudo`, which it does indefinitely on this
      hardware. Full writeup: `docs/adr/0008-driver-loops-stop-depending-on-stdout.md`.
      "Add as Target" itself not yet separately re-confirmed after this fix —
      worth a quick real click-through, though nothing about this fix touched
      that path.**
- [ ] Pause / New Session (ADR-0010, added 2026-10-07; only ever run against
      `FakeProcRunner` and a real Tk window, never real hardware): click
      Pause and confirm it reads "Pausing…" for about a second and then
      "Resume Discovery"; confirm Resume keeps the table; click Pause then
      New Session and confirm the table clears and refills with only what is
      in range (and that it does NOT re-run `airmon-ng`/restart NetworkManager
      mid-session: watch for your wifi dropping, which should not happen). Also
      confirm clicking Resume while a Capture holds the radio shows a status-bar
      error instead of doing nothing.
- [x] Target Actions tab: passive Capture against your own Target; confirm a
      real Handshake gets captured and shows up in the panel and the Crack
      tab's picker. Try deauth-assisted Capture too — confirm the
      confirmation dialog actually appears, and that every burst shows up
      live in the Audit Log tab, not just at the end. **Done — confirmed by
      the user on real hardware 2026-10-08: both passive and deauth-assisted
      Capture worked.** Three real blockers had to be found and fixed first:
      `capture.py` reading its own `.cap` file at the wrong path (fixed
      2026-10-07, see `docs/roadmap.md`'s entry that day — every real
      handshake capture would have raised `FileNotFoundError` mid-loop and
      silently reported plain `COMPLETED` with no Handshake recorded);
      handshake detection never actually working against real
      `aircrack-ng` output (item 1 above); and a failed deauth burst being
      indistinguishable from a real one in both the audit log and the GUI.
      The last two are `docs/adr/0012` — `DeauthFired`/`AuditLogEntry` now
      carry `succeeded`/`error_detail`, surfaced in both `CapturePanel` and
      `AuditLogView`.
- [ ] Cancel button: start a deauth-assisted Capture, click Cancel mid-run,
      confirm the UI actually returns to its idle state (this was unit-tested
      against `FakeProcRunner`, but never against a real, slower-to-terminate
      `aireplay-ng` process). **Re-opened 2026-10-07, after the entry below
      turned out to be wrong**: the "already fixed by ADR-0008, confirmed"
      claim two updates below only ever covered `aireplay-ng`'s own stdout-
      stall shape, via a standalone harness — not a PASSIVE Capture (no
      `aireplay-ng` involved at all), not the real GUI Cancel button, and not
      real hardware. The user hit exactly that gap for real: a passive
      Capture's Cancel button did nothing, no error, UI stuck on
      "Capturing…" forever, only fixable by closing the app. Root-caused and
      fixed — see `docs/adr/0011-capture-cancel-hang-on-real-aircrack-ng.md`
      for the full writeup: real `aircrack-ng`, confirmed via `strace`, hangs
      indefinitely (a worker thread calls a raw `exit()` instead of
      `exit_group()`/`pthread_exit()`, never waking the main thread's own
      wait) when `capture.py`'s periodic handshake-check runs against a
      `.cap` file that doesn't exist yet or isn't fully written — a race
      `Capture._drive`'s own 4-second check interval does not reliably avoid
      on real hardware. `Capture._drive`'s cancellation loop blocked on that
      one-shot check's own `handle.lines()`, so it could never get back
      around to notice `token.is_cancelled()` either. Fixed: a guard (skip
      the check until the `.cap` file has a full pcap header) plus a bounded,
      per-tick-cancellable wait around the check (same `ProcHandle.poll()`
      idiom ADR-0008 already established), plus Cancel now escalates
      SIGTERM→SIGKILL on the main `airodump-ng` handle too (it wasn't
      verifying the process actually died before this). Verified: new
      `FakeProcRunner` acceptance tests for the guard/cancellation/escalation
      paths, AND a new real-subprocess test file
      (`tests/test_capture_real_subprocess.py`) that spawns the actual
      installed `aircrack-ng` against a real empty `.cap` file — confirms the
      hang is real, and that the fix detects and kills it. **What's STILL not
      re-confirmed, and what this checklist item still needs**: the real GUI
      Cancel button, a real `airodump-ng`/`aireplay-ng` process, real
      hardware (`wlx24050f7d7ae0`) — none of the above needed sudo or wifi
      hardware, since `aircrack-ng` itself never runs privileged.
      **Two more real, confirmed-but-unfixed bugs surfaced while verifying
      this, both about whether handshake detection ever actually SUCCEEDS
      (separate from Cancel) — see item 1 below, now substantially updated,
      and ADR-0011's own Consequences section for the full detail.**
- [ ] Enumerate panel: join the Target's network via your OS's normal wifi
      settings first (AirCommand doesn't do this itself, by design — see
      `enumerate.py`'s own docstring), then run Enumerate and confirm real
      hosts/ports come back. Also try it *without* joining first, to confirm
      `EnumerationFailed` actually surfaces a real, readable error instead of
      leaving the panel stuck on "Enumerating…".
- [ ] Crack tab: pick the real Handshake from above, pick a real wordlist,
      run a crack to completion (use a small wordlist that you know contains
      the real key, to get a `Found` result in reasonable time, not just an
      `Exhausted` one).
      **The underlying engine path is now confirmed this way, just not yet
      through the actual GUI tab**: a real Engine + real `Crack`, driven
      directly (not through `App`/the Crack panel), against a real captured
      handshake and a real wordlist, through the real installed
      `hcxpcapngtool` and `hashcat` — found the real password. Two real bugs
      were found and fixed doing this: a silent hang on one non-UTF-8 byte in
      hashcat's own stdout (ADR-0014), and hashcat's -m 22000 needing a real
      `hcxpcapngtool` conversion step it was never given (ADR-0015) — before
      ADR-0015, Crack could not find a real password at all, regardless of
      ADR-0014's own fix. Still open: clicking through the actual Crack tab
      for this same confirmation.
- [ ] Status bar: confirm the privilege indicator looks right; if you can
      arrange it, let sudo's cache lapse (or kill the keepalive) and confirm
      the "LOST" warning actually shows up.
- [ ] Restart-after-crash path: `kill -9` the app while a deauth-assisted
      Capture is running, relaunch, confirm the reconciliation banner and the
      Audit Log's "may be missing firings" note both appear correctly.

If any of these surface a real bug, that's expected — this is exactly what
"validated only headlessly" means. Fix it the normal way (debugging a
non-obvious issue stays with the strongest model per CLAUDE.md, not a
subagent dispatch), then record what changed in `docs/roadmap.md` or a new
ADR if it was a real design tradeoff, matching this project's own convention
("a decision that only exists in one session's chat transcript doesn't exist
for the next one").

## 3. Verify dual-band Discovery on real hardware (ADR-0013)

Built 2026-10-08 on branch `feat/dual-band-discovery` against `FakeProcRunner`
and a real Tk window. Only the first box below has been run on real hardware.
Same ground rule as item 2: your own network, and only listening
(Discovery is passive).

- [x] The adapter's bands are read correctly (2026-10-08, no sudo):
      `RadioController("wlx5c628b9faa9d", ...).supported_bands()` returned
      2.4 GHz and 5 GHz from the real `iw phy phy3 info`.
- [ ] `python scripts/smoke_test_bands.py --adapter <your adapter>` (asks for
      your sudo password, drops your other wifi connection while it runs):
      for each of 2.4 GHz, 5 GHz and both, every network heard must be on a
      channel inside the requested band(s), and the live `airodump-ng`
      command line it prints must show `--band bg`, `a` and `abg`. A band with
      nothing heard is reported INCONCLUSIVE, not PASS. Watch in particular for
      `--band abg` failing on disabled or DFS channels (it would show as
      Discovery dying with an error), and for `--band bg` finding the same
      networks the old no-flag scan did.
- [ ] GUI on the real adapter: launch and confirm nothing scans and your wifi
      stays up until you click Start Discovery; confirm the dropdown offers all
      three choices; Start on "2.4 + 5 GHz" and confirm 5 GHz rows appear with
      the Band column filled in; Pause, switch to "5 GHz", Resume, and confirm
      the old 2.4 GHz rows stay while new 5 GHz rows arrive; New Session on a
      different band clears the table; the dropdown is greyed out while
      scanning.
- [ ] Failure path: with the adapter unplugged (or `iw` renamed), launch and
      confirm the dropdown offers 2.4 GHz only and the status bar says why.
- [ ] Still open from ADR-0006, unchanged: 5GHz deauth-assisted Capture. The
      new adapter makes it testable, but nothing here tests it.

## Once the above is done

That's it — there's no further roadmap phase written down. v1 is "done" when
items 1, 2 and 3 above have actually been carried out, for real, on your own
hardware. Item 0 no longer blocks either — you can start whenever you're
ready.
