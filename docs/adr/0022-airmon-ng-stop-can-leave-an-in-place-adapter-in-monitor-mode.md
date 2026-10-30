# `airmon-ng stop` can leave an in-place adapter in monitor mode

**Status: implemented 2026-10-30 and tested against fakes only. The fallback
below has not run on the real adapter yet. The next full harness run is what
confirms it.**

The first full run of the hardware harness failed three times in the same
way. It ran as `scripts/hw_test.sh wlx5c628b9faa9d --allow-network-drop
--redact` and wrote `hardware-test-reports/20261030T190659Z`, a folder git
ignores. The Monitor mode tier's `stop_monitor_mode()`,
`Engine.shutdown()` and the harness's own Final state repair all ran
`airmon-ng stop wlx5c628b9faa9d`. Each time it exited 0 and printed
`(monitor mode disabled)`, and each time `iw dev` still said `type monitor`
afterwards. Restarting NetworkManager changed nothing, because NetworkManager
ignores an interface in monitor mode. The adapter was still stuck after the
run ended.

## Root cause

The adapter is a TP-Link Archer T4U v3 on the in-kernel `rtw88_8822bu`
driver. `iw phy phy8 info` ends with `interface combinations are not
supported`. airmon-ng reads that line and, for such a driver, switches the
interface's type in place instead of adding a separate `<name>mon`
interface. That is why the name never changed.

Mint installs aircrack-ng `1:1.7+git20230807.4bf83f1a-1build2`. In that
build's `/usr/sbin/airmon-ng`, the in-place start path takes the link down,
runs `iw dev <if> set type monitor`, and brings it back up. The in-place stop
path, `changeMac80211IfaceTypeManaged`, skips the first step:

```sh
changeMac80211IfaceTypeManaged() {
  iw dev "${1}" set type managed > /dev/null 2>&1
  setLink "${1}" up
  printf "\t\t(monitor mode disabled)\n"
}
```

The interface is up at that moment, because airmon-ng start brought it up.
mac80211's `ieee80211_if_change_type()` hands a running interface to
`ieee80211_runtime_change_iftype()`, which returns `-EBUSY` whenever the
current type is monitor. `iw` fails, the redirect to `/dev/null` hides the
error, and the function prints success anyway. airmon-ng's exit code is 0.

Upstream aircrack-ng fixed this on 2023-12-22 in commit `02c1370cb5`,
"airmon-ng.linux: fix missing setDown", which adds `setLink "${1}" down` as
the function's first line. The distro package is built from a snapshot four
months older than that fix.

The live state after the run matches every step. `ip -d link show
wlx5c628b9faa9d` printed `UP` and `link/ieee802.11/radiotap`, and
`/sys/class/net/wlx5c628b9faa9d/type` held `803`, the monitor netdev type.
The harness read the right interface. It polled the same name, ifindex 10
on wiphy 8, for 15 seconds after each restore.

This is probably also how the adapter in
[ADR-0016](0016-radio-mode-desync-after-a-crash.md) ended up in monitor mode.
That ADR blamed a force-killed process. A shutdown that ran this
`airmon-ng stop` would leave the same state behind with no crash at all.
ADR-0016 never confirmed the privileged restore on hardware either.

## Decision

`RadioController._stop_monitor_mode` (`aircommand/core/rf.py`) no longer
takes `airmon-ng stop` at its word.

1. It runs `airmon-ng stop <iface>` as before.
2. It reads `/sys/class/net/<iface>/type` through `_is_monitor_interface`. `803`
   means the interface is still in monitor mode. A missing file means
   airmon-ng deleted it, which counts as restored.
3. If the interface is still in monitor mode and still has the adapter's own
   name, which is the in-place case, the core runs the sequence current upstream
   airmon-ng runs, through sudo: `ip link set dev <iface> down`, `iw dev
   <iface> set type managed`, `ip link set dev <iface> up`. Then it reads
   sysfs again. `in_place_restore_argvs()` holds this list, and the harness
   uses the same function.
4. It restarts NetworkManager whatever happened above, so the operator's
   other connections come back.
5. If the interface is still in monitor mode, it raises `MonitorModeStuck`, a
   `RadioCommandFailed` subclass. The message carries the commands that fix it
   by hand and the unplug hint. `_monitor_adapter` keeps the interface name,
   because the adapter really is still in monitor mode, so a second Stop or
   `Engine.shutdown()` tries again.

A separate monitor interface such as `wlan0mon` never gets step 3. airmon-ng
deletes that interface and recreates the managed one, and the core doesn't
take over that job. If one survives `airmon-ng stop`, the core only reports
it.

The harness follows the same rule. Its Final state repair runs `airmon-ng
stop`, reads `iw dev` back, and runs the in-place sequence if the adapter is
still in monitor mode. When even that fails, the manual commands it prints
are the in-place sequence from `manual_restore_commands()` and a
NetworkManager restart, followed by a comment line telling the operator to
unplug and replug the adapter as a last resort. The Management page's
adapter-row click now catches `RadioCommandFailed` too, since
`select_adapter()` stops the old adapter's monitor mode first and can now
raise from there.

## Considered options

- **Always take the link down before `airmon-ng stop`, as upstream's fix
  does.** Simpler, but it changes the command sequence for every adapter,
  including the ones airmon-ng already handles. It also detects nothing. The
  next way `airmon-ng stop` fails without saying so would stay silent. The
  read-back is the part that matters, and once it exists, running extra
  commands only when it fails is the smaller change.
- **Replace `airmon-ng stop` with the core's own commands for every
  adapter.** Rejected. On a renamed adapter, airmon-ng recreates the managed
  interface and deletes the monitor one, a path confirmed on real rt2800usb
  hardware. Reimplementing that buys nothing.
- **Tell the operator to install a newer aircrack-ng.** Not a fix by itself.
  Mint ships the 2023 snapshot, and building from source isn't the operator's
  normal path. The read-back would still be needed to notice the failure.
- **Rebind the USB device automatically** by unbinding and binding the
  driver in sysfs. Rejected as more disruptive than the problem needs, and it depends
  on per-driver sysfs paths. Unplugging the adapter stays the manual last
  resort.
- **Read the type with `iw dev <iface> info`**, as ADR-0016's check and the
  harness do. It works, but it adds a spawn to every restore, and every test
  that scripts the stop path would need a new `iw` entry. The sysfs file is
  what airmon-ng itself checks, and reading it costs no command. Adapters
  that airmon-ng restores correctly see exactly the commands they saw before
  this change.

## Consequences

- On this adapter, every return to managed mode logs one warning and runs
  three extra privileged commands, until Mint ships a fixed airmon-ng. After
  that the fallback never fires.
- `_stop_monitor_mode` has a new way to fail. Enumerate's start, the
  Management page's Stop and adapter-row click, and `Engine.shutdown()` all
  catch `RadioCommandFailed`, so they report it rather than crash.
- The fallback is expected to work. With the link down, mac80211 changes the
  type without calling the driver at all, and bringing the link up adds a
  managed interface, which `rtw88` supports. It still needs the hardware
  rerun before anyone calls it confirmed.
- `tests/conftest.py` stubs `_is_monitor_interface` to "not monitor" for the
  whole suite, because it reads the host's real sysfs, and a test name like
  `wlan0` can be a real adapter on a developer's machine. Tests that need it
  patch it themselves.
- The harness's simulated machine (`tests/test_hwtest_tiers.py`) now models
  mac80211 refusing to retype a running monitor interface, and has a switch
  for this airmon-ng build. Before, its `airmon-ng stop` always succeeded,
  which is why the fake-backed suite couldn't see this bug.
