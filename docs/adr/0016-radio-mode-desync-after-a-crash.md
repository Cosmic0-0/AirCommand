# RadioController's in-memory mode tracking can desync from real hardware

**Status: implemented, fixed, confirmed against the real, currently-affected adapter on the project owner's own machine.**

Reported: clicking Start Enumerate produces

```
OSError: [Errno 99] Cannot assign requested address
```

from `enumerate.py`'s `get_interface_subnet`, with a full traceback in the
terminal. `enumerate.py`'s own module docstring and CONTEXT.md's "Enumerate"
entry both already name the most obvious explanation — the operator never
joined the Target's network via their OS's normal wifi settings — and
`docs/final-touches.md` already had an open checklist item asking whether
`EnumerationFailed` actually surfaces a readable error for exactly this case
or leaves the panel stuck. Both were checked. Neither was the real cause.

## Root cause

`RadioController.__init__` (`rf.py`) sets `self._monitor_adapter = None`
unconditionally — "assume managed" — and nothing ever checks that against
the adapter's real, on-disk mode. The class's own prior comment already
named this as a known gap ("if the adapter's real mode was changed by
something outside this process... this class won't detect that; accepted...
not solved here").

**Confirmed directly on the reporting user's own machine, not reasoned about
in the abstract:**

```
$ iw dev wlx5c628b9faa9d info
Interface wlx5c628b9faa9d
	...
	type monitor
	channel 108 (5540 MHz), ...
```

No AirCommand process was running at the time. The real adapter — confirmed
via shell history to be the one AirCommand is actually launched with — was
genuinely sitting in monitor mode, left there by a prior AirCommand process
that never reached `Engine.shutdown()` (a crash or force-kill; this exact
machine's own `ADR-0014` hang is one plausible way that happens, though the
history doesn't pin down which specific run left it this way).

A *fresh* `RadioController` has no way to know that. When Enumerate calls
`reserve(AdapterMode.MANAGED, ...)`, `_ensure_mode` reads
`self._monitor_adapter is None` as proof "already managed, nothing to
switch" and returns immediately — skipping the real `airmon-ng stop` /
`systemctl restart NetworkManager` sequence entirely. `Enumerator._drive`
then tries to read an IPv4 address off an interface that is genuinely, still,
in monitor mode — which never has one, by definition, regardless of whether
the operator joined any network at all. The result is byte-for-byte the same
`OSError[Errno 99]` a genuinely-not-yet-joined network produces, for a
completely unrelated reason — which is exactly why the two already-named
explanations (never joined; GUI not surfacing the error) both looked
plausible and neither was it.

## Decision

`_ensure_mode` (`rf.py`) gains one new branch, checked only in the one
combination that can be wrong: `MANAGED` requested, `self._monitor_adapter`
reads `None`.

```python
elif not wants_monitor and self._real_adapter_is_in_monitor_mode():
    self._monitor_adapter = self._adapter
    self._stop_monitor_mode()
```

`_real_adapter_is_in_monitor_mode` runs `iw dev <adapter> info` (new parser:
`parse_iw_dev_type`, `parse.py`) and checks for a `monitor` type line —
unprivileged, a plain read, nothing like the airmon-ng/systemctl calls this
file already treats as real-world-affecting. Deliberately **not** cached:
unlike `supported_bands()`'s band cache (a fact that genuinely never changes
for a given adapter), the adapter's mode can change at any time for reasons
outside this process's control, so every `reserve(MANAGED, ...)` call that
still sees `self._monitor_adapter is None` re-checks reality — this
self-heals even from a *second*, later desync, not only the first one.
Tolerant of its own failure (`iw` missing, the interface renamed/gone) by
design: any failure there just preserves the pre-fix default rather than
raising a new way for `reserve()` to fail.

**Why this one branch, not a broader "always re-sync at every entry
point"**: an earlier version of this fix added the check unconditionally to
`reserve()`, `supported_bands()`, and `release_to_managed()`. That's a more
thorough fix in principle (it also closes the gap for a session that ends
without ever requesting `MANAGED` at all), but broke 54 tests across 11
files that construct a `FakeProcRunner` without an `"iw"` script entry,
since the sync check would fire unconditionally on every `reserve()` call —
including every Discovery/Capture-only test, which have nothing to do with
this bug. The narrower, single-branch fix above touches zero of those tests
(confirmed: all 24 pre-existing `test_rf.py` tests, plus the entire rest of
the suite, pass completely unmodified against it) because Discovery and
Capture only ever request monitor modes, never `MANAGED` — they were never
exposed to this bug in the first place.

## A second, related finding, fixed in the same pass

Confirmed while verifying the above would actually unblock Enumerate end to
end: fixing the stale-mode detection means `RadioController` now correctly
*attempts* the real mode switch, but `_stop_monitor_mode`'s `systemctl
restart NetworkManager` call returning success only means the systemd
service is active — not that the interface has already re-associated to a
wifi network and obtained a fresh DHCP lease, which can take a few more real
seconds. `Enumerator._drive` used to call `get_interface_subnet` exactly
once, immediately, with no tolerance for that gap — meaning even a
correctly-detected-and-corrected stuck adapter could still intermittently
fail Enumerate's very next step, immediately after the real fix landed.

Fixed with a bounded retry (`Enumerator._await_subnet`, `enumerate.py`): up
to `DEFAULT_SUBNET_WAIT_TIMEOUT` (8s), polling every
`DEFAULT_SUBNET_RETRY_INTERVAL` (0.5s), checking `token.is_cancelled()`
between attempts. A single attempt and a bounded retry both still can't
distinguish "still reconnecting" from "operator genuinely never joined" —
both raise the identical `OSError` — so the bound is a tradeoff, not a fix
for that ambiguity: long enough to give a real reconnect a fair chance,
short enough that "really never joined" still fails in a few seconds, not
instantly but not unreasonably either.

## Considered options

- **Leave `RadioController`'s in-memory tracking as the only source of
  truth, fix nothing.** Rejected — this is the confirmed, real, currently-
  live bug on the reporting user's own machine.
- **Re-sync at every `RadioController` entry point** (`reserve()`,
  `supported_bands()`, `release_to_managed()`), closing the gap completely,
  including for a session that never requests `MANAGED` at all. Rejected for
  now — see the Decision section above for the real test-blast-radius
  tradeoff this was weighed against. `release_to_managed()`'s own residual
  gap (nothing reserved this session at all, but the adapter was already
  stuck from a prior one) is strictly smaller than the bug actually
  reported, and worth its own follow-up rather than reopening this one.
- **Have `Engine.reconcile_startup()` (ADR-0004) absorb this check too**,
  since it already runs once at startup, after privilege, specifically to
  reconcile in-memory assumptions against real leftover state. Considered,
  not chosen: ADR-0004's reconciliation is about orphaned *processes*
  (airodump-ng/aireplay-ng PIDs recorded in the jobs table), a different
  kind of state than radio *mode*, which `RadioController` owns entirely on
  its own and has no jobs-table analog for. Checking lazily, the one time
  it's actually needed (inside `_ensure_mode`), avoids adding radio-mode
  awareness to a module whose job is specifically process cleanup.
- **(Chosen)** One targeted branch in `_ensure_mode`, checked only for the
  one combination that's actually wrong; a bounded, cancellable retry around
  `get_subnet` for the adjacent timing gap found while verifying it.

## Consequences

- `rf.py` gains `_real_adapter_is_in_monitor_mode()` and one new
  `_ensure_mode` branch; `parse.py` gains `parse_iw_dev_type`.
- `enumerate.py`'s `Enumerator` gains two new constructor-overridable
  tunables (`subnet_wait_timeout`, default 8s; `subnet_retry_interval`,
  default 0.5s) and `_await_subnet`, replacing the single direct
  `self._get_subnet(...)` call in `_drive`.
- Confirmed directly against the real, currently-stuck adapter on the
  reporting user's own machine: `RadioController("wlx5c628b9faa9d",
  SubprocessRunner(...))._real_adapter_is_in_monitor_mode()` returns `True`
  right now, proving the detection half of this fix against the actual
  live bug, not a synthetic stand-in.
- **Not independently confirmed here**: the full privileged mode-switch
  (`airmon-ng stop` + `systemctl restart NetworkManager`) actually
  completing against this real adapter. This session has no interactive
  sudo available to exercise it; `tests/test_rf.py`'s new tests prove the
  *routing* (the right commands get spawned in the right order once the
  real-mode check fires), the same tier of proof `test_rf.py`'s existing
  privileged-path tests already settle for without real sudo. The next
  real launch of AirCommand (which prompts for sudo at startup, same as
  always) exercising Enumerate is what completes this — expected to just
  work now, given the detection half is independently confirmed, but
  genuinely unverified until it happens.
- New tests: `tests/test_rf.py` (the real-mode detection, its tolerance of
  `iw` failing, and that nothing about the existing mode-transition tests
  changed), `tests/test_parse.py` (`parse_iw_dev_type` against two real
  samples), `tests/test_enumerate_acceptance.py` (the subnet retry
  succeeding, timing out, and being cancellable). Full suite: 344 passed.
- Cross-reference: this is the gap the pre-existing `__init__` TODO comment
  already named as "accepted... not solved here" — now partially solved,
  for the one combination that was actually causing a live, reported
  failure. The broader "every entry point" version remains open, same tier
  as `release_to_managed()`'s smaller residual gap noted above.
