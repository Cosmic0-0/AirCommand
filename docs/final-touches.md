# Final touches — what's left before v1 is actually finished

Phase 1 (core engine), Phase 2 (real subprocess/hardware code), and Phase 3
(GUI) are all complete per `docs/roadmap.md`: 194 tests pass, all headless
(`FakeProcRunner`, no real subprocess/root/hardware), and an independent
review pass (2026-09-20) re-verified the Phase 3 GUI commits against
`docs/design/gui-structure.md` line by line, reproduced and re-fixed the one
flaky test found, and found no spec drift or security gaps. One stale comment
(`aircommand/core/reconciliation.py`) was corrected as part of that pass.

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

## 2. Drive the real GUI end-to-end, for real

Once item 0 exists: launch AirCommand for real, with your real sudo password,
against a real monitor-mode-capable adapter, against a network **you
personally own and administer** (per ADR-0001 — this is a hard requirement,
not a suggestion). Concretely:

- [ ] Sudo dialog: enter your password, confirm it accepts a correct one and
      retries cleanly on a wrong one.
- [ ] Discovery & Targets tab: confirm real networks populate the table (not
      just your own — any nearby beacon, per CONTEXT.md's "Discovery is open
      to any Network"), signal/channel/encryption columns look sane, "Add as
      Target" works against a network you own.
- [ ] Target Actions tab: passive Capture against your own Target; confirm a
      real Handshake gets captured and shows up in the panel and the Crack
      tab's picker. Try deauth-assisted Capture too — confirm the
      confirmation dialog actually appears, and that every burst shows up
      live in the Audit Log tab, not just at the end.
- [ ] Cancel button: start a deauth-assisted Capture, click Cancel mid-run,
      confirm the UI actually returns to its idle state (this was unit-tested
      against `FakeProcRunner`, but never against a real, slower-to-terminate
      `aireplay-ng` process).
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

## Once the above is done

That's it — there's no further roadmap phase written down. v1 is "done" when
items 1 and 2 above have actually been carried out, for real, on your own
hardware. Item 0 no longer blocks either — you can start whenever you're
ready.
