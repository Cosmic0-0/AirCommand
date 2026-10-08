# A non-UTF-8 byte from a spawned tool must not kill its drain thread

**Status: implemented, fixed, confirmed against real hashcat output (see Consequences for what this does NOT fix).**

A real-world bug, reported 2026-10-08: starting a Crack job (handshake +
wordlist selected, button pressed) hung silently — no crash, no error in the
GUI, nothing. The terminal running AirCommand showed a background thread had
died with:

```
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xd4 in position 115:
invalid continuation byte
```

## Root cause

`procutil.py`'s `_RealProcHandle` spawns two daemon threads per handle —
`_drain_stdout`/`_drain_stderr` — whose whole job is to keep a spawned tool's
pipes drained so it can never deadlock trying to write (see the class's own
"STDOUT/STDERR DEADLOCK NOTE"). Both read with a plain `for line in
self._popen.stdout:` / `self._popen.stderr:`. `SubprocessRunner.spawn()`
constructs every `Popen` (both the privileged and unprivileged branch) with
`text=True`, which decodes as **strict** UTF-8 by default — one invalid byte
anywhere in the tool's output raises `UnicodeDecodeError` *inside the drain
thread*, before it ever reaches its own sentinel/EOF handling. For stdout,
that means `_stdout_queue` never gets its `None` sentinel, so `lines()`
(`crack.py`'s `for line in handle.lines():`, the only real stdout consumer in
this codebase) blocks on `queue.get()` forever — not cancellable either, since
`token.is_cancelled()` is only checked *between* queue items, and the
generator is parked inside a blocking call. The underlying tool (hashcat, in
the reported case) keeps running the entire time, orphaned.

**Confirmed directly against the real, installed `hashcat` (v6.2.6), against
the user's actual handshake capture and wordlist — not assumed, not
synthetic:**

```
$ hashcat -m 22000 <real .cap> wordlist.txt --status --status-json \
    --outfile out --outfile-format 2
...
Hashfile '<path>' on line 1 (\xd4\xc3\xb2\xa1\x02): Separator unmatched
Hashfile '<path>' on line 2 (): Separator unmatched
...
No hashes loaded.
```

`0xd4 0xc3 0xb2 0xa1` is the standard little-endian pcap global-header magic
number — the first four bytes of *every* `.cap` file. hashcat's `-m 22000`
does not accept a raw pcap/pcapng capture as input at all (confirmed: no
`pcap`/`pcapng` string anywhere in the installed `hashcat` binary, no mention
in `--help`, and `hcxtools` — the package that provides the `hcxpcapngtool`
converter real-world WPA cracking workflows use — isn't installed on this
machine). Given the raw `.cap` file directly, hashcat tries to parse it as a
line-oriented hash-per-line text file, fails on line 1, and — because its own
error message embeds the rejected line's bytes verbatim — echoes the pcap
magic number's own non-UTF-8 byte straight back onto stdout. That byte is
what reaches `_drain_stdout`'s strict-UTF-8 decode and kills the thread.

Reproduced independently via a standalone script spawning the exact real
`hashcat` command through a real `text=True` `Popen` and iterating
`popen.stdout`: raises `UnicodeDecodeError('utf-8', b"Hashfile '...' on line 1
(\xd4\xc3\xb2\xa1\x02): Separator unmatched\n...", 115, 116, 'invalid
continuation byte')` — byte-for-byte and position-for-position the same
traceback originally reported. The `hashcat` process (confirmed via `ps`) was
still running, writing further rejection lines into a pipe nobody was left to
drain, for as long as the hang was observed — exactly the orphan the original
report suspected.

**Why this isn't hashcat-specific, and isn't specific to this one input
shape**: `_RealProcHandle` is the one seam every driver's spawned tool goes
through (`procutil.py`'s own module docstring), privileged or not — hashcat
and aircrack-ng (unprivileged) and airodump-ng and aireplay-ng (privileged,
via `SudoSession.run_privileged`, which constructs its `Popen` with the
identical `text=True` shape) all hit the same two drain threads. Any of them
echoing a raw byte string into otherwise-human-readable output is plausible
beyond this one case — a beacon's SSID is an arbitrary byte string with no
guarantee of valid UTF-8, and GPU/device name strings are vendor-supplied.
`enumerate.py`'s nmap invocation drains the same way (`b"".join(l.encode()
for l in handle.lines())`). None of this was hit before now purely because no
one had yet fed a tool input shaped to trigger it.

## Decision

`_RealProcHandle.__init__` now calls `self._popen.stdout.reconfigure(errors=
"replace")` and the same for `stderr`, before either drain thread starts.
`'replace'` substitutes U+FFFD for each undecodable byte instead of raising.
Fixed once, in the one shared seam, rather than at each of the four call
sites (`crack.py`, `discovery.py`, `capture.py`, `enumerate.py`) or in both of
the two `Popen`-constructing sites (`SubprocessRunner.spawn`,
`SudoSession.run_privileged`) that would otherwise need to independently
remember to pass the same `errors=` kwarg to `subprocess.Popen` itself —
`reconfigure()` on the already-open `TextIOWrapper` _RealProcHandle already
holds does it without touching either constructor, so the two Popen call
sites keep the "must produce the exact same shape" invariant their own
comments already assert, instead of gaining a second thing to keep in sync.

Every real consumer of this content already only wants readable diagnostic
text — JSON status lines (`parse_hashcat_status_line`), CSV rows
(`parse_airodump_csv_line`), nmap XML, stderr tails for a GUI hint
(`summarize_stderr`) — and every one of them already tolerates a
garbled/unparseable line by treating it as "nothing new this round" (e.g.
`parse_hashcat_status_line` returning `None` for a non-JSON line). None of
them need byte-exact content. Losing fidelity on a byte that was already
unreadable is a strictly better outcome than crashing the one thread that
exists to keep the pipe drained.

## Considered options

- **Catch `UnicodeDecodeError` inside the drain loop and keep iterating.**
  Rejected: `for line in file_object:` raising mid-iteration leaves the
  underlying buffered reader's position in an unspecified state for a strict
  decoder — there's no documented guarantee the *next* `next()` call resumes
  cleanly rather than re-raising on the same spot or skipping data. Rejected
  for added complexity (a `while True: try/except` replacing the simple
  `for`) to reimplement what `errors="replace"` already does correctly as a
  single decoder setting.
- **Pass `errors="replace"` to `subprocess.Popen()` directly**, in both
  `SubprocessRunner.spawn`'s unprivileged branch and `SudoSession.
  run_privileged`. Rejected — see Decision above: two call sites to keep in
  sync instead of one, for a behavior that's about how `_RealProcHandle`
  reads, not about how the process is spawned.
- **(Chosen) `TextIOWrapper.reconfigure(errors="replace")` on
  `self._popen.stdout`/`stderr` inside `_RealProcHandle.__init__`, before
  either drain thread starts.** One seam, matches this module's own stated
  design ("the only seam through which core touches a subprocess"), no new
  abstraction.

## Consequences

- Both drain threads (`_drain_stdout`, `_drain_stderr`) now survive any
  single malformed byte from any spawned tool, on both the privileged and
  unprivileged path, confirmed by two new real-subprocess tests in
  `tests/test_procutil.py` (one per stream) that spawn a plain `python3`
  stand-in writing a non-UTF-8 byte straight at stdout/stderr — same idiom
  this file already uses for the stderr-pipe-deadlock test, so no new tool
  dependency for the regression test itself. Both were confirmed to actually
  catch the regression: reverting just the `reconfigure()` calls reproduces
  the *exact* `UnicodeDecodeError` byte/position shape from the original
  report against this synthetic input too, and both new tests fail against
  that reverted code.
- End-to-end confirmed against the real reported case: a real `Engine` +
  `Crack` run against the user's actual handshake capture and `wordlist.txt`,
  through the real installed `hashcat`, completes in ~2s instead of hanging,
  publishes exactly one `CrackResult`, and that result is durably readable
  back via `list_results()`.
- **Orphan cleanup, checked but NOT changed**: a hashcat process orphaned by
  a hang like this one is already covered by `reconciliation.py`'s existing
  unprivileged fallback path (ADR-0004) — `terminate_process_group` tries
  `send_unprivileged` (plain `os.killpg`) first and only falls back to the
  sudo path on `PermissionError`; `_send_signal_unprivileged`'s own docstring
  already names "a leftover hashcat process" as exactly this case. Already
  exercised by an existing test
  (`test_reconcile_terminates_a_real_unprivileged_orphan_and_clears_its_job_row`,
  `tests/test_reconciliation.py`, using `JobKind.CRACK`) against a real
  spawned process — no gap found, no code change made here.
- **A second, separate, more fundamental bug was found while reproducing
  this, confirmed but deliberately NOT fixed here — same "confirmed but out
  of scope" posture ADR-0011 used for its own two handshake-detection
  findings:** `crack.py` passes the raw airodump-ng `.cap` file straight to
  `hashcat -m 22000`. Real hashcat's `-m 22000` does not parse raw
  pcap/pcapng at all — confirmed above (`--hash-info -m 22000` shows the
  expected input is the `WPA*01*...` hex-encoded hc22000 text format, not a
  capture file; no `pcap` string anywhere in the installed binary). That
  format is normally produced by `hcxpcapngtool`, part of the separate
  `hcxtools` package — **not installed on this machine, and never invoked
  anywhere in this codebase** (`grep -rl hcxpcapngtool` on `aircommand/`
  finds nothing). The practical consequence: **Crack cannot currently find
  the correct key for any real captured handshake, regardless of this fix**
  — hashcat loads zero hashes and exits (`"No hashes loaded."`, confirmed
  exit code 255) every time, for every `.cap` file, independent of wordlist
  content. This fix changes that failure from an infinite silent hang into a
  fast, correctly-classified `Exhausted()` / `StopReason.ERROR` result
  (confirmed end-to-end, see above) — a real improvement on its own — but it
  does **not** make Crack functionally able to recover a real password.
  Fixing that needs a real scoping decision (adding `hcxtools` as a new
  system dependency, and a conversion step — in `Capture` after a handshake
  is minted, or in `Crack` before spawning hashcat) that belongs in its own
  session, per CLAUDE.md's model-tiering rule for architecture decisions, not
  bundled into a hang fix.

  **Resolved in [ADR-0015](0015-hcxpcapngtool-conversion-step.md)**: `hcxtools`
  installed, the conversion step added inside `Crack`, confirmed end to end
  against a real capture and wordlist — hashcat now finds the real password.
- **Still needs, and does NOT yet have**: nothing further for the hang
  itself (fully confirmed above, on real hardware, against the real reported
  case) — but the hc22000-format gap just above means this bug report's own
  "once fixed, confirm it finds the correct key" acceptance criterion cannot
  be met by this change alone. That's a distinct, larger piece of work,
  intentionally not started here.
