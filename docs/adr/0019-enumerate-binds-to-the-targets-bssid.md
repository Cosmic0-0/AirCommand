# Enumerate verifies the adapter is actually on the Target's network

**Status: implemented, verified against `FakeProcRunner`; the real `iw` output
shape it depends on was confirmed on real hardware (see Consequences), the
privileged nmap scan that follows it was not re-verified live in this pass.**

`start_scan()`'s `self._allowlist.require_target(target.bssid)` call proves
the Target is on the authorization allowlist. It proves nothing about what
network the adapter is physically associated to right now. Option A (this
module's own docstring, `docs/roadmap.md` Phase 1 item 3) already has
AirCommand never joining a network itself: the operator associates the
adapter through their OS's normal wifi settings before clicking Enumerate,
and `get_interface_subnet` just reads whatever address that interface
currently holds. Nothing before this ADR checked that the subnet it read
back actually belonged to the Target, as opposed to some other network the
adapter happened to be sitting on. An authorized Target and an unauthorized
scan target were two different checks that could silently point at two
different networks, and nmap would scan whichever one `get_interface_subnet`
handed it.

## Decision

`_verify_adapter_on_target` (`enumerate.py`) runs `iw dev <adapter> link`,
parses the result with a new `parse_iw_dev_link_bssid` (`parse.py`), and
compares it against `target.bssid`. Fails closed on every ambiguous case:
not connected, empty output, a nonzero `iw` exit, or anything the parser
can't match. It never skips the check and scans anyway. Both sides of the
comparison are `MacAddress` instances; `MacAddress.parse` already
canonicalizes to uppercase, so the comparison is case-insensitive without
any special-casing in the check itself.

Called from `_drive`, once, immediately before building the nmap `argv`,
deliberately *after* `_await_subnet` succeeds, not before. Three existing
tests supply a `get_subnet` that always raises, specifically to exercise
`_await_subnet`'s own retry-then-propagate path, and need to keep failing
there without ever reaching this new check.

This is a single, point-in-time check, not continuously revalidated for the
scan's duration. The same trade-off Capture already accepts for its own
allowlist gate (CONTEXT.md): the adapter could, in principle, re-associate
to a different network a moment after this check passes and before nmap
exits. Nmap's scan isn't interruptible mid-run on a signal this codebase
currently sends (see the module's own `_drive` comment on `-oX -` output
being buffered to completion, not streamed), so there's no natural place to
re-check partway through. Accepted as the same kind of gap as any
point-in-time authorization check, not something this change set out to
close.

## Considered options

- **Trust `get_interface_subnet`'s result and skip the BSSID check
  entirely** (status quo before this ADR). Rejected: this is exactly the
  gap described above. An authorized Target and the adapter's actual network
  were never cross-checked.
- **Re-verify continuously for the scan's duration**, not just once before
  it starts. Rejected for now: nmap's own output isn't streamed in a way
  that gives this check a natural point to re-run mid-scan, and the scan
  itself is short-lived enough that the residual gap is the same shape as
  other accepted point-in-time checks elsewhere in this codebase.
- **(Chosen)** One point-in-time check, run once, immediately before the
  scan starts, failing closed on every case it can't positively confirm.

## Consequences

- `enumerate.py` gains `_verify_adapter_on_target`; `parse.py` gains
  `parse_iw_dev_link_bssid`.
- New tests: `tests/test_parse.py` (`parse_iw_dev_link_bssid` against a
  connected line, a case-different BSSID, "Not connected.", empty output,
  and a malformed BSSID; all five return `None` except the two connected
  cases); `tests/test_enumerate_acceptance.py`
  (`test_scan_refused_when_adapter_is_connected_to_a_different_network`,
  `test_scan_refused_when_adapter_is_not_connected_to_anything`,
  `test_scan_refused_when_iw_exits_nonzero`).
- Real `iw` output shape confirmed directly on real hardware: a connected
  interface's first line is `"Connected to <mac> (on <if>)"` (lowercase hex,
  colon-separated); an unassociated one prints the single line `"Not
  connected."`. `parse_iw_dev_link_bssid`'s regex and fallback-to-`None`
  behavior were written against these real samples, not assumed from
  documentation.
- **Not independently confirmed in this pass**: a live `iw dev <adapter>
  link` call against a real Target network, immediately followed by a real
  privileged nmap scan, end to end. `tests/test_enumerate_acceptance.py`'s
  new tests prove the routing (the right check runs at the right point,
  with the right fail-closed behavior) against `FakeProcRunner`, the same
  tier of proof this codebase's other privileged-path tests already settle
  for without real sudo. Worth a real click-through on real hardware before
  calling this fully closed, same as `docs/final-touches.md`'s existing
  open items for other real-hardware paths.
