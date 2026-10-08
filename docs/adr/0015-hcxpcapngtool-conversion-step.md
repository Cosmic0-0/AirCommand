# Crack converts a handshake to hc22000 before handing it to hashcat

**Status: implemented, fixed, confirmed end to end against a real capture and the real password.**

ADR-0014 fixed Crack's silent hang but left a bigger gap confirmed and
documented, not fixed: hashcat's `-m 22000` cannot parse a raw `.cap` file at
all, so Crack could never recover a real password regardless of that fix. The
user asked for that gap to be closed: install `hcxtools` and add the missing
conversion step.

## What was confirmed before writing any code

- `hcxtools` (providing `hcxpcapngtool`) installed via `apt` (`universe`
  repo, version 6.2.7-2build3 on this machine) — a new runtime dependency,
  not bundled with `hashcat` or the `aircrack-ng` suite.
- Ran real `hcxpcapngtool -o <out> <real .cap>` against the user's actual
  handshake capture: exits 0, extracts exactly one hash, and that hash's own
  AP-MAC field (`623761a16f51`) matches the Target's BSSID
  (`62:37:61:A1:6F:51`) exactly — confirming `capture.py`'s existing
  assumption ("airodump-ng's own --bssid filter... guarantees this .cap file
  holds exactly one network") holds for a real sample, not just by reading
  the comment.
- Fed that converted file + the real `wordlist.txt` to real `hashcat`: found
  the real password (**`IDFK2021`**) on the first try. Confirmed this is
  genuinely the end-to-end fix the original bug report asked for ("confirm a
  run actually completes and reports the correct found key").
- Re-ran the exact same hash a second time: hashcat printed `All hashes
  found as potfile and/or empty entries!` and exited **without writing
  `--outfile` at all** — a second, independent, real bug (see below).
- Checked `hcxpcapngtool --help` directly rather than assuming flag names
  (this repo's own standing convention): `-o <file>` is the hc22000 output
  flag; there is no explicit `--filterbssid`-style flag, only
  `--max-essids` (default 1, selected by an internal ranking heuristic, not
  an explicit BSSID match).

## Decision

`Crack._drive` (`crack.py`) now does, in order, before ever spawning
hashcat:

1. **Convert.** Spawn `hcxpcapngtool -o <hash_file> <handshake's .cap>`,
   privileged=False (it only reads a file `capture.py` already made
   world-readable; no root needed). Waited on with a new bounded-wait helper
   (`_await_conversion`), not a blind `.wait()` — `hcxpcapngtool` is a
   one-shot, non-interactive pcap parser with a much lower hang-risk profile
   than `aircrack-ng`/`airodump-ng` (no curses UI, no pty, no network
   dependency, and ADR-0011's confirmed hang was never reproduced here), but
   this codebase's own established lesson (ADR-0008, ADR-0011) is to never
   trust an external tool's liveness unconditionally just because it looks
   safe. `DEFAULT_CONVERSION_TIMEOUT` is 30s — generous relative to the real
   run observed while writing this (well under a second against a ~200KB
   real capture).
2. **Filter.** Keep only the converted file's lines whose AP-MAC field
   matches the Handshake's own `bssid` (`filter_hc22000_lines_by_bssid`,
   `parse.py`) — defense in depth, not the only thing enforcing this (see
   above: a real single-BSSID capture already converts to a single-network
   hash file), but CONTEXT.md is explicit that cracking's authorization is
   *inherited from the Handshake's own Target* — Crack itself should never
   blindly trust whatever a conversion step happened to produce, only the
   one hash this Handshake is actually for.
3. **Gate.** Only if filtering leaves at least one line, and the job hasn't
   been cancelled, spawn hashcat — now pointed at the filtered hc22000 file,
   not the raw `.cap`. Conversion failure, timeout, cancellation, or an
   empty filter result all skip hashcat entirely and fall into the
   **already-existing** `Exhausted()` / `StopReason.ERROR` path (ADR-0009's
   established bucket for "something went wrong before a real attempt
   happened") — no new `CrackOutcome` variant needed; `handle` (the
   variable `_drive`'s `finally` block reads) simply never gets assigned in
   any of these cases, which that `finally` block already handles correctly
   today.

Where the conversion lives: inside `Crack`, not `Capture`/`Handshake`
(domain.py gains no new field). It's a fact about what hashcat specifically
needs, not a fact about the Handshake itself — a different cracker backend
(or `aircrack-ng`'s own dictionary mode, which reads `.cap` directly) would
need no such step at all.

## A second real bug, found while verifying this end to end, and fixed here too

Re-running the exact same hash a second time doesn't re-attempt it — hashcat
caches cracked `hash:plaintext` pairs in a cross-session potfile
(`~/.local/share/hashcat/hashcat.potfile`), keyed by the hash itself, not by
anything this codebase controls. Confirmed directly (see above): the second
run exits having found nothing new to do, **without writing `--outfile`** —
the only artifact `_drive`'s `finally` block reads to decide `Found` vs
`Exhausted`. Left unfixed, every re-crack attempt against the same Handshake
after its first success would silently report `Exhausted()` forever,
regardless of how obviously correct the wordlist is — a confusing, far-off
failure mode that would have been very hard to connect back to "it already
found this once" without already knowing to look in `~/.local/share/hashcat/`.

Fixed by adding `--potfile-disable` to the hashcat invocation: forces a real
attack (and a real `--outfile` write on success) every time, matching the
single detection mechanism this module actually has, and keeps a
system-wide file this codebase doesn't own from silently affecting its
results. Cost: a re-crack of an already-cracked Handshake takes full time
again instead of an instant potfile hit — accepted, since this is an
auditing tool prioritizing correct, inspectable results over cracking speed,
and the whole point of a repeat run is to prove the result again, not take
a shortcut around proving it.

## A third real bug, found while implementing this, fixed for Crack and flagged elsewhere

While rewriting Crack's hashcat spawn call, checked whether its
`record_process` fingerprint (`f"hashcat {cap_file_path}"`, used by
`reconcile_orphaned_processes`'s `is_process_group_alive` check, ADR-0004)
actually matches a real process's `/proc/pid/cmdline`. **It doesn't**:
confirmed directly against a real spawned process — real argv is
`["hashcat", "-m", "22000", <path>, ...]`, so `"-m 22000"` sits between the
tool name and the path, and `f"hashcat {path}"` is never a substring of the
real (space-joined) cmdline. `is_process_group_alive` would return `False`
for a genuinely orphaned hashcat process every time, meaning
`reconcile_orphaned_processes` would never actually kill it — it would just
mark the stale job row terminal and leave the real process running.

Checked the same shape against the other three drivers' own fingerprints —
**all three have the identical bug**:
- Discovery: `f"airodump-ng {adapter}"` vs real argv
  `airodump-ng --band bg --write <prefix> <adapter>` — not a substring.
- Capture: `f"airodump-ng {bssid} {adapter}"` vs real argv
  `airodump-ng -c <ch> --bssid <bssid> -w <prefix> <adapter>` — not a
  substring.
- Enumerate: `f"nmap {bssid}"` — the bssid doesn't even appear in nmap's own
  argv (which only has the subnet) — never a substring, by construction.

This means **ADR-0004's entire orphan-cleanup promise has been silently
non-functional for every driver**, not just Crack, since reconciliation was
first written. `terminate_process_group`'s unprivileged/privileged-fallback
mechanism itself is sound (confirmed working, ADR-0014) — the fingerprints
handed to it are what's wrong.

Fixed here **only for Crack's own two spawn calls** (the conversion step and
the hashcat run), since this change already rewrites that exact code: both
now use the bare output-file path as the fingerprint, which — unlike a
prefixed `"<tool> <path>"` form — is guaranteed to appear as its own argv
token in the real cmdline (confirmed the same way: a real spawned process's
`/proc/pid/cmdline` does contain the bare path verbatim).

**Not fixed here, deliberately**: Discovery, Capture, and Enumerate's own
fingerprints. Fixing those means editing three more files for a concern this
session wasn't asked to audit — same scoping discipline ADR-0011 already
applied to its own adjacent findings. This needs its own pass, and should
probably include an automated check (e.g. a fingerprint-construction helper,
or a test that spawns the real tool and asserts its own fingerprint matches
its own cmdline) so this class of bug can't reappear silently per call site
the way it did here, four times, independently.

**Fixed in a follow-up pass, same session**: Discovery's and Capture's own
long-running `airodump-ng` fingerprints now use the bare, job_id-derived
prefix path (`csv_prefix`/`cap_prefix`) each already builds — a real,
standalone argv token, same reasoning as Crack's own fix above. Enumerate's
now uses the bare `subnet` (its `target.bssid` form could never have matched
in the first place — `bssid` never appears in nmap's own argv at all).
`tests/test_fingerprint_matches_real_cmdline.py` adds the automated check
this paragraph asked for: one real-process-backed test per driver, each
proving its new fingerprint is a genuine `/proc/pid/cmdline` substring and
its old one wasn't.

## Considered options

- **Run the conversion inside `Capture._drive`, store the converted path on
  `Handshake`.** Rejected: bakes a hashcat-specific input-format detail into
  the domain model and the database schema, for a fact that's true about
  *how Crack calls hashcat*, not about the Handshake itself. `Crack`-local
  keeps `Handshake` unchanged and keeps the concern next to the only code
  that has it.
- **Rely solely on `hcxpcapngtool`'s own `--max-essids=1` default instead of
  an explicit post-filter.** Rejected: `--max-essids` picks the
  highest-ranked ESSID by its own internal heuristic, not an explicit BSSID
  match — it happens to agree with the Target here because the capture is
  already single-network, but that's a property of the *input*, not a
  guarantee the tool itself makes. An explicit filter keyed to the
  Handshake's own `bssid` is a correctness property this codebase can
  actually state and test, not an incidental side effect of a ranking
  heuristic.
- **Leave hashcat's potfile enabled, and layer a secondary "was it already
  cracked" check (`--show`) on top.** Rejected: two different success-signal
  code paths (fresh `--outfile` vs potfile recall via `--show`) for one
  outcome, when `--potfile-disable` makes the existing single mechanism
  correct unconditionally, for a trivial, already-accepted-elsewhere speed
  cost (this project's own precedent: ADR-0009 already chose
  correctness/clarity over raw speed for Crack's success signal).
- **(Chosen)** Conversion step lives in `Crack`, BSSID-filtered explicitly,
  hashcat run with `--potfile-disable`, Crack's own fingerprints fixed in
  the same pass.

## Consequences

- `crack.py` gains a new constructor-overridable `conversion_timeout`
  (default 30s) and now also takes `tick_interval` (matching
  Discovery/Capture's existing pattern) — threaded through from `Engine` via
  `drive_tick_interval`, same as Discovery/Capture already are.
- New pure parser `filter_hc22000_lines_by_bssid` (`parse.py`), tested in
  `tests/test_parse.py` against the real line shape confirmed above.
- `tests/test_crack_acceptance.py`: all existing `FakeProcRunner` scenarios
  updated to script the new `hcxpcapngtool` spawn; four new scenarios cover
  the conversion step's own failure modes (wrong BSSID extracted, nonzero
  exit, timeout, cancellation mid-conversion) — all land on the existing
  `Exhausted()`/`ERROR` or `Aborted()`/`CANCELLED` paths, no new branching
  needed in the assertions themselves.
- New `tests/test_crack_real_subprocess.py`: real `hcxpcapngtool` against a
  real (empty) capture, confirming the "nothing to extract" case behaves as
  expected against the actual binary, and that `_await_conversion` really
  kills a real process on cancellation. Skips if `hcxpcapngtool` isn't
  installed, matching `test_capture_real_subprocess.py`'s own convention. A
  genuine end-to-end run against a real handshake is deliberately NOT a
  committed fixture — same "no secrets, no real passphrase checked into the
  repo" reasoning as `wordlist.txt` — so that proof is narrated above
  instead, verified by hand against the reporting user's own capture.
- `README.md`'s Requirements list gains `hcxtools` alongside `aircrack-ng`,
  `hashcat`, and `nmap`.
- Full suite: 336 passed (321 at ADR-0014, 333 after this ADR's own changes,
  336 after the Discovery/Capture/Enumerate fingerprint follow-up below).
- Cross-reference: this resolves the gap ADR-0014's own Consequences section
  flagged as confirmed-but-not-fixed ("Crack cannot currently find the
  correct key for any real captured handshake, regardless of this fix").
  That gap is closed as of this ADR.
- **Follow-up, same session**: the Discovery/Capture/Enumerate fingerprint
  bug flagged above as its own pass was done — see the "Fixed in a
  follow-up pass" note earlier in this document.
