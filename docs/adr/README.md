# Architecture decision records

This directory holds AirCommand's ADRs: short, dated records of decisions
that shape the core engine or the GUI, plus the real-hardware bug
post-mortems that carried a durable decision of their own. They are
append-only history. When a later decision changes or reverses an earlier
one, the earlier ADR is not edited away or deleted; a status note is added
at its top, pointing forward, and the original text stays as written below
it. ADR numbers are cited by number throughout the code and test comments,
so files here are never renumbered, merged, or renamed.

## Index

| # | Title | Status | What it decides |
|---|---|---|---|
| [0001](0001-scope-boundaries.md) | Scope boundaries | Live | Permanently excludes WEP cracking, WPS attacks, and evil-twin creation from the roadmap. |
| [0002](0002-privilege-elevation.md) | Privilege elevation | Live | Elevates privilege via a session-scoped sudo keepalive at launch, not setcap or a root GUI. |
| [0003](0003-core-architecture-event-driven.md) | Core architecture: event-driven | Live | Core engine is event-driven pub/sub (EventBus + JobHandle), not a polling facade. |
| [0004](0004-orphan-process-cleanup.md) | Orphan process cleanup | Live (amended by 0015) | Auto-kills orphaned privileged processes left running after a crash, by PID/PGID + cmdline fingerprint. |
| [0005](0005-networkmanager-check-kill.md) | NetworkManager check/kill | Partially superseded by 0017 | Runs `airmon-ng check kill` automatically around every mode switch, restoring NetworkManager afterward. |
| [0006](0006-dual-band-support.md) | Dual-band support | Live (amended by 0013) | Commits to real dual-band (2.4/5GHz) support, scoped to adapter capability, authorized per BSSID, no 6GHz. |
| [0007](0007-discovery-session-vs-history.md) | Discovery: session vs history | Partially superseded by 0010 | Decided to split Discovery's table into a session view and an all-time archive view. |
| [0008](0008-driver-loops-stop-depending-on-stdout.md) | Driver loops stop depending on stdout | Live | Driver loops tick on wall-clock time instead of gating on subprocess stdout. |
| [0009](0009-driver-thread-failure-safety-net.md) | Driver thread failure safety net | Live | Gives every driver thread a crash-safe cleanup path and makes Crack check hashcat's real exit code. |
| [0010](0010-discovery-table-is-session-scoped.md) | Discovery table is session-scoped | Live (amended by 0013) | Discovery's table shows only the current session; the archive view is dropped. |
| [0011](0011-capture-cancel-hang-on-real-aircrack-ng.md) | Capture Cancel hang | Live | Fixes a real hang where Capture's one-shot handshake check could block Cancel. |
| [0012](0012-capture-handshake-detection-and-deauth-failure-visibility.md) | Capture handshake/deauth visibility | Live | Fixes real handshake detection and makes deauth burst failures visible instead of silent. |
| [0013](0013-discovery-band-selection.md) | Discovery band selection | Live (amended by 0018) | Gives Discovery an adapter-scoped band control, explicit Start, and a derived Band column. |
| [0014](0014-non-utf8-bytes-killing-a-drain-thread.md) | Non-UTF-8 bytes killing a drain thread | Live | Makes the stdout/stderr drain threads survive a non-UTF-8 byte instead of dying silently. |
| [0015](0015-hcxpcapngtool-conversion-step.md) | hcxpcapngtool conversion step | Live | Adds the missing conversion step so hashcat can actually read a real capture. |
| [0016](0016-radio-mode-desync-after-a-crash.md) | Radio mode desync after a crash | Live | Fixes RadioController's in-memory mode tracking desyncing from the adapter's real state after a crash. |
| [0017](0017-manual-radio-control-for-management-page.md) | Manual radio control | Live | Adds manual, runtime adapter selection and monitor-mode control on the Management page. |
| [0018](0018-discovery-band-gating-revised.md) | Discovery band-gating, revised | Live | Makes the band dropdown open unchosen and gates Start on an explicit band pick. |
| [0019](0019-enumerate-binds-to-the-targets-bssid.md) | Enumerate binds to the Target's BSSID | Live | Makes Enumerate verify the adapter is actually joined to the Target's network before scanning. |
| [0020](0020-appimage-packaging.md) | AppImage packaging | Live | Ships AirCommand as an AppImage that bundles Python, Tcl/Tk, and CustomTkinter, leaving the native tools (aircrack-ng suite, nmap, hashcat, hcxpcapngtool, iw) as host packages. |
| [0021](0021-gui-toolkit-pyside6-qt-widgets.md) | GUI toolkit: PySide6 Qt Widgets | Live (conditional on the Kali VM run) | Ports the GUI from CustomTkinter to PySide6 Qt Widgets, rendering on the CPU raster engine so no GPU or QML is needed, with the event pump becoming a queued cross-thread Qt signal. |

## Chains

Four groups of ADRs revisit the same area over time. Each ADR's own header
already points to the one before and after it; this list is a map for
reading them in order.

- **Band / band-gating (0006, 0013, 0018).** 0006 decided dual-band support
  was in scope; 0013 settled the interface 0006 had left open (where the
  control lives, how it reaches `airodump-ng`, the Band column) and amended
  0010 to re-add it; 0018 then amended 0013's default so Start is gated on
  an explicit band pick, instead of a pre-selected default.
- **Discovery session vs. history (0007, 0010).** 0007 decided on a
  session view plus an all-time archive view; 0010 reversed the archive
  half, which had never been built, and settled 0007's open session-key
  question.
- **Driver/thread failure handling (0008, 0009, 0014).** 0008 moved driver
  loops off stdout-gated polling and onto a wall-clock tick; 0009 added a
  crash-safe cleanup path around that loop and gave Crack a real exit-code
  check; 0014 hardened the same drain threads against a non-UTF-8 byte.
- **Radio mode control (0005, 0016, 0017).** 0005 decided NetworkManager
  handling around mode switches should be automatic; 0016 added a self-heal
  for the adapter's tracked mode drifting from its real state; 0017 added a
  second, manual entry point on the Management page, without changing
  0005's automatic path.
