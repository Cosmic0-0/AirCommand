# PySide6 Qt Widgets spike results

Date: 2026-10-13. One-afternoon spike to confirm or fail PySide6 Qt Widgets as
the GUI toolkit for the AirCommand port, before it is recorded as a decision.
The framework was fixed going in (see `03-framework-research.md`); this spike
does not pick a different one. It either confirms PySide6 on measured numbers or
fails it and stops.

Spike code lives in `spikes/gui-port/` and is throwaway. It is not the port and
should not be committed. This results doc is the part worth keeping.

## What was built

The Discovery and Targets page from the Grid Watch design, in PySide6 Qt
Widgets (`spikes/gui-port/spike.py`):

1. A collapsible sidebar animated from 196px to 54px with a `QPropertyAnimation`
   on a custom `barWidth` property, 140ms, and a chevron glyph that flips on
   collapse. 140ms matches the `.14s ease` in the mockup.
2. The Networks-seen table as a `QTableView` backed by a `QAbstractTableModel`.
   A refresh mutates the model's backing rows (keyed by BSSID, like
   `discovery_view.upsert_row`) and emits `dataChanged`, or `beginInsertRows`
   for genuinely new BSSIDs. It never rebuilds widgets.
3. A background thread that pushes about 300 row snapshots and refreshes them
   every 2 seconds, handed to the model through a queued cross-thread Qt signal.
   This is the `GuiEventPump` replacement under test.
4. The Grid Watch dark theme as a Qt Style Sheet, using the exact token hex
   values from `docs/design/gui-redesign-gridwatch.md`.

### Real engine or plain thread, and why

I used a plain background thread, not the real Engine with `FakeProcRunner`.

The thing under test is the cross-thread handoff and the model update, which is
exactly what replaces `event_pump.py`. A plain thread that emits the same
per-network snapshot a `NetworkDiscovered` subscriber would, on the same Qt
signal, exercises that path fully. Standing up the real Engine would add a DB, a
scripted `airodump` CSV, and `FakeProcRunner` wiring that the frame-time, CPU,
and latency harness does not read, so it would not change a single measured
number here. The real engine path is already proven headless in the core test
suite (`core-gui-boundary.md`). The worker's callback shape matches the real
feed, so swapping the real `engine.subscribe` behind the same signal in the
actual port is a small change, not a redesign.

## How it was measured

I cannot judge smoothness by eye and I cannot run the Kali VM, so the spike logs
numbers instead. `spike.py --bench` runs headless: it starts the worker, drives
several sidebar collapses, runs several 2-second refresh cycles, and prints a
JSON summary. The same metrics collect in a normal interactive run, written out
with `--metrics-file`.

The four measurements:

- **Animation frame cadence.** Wall-clock time of every `QPropertyAnimation`
  tick, gapped within each collapse (idle time between separate collapses is not
  counted). The gap between ticks is the frame interval the animation produced.
  About 16ms means 60fps. A gap over 32ms is a dropped frame (below 30fps).
- **Refresh model-update cost.** Wall time to apply one 300-row refresh batch to
  the model and emit `dataChanged`.
- **Event-loop latency.** A 20ms `QTimer` runs the whole time; its drift (fired
  late by how much) is how long the loop was blocked, which is the worst-case
  input latency proxy. If a refresh blocked the loop, this timer fires late by
  that much.
- **CPU.** Process CPU time over wall time across the run, as a fraction of one
  core. Near 1.0 means a core is pegged.

Structural facts (`spikes/gui-port/checks.py`): a static pass over the spike
source with comments and docstrings stripped, asserting no `after()`-style poll
and no `queue.Queue` in the handoff, and that the handoff is a Qt `Signal` the
worker emits and the window connects. Then a dynamic check that runs one emit
from the worker thread and asserts the slot ran on the GUI thread, which proves
the connection is queued and cross-thread, not direct.

### A real caveat about the numbers

Qt Widgets draws on the CPU raster engine on every platform, GPU or not, so the
no-GPU Kali VM is the normal case for this toolkit, not a degraded one. That is
the reason PySide6 Qt Widgets was chosen over Qt Quick.

But every frame number below came from the `offscreen` platform plugin. Offscreen
renders through the raster engine into a buffer with no compositor and no real
display. So these frame times measure the animation driver cadence, the layout
cost, and whether the refresh work blocks the main thread. They do not measure
the final blit to a screen. They are a lower bound on what the user will see, and
that is exactly why the user-run steps below exist.

I could not run under a real display plugin in this environment. `xcb` aborts
because `libxcb-cursor0` is not installed, and `wayland` has no socket to connect
to. Both are environment limits here, not PySide6 problems. The user's Mint host
and Kali VM runs cover the real-display measurement.

## Measured results

All numbers are from my runs on the `offscreen` platform plugin, Python 3.12,
PySide6 6.12.0, on the dev host (an AMD Radeon 680M machine; irrelevant to the
offscreen raster path, noted for honesty). Mint is the primary target; the Kali
VM is the fallback. Qt Widgets renders on the CPU raster engine on both.

### Spec cadence: 300 rows, 2-second refresh (three runs)

| Metric | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| Animation tick mean | 14.55ms | 14.51ms | 14.56ms |
| Animation tick p95 | 17.81ms | 18.01ms | 17.82ms |
| Animation tick max | 18.94ms | 18.84ms | 18.23ms |
| Dropped frames (over 32ms) | 0 | 0 | 0 |
| Refresh model-update mean | 0.33ms | 0.28ms | 0.31ms |
| Refresh model-update max | 0.50ms | 0.46ms | 0.43ms |
| Event-loop max drift | 10.26ms | 6.80ms | 9.12ms |
| Event-loop p95 drift | 1.75ms | 1.72ms | 1.72ms |
| CPU (fraction of one core) | 0.141 | 0.139 | 0.141 |

Eight collapse animations per run, 88 frames measured each.

### Stress: 1000 rows, 250ms refresh, 20 cycles

Eight times the spec refresh rate and three times the spec row count, to see if
anything falls over under load well past the requirement.

| Metric | Value |
|---|---|
| Animation tick mean | 14.54ms |
| Animation tick max | 21.34ms |
| Dropped frames (over 32ms) | 0 |
| Refresh model-update mean | 0.53ms |
| Refresh model-update max | 0.78ms |
| Event-loop max drift | 8.23ms |
| Event-loop p95 drift | 4.00ms |
| CPU (fraction of one core) | 0.273 |

Even here nothing dropped a frame and no core came close to pegging. The model
update stays near a millisecond because `QTableView` only paints visible rows and
`dataChanged` does not rebuild anything.

### Structural checks

`python spikes/gui-port/checks.py` exits 0. Static checks pass: no `after()`
poll, no `queue.Queue` in the handoff, the handoff is a Qt `Signal` the worker
emits and the window connects. Dynamic check passes: the slot ran on the GUI
thread while the worker emitted from a different thread, so the connection is a
real queued cross-thread signal.

One Qt Style Sheet gotcha worth recording: Qt's `.cell` and `QPushButton.nav`
selectors match the widget's C++ type name, not a dynamic `class` property, so
the first pass left most of the theme unstyled and offscreen runs did not reveal
it. The fix is the attribute form, `QFrame[class="cell"]` and
`QPushButton[class="nav"]`. I verified the theme applies by rendering the window
with `QWidget.grab()` offscreen and tallying pixels: the Grid Watch tokens all
show up in the right proportions (panel `#121418` is the dominant fill, rail
`#0d0f12` for the sidebar and statusbar, page `#0a0b0d` behind the cells, accent
`#b4d94a` on the primary button and wordmark, success `#49ab7a` on the status
pill border). That confirms the QSS reaches the widgets rather than just parsing.

### Packaging

Built from the same pinned python-appimage base and appimagetool as
`packaging/appimage/build.sh` (`spikes/gui-port/build-appimage.sh`). Two
AppImages from one base: a baseline with no PySide6, and the base plus
`pip install PySide6-Essentials==6.12.0` per ADR-0020's flow. The delta is the
real measured cost of PySide6 that the research left as an estimate.

| AppImage | Size | Launch from inside the bundle |
|---|---|---|
| Baseline (python-appimage base, no PySide6) | 17,082,872 bytes (16.3 MiB) | `in-bundle python 3.12.15 OK` |
| Base plus PySide6-Essentials 6.12.0 | 94,599,672 bytes (90.2 MiB) | `in-bundle PySide6 6.12.0 QApplication OK` |
| **Delta (PySide6-Essentials)** | **77,516,800 bytes (73.9 MiB)** | |

The PySide6 bundle builds, and from inside it `import PySide6` plus a real
`QApplication([])` start both succeed under `QT_QPA_PLATFORM=offscreen`, launched
through the packaged AppImage. The baseline launches its bundled Python too, so
the delta is a clean before-and-after on one base.

The measured delta is 73.9 MiB compressed, well under the research's 150 to
250MB estimate. PySide6-Essentials unpacks to about 247MB on disk, and squashfs
compresses the Qt shared libraries down to roughly a third. That is the single
most useful correction this spike makes to the research: the size cost is real
but much smaller than feared, and it is the trimmed Essentials set, not all of
Qt.

## Pass/fail verdict

| Criterion | Target | Measured | Verdict |
|---|---|---|---|
| Sidebar collapse smooth | about 60fps, no visible stutter | 14.6ms mean tick, 0 dropped frames over 8+ collapses and under 8x stress | Pass (CPU side); visible smoothness needs the user's display |
| Input responsive during refresh | under about 100ms | max event-loop drift 10.3ms, refresh update under 1ms | Pass |
| Refresh does not peg a core | no core saturated | 0.14 of one core at spec, 0.27 at 8x stress | Pass |
| Worker-to-model handoff via Qt signal, no poll | queued signal, no `after()` | static and dynamic checks pass | Pass |
| AppImage builds and launches in-bundle | builds, launches, size recorded | builds; `import PySide6` + `QApplication` start from inside the bundle; delta 73.9 MiB | Pass |
| No QML or GPU needed to hit the budget | stays on raster | 0 dropped frames on the offscreen raster engine, no GPU used | Pass |

No fail condition triggered on my runs. No visible jank in the frame log, no
refresh that blocked input or saturated a core, and no need to reach for QML or a
GPU.

## What I could not verify (needs the user's hardware)

- Visible smoothness on a real display. Offscreen has no compositor, so the
  final blit cost is unmeasured. This is the main gap.
- Behavior under llvmpipe software rendering in the Kali VM. The dev host has a
  real GPU, and I could not run under a real display plugin here anyway.
- Input responsiveness felt by a human clicking and scrolling, as opposed to the
  event-loop drift proxy.
- Real-display frame times on Mint (real GPU) and Kali (llvmpipe). The user-run
  steps below produce these.

## User-run verification

Run these on the Mint host first (primary target, real GPU), then in the Kali VM
(fallback, llvmpipe software rendering). Qt Widgets renders on the CPU raster
engine on both; the difference is the final compositing path.

Setup (both machines), from the repo root:

```
python3 -m venv /tmp/pyside-spike
/tmp/pyside-spike/bin/pip install PySide6-Essentials==6.12.0
```

Then, on each machine (still from the repo root, so the `spikes/gui-port/` paths
resolve):

```
# 1. Eyeball test. Click the chevron at the top of the sidebar several times.
#    Watch the collapse: is it smooth, or does it jump or stutter?
#    While the table is refreshing (it updates every 2s on its own), click the
#    nav items and scroll the table. Does clicking and scrolling stay responsive?
/tmp/pyside-spike/bin/python spikes/gui-port/spike.py --metrics-file ~/spike-metrics.txt

#    Close the window when done. It writes ~/spike-metrics.txt. Paste that file
#    back, plus in your own words: did the collapse look smooth, and did clicking
#    and scrolling stay responsive during the refresh?

# 2. Headless numbers (no clicking needed; drives the animation itself):
/tmp/pyside-spike/bin/python spikes/gui-port/spike.py --bench --metrics-file ~/spike-bench.txt
#    Paste ~/spike-bench.txt back.

# 3. Structural checks (should print ALL PASS and exit 0):
/tmp/pyside-spike/bin/python spikes/gui-port/checks.py
```

What to report back from each machine:

- Did the sidebar collapse look smooth to your eye, or did you see jank?
- During the 2-second refresh, did clicking nav items and scrolling the table
  stay responsive, or did it hitch?
- The `animation_frames` block from the metrics file: the mean tick in ms, and
  the `over_32ms` dropped-frame count. On a real display, a mean near 16ms and a
  low dropped-frame count is the pass. A climbing dropped-frame count under
  llvmpipe, or collapses that visibly stutter, is the fail.
- The `cpu_fraction_of_one_core` value while refreshes run. Well under 1.0 is the
  pass. Near or above 1.0 is a pegged core.

If the window fails to start with `xcb-cursor0 or libxcb-cursor0 is needed to
load the Qt xcb platform plugin`, install it once: `sudo apt install
libxcb-cursor0`. I hit exactly this in the build environment, and a fresh Mint or
Kali may not ship it either. It is a one-line host dependency, not a PySide6
defect, but the real port's packaging and setup docs should account for it.

Note which machine each number came from. If the Kali VM shows visible jank on
the collapse or a refresh that pegs a core under llvmpipe, that is a fail
condition and the decision below should weigh the fallback in
`03-framework-research.md` (a Python web UI, pywebview first). Taking the
fallback is the user's call, not this spike's.

## Decision

Go on PySide6 Qt Widgets as AirCommand's GUI toolkit. The user decided this on
2026-10-13.

The user picked the "Go now" option. That means accepting PySide6 Qt Widgets on
the measured numbers in this doc while the Mint and Kali runs still follow. A bad
result from either run reopens the decision.

The user gave no reason beyond picking that option. The basis on record is the
measured results above. Every criterion passed on the offscreen runs, the
AppImage delta is 73.9 MiB, and no fail condition triggered.

The go is conditional. Nobody has reported the Mint host run (real GPU) or the
Kali VM run (llvmpipe) yet. If either one shows a fail condition, the decision
reopens. The fail conditions are visible jank on the sidebar collapse, a refresh
that blocks input or pegs a core, or needing QML or a GPU to hit the frame
budget. Whether to take the fallback in `03-framework-research.md`, a Python web
UI with pywebview first, is then the user's call.

To do: promote this decision into an ADR in `docs/adr/` so it survives the next
session, per CLAUDE.md. That ADR is not written yet.
