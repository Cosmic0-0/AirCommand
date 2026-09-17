# Startup reconciliation kills orphaned capture/crack processes from a prior crash

If AirCommand crashes while a privileged tool (airodump-ng, aireplay-ng) is running, Linux reparents that child to init and it keeps running — worst case, an active deauth loop with nobody left to audit-log its firings (ADR-0001 requires every firing logged; the process that would log them is what crashed). We decided AirCommand should find and terminate such orphans automatically at the next launch, rather than leave them running silently. Every job-driver thread records its subprocess's PID/PGID and a cmdline fingerprint (tool name + interface) into the jobs table the moment it spawns. At startup, once privilege is re-established (`Engine.reconcile_startup()`, called right after `SudoSession.start()` succeeds — killing a root-owned process needs the same privilege that started it), any job row still marked RUNNING is checked: if a process still holds that PGID and its `/proc/<pid>/cmdline` still matches the recorded fingerprint, it's sent SIGTERM, escalating to SIGKILL after a grace period, then the job row is marked `StopReason.INTERRUPTED_PRIOR_SESSION`.

## Why

Matches ADR-0001's own posture: this project is deliberately narrow and audit-conscious, so a silently-running unattended deauth loop is a real gap, not a cosmetic one. The fingerprint check exists specifically because PIDs/PGIDs get reused — a blind `kill -9 <old-pgid>` after a reboot could otherwise signal a completely unrelated process that happened to land on the same PGID.

## Considered Options

- **Leave orphans running silently, rely on the user noticing (e.g. via `ps`) and killing them by hand.** Rejected — conflicts directly with the project's own audit/authorization discipline; a tool built around "every Action is accounted for" shouldn't have an unaccounted-for failure mode as its default crash behavior.
- **Kill by PID/PGID alone, no fingerprint check.** Rejected — unsafe after a reboot or heavy process churn, where PID/PGID reuse means "the process currently at this PGID" is no longer a reliable identity.

## Consequences

Cleanup stops *further* unaudited deauth firings; it cannot retroactively account for firings that happened between the crash and the next launch, since the process that would have written those audit rows is exactly what crashed. A `CaptureStopped` event with `reason=INTERRUPTED_PRIOR_SESSION` on a deauth-assisted job should be read by any audit reviewer (GUI or otherwise) as "an unknown, unlogged number of deauth bursts may have fired here" — see `docs/design/core-gui-boundary.md`. This also imposes a hard ordering requirement on Engine's startup sequence: `reconcile_startup()` must run after `privilege.start()` succeeds, not during `Engine.__init__` — reconciliation was originally sketched at construction time and had to move once this decision made the privilege dependency concrete.
