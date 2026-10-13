# GUI toolkit: port the GUI from CustomTkinter to PySide6 Qt Widgets

**Status: Accepted, 2026-10-13. Verified: every pass/fail criterion in the
spike passed on headless `offscreen` runs; the AppImage builds and `import
PySide6` plus a real `QApplication` start succeed from inside the bundle, with a
measured PySide6-Essentials size delta of 73.9 MiB; and the user ran the spike on
their Mint host (real display, default xcb plugin) and reported "Its smooth and i
saw no problems", with 0 dropped frames, a 14.52ms mean animation tick, a 14.76ms
max event-loop drift, and 0.113 of one core. Not verified: the Kali VM under
llvmpipe software rendering, which the user chose not to run, and the fact that
the spike drove the table from a plain background thread rather than the real
Engine with `FakeProcRunner`. See
[`docs/design/gui-port/04-spike-results.md`](../design/gui-port/04-spike-results.md)
for the measured evidence.**

The GUI leaves CustomTkinter. The redesign set three goals for the GUI
(`docs/design/gui-port/03-framework-research.md`): a faster UI than
CustomTkinter, real animations and transitions, and the simplest path to both,
with a language change allowed only if it earned its keep. Four hard constraints
decided the choice. The core Engine is an in-process Python object the GUI talks
to through `engine.subscribe()` and direct calls, so any non-Python or web-shell
option turns that into inter-process communication with the Python core as a
sidecar. The app ships as an AppImage built by pip-installing into the
python-appimage base (ADR [0020](0020-appimage-packaging.md)), so the toolkit has
to be pip-installable. The Kali VM has weak or no 3D acceleration (llvmpipe
software GL), so a GPU-first renderer degrades there, and that is a real runtime,
not an edge case. The approved Grid Watch design is a dark theme with a
collapsible sidebar (196px to 54px) and a Networks table of hundreds of rows
refreshed every couple of seconds. The constraint that drove the pick is the
no-GPU VM meeting the AppImage pip flow and the in-process Engine at once.

## Decision

Use PySide6 Qt Widgets (the pip-installable `PySide6-Essentials`). Qt Widgets
renders through the CPU raster engine on every platform, so neither QML nor a GPU
is required, and the no-GPU Kali VM is the normal case rather than a degraded one.
The in-process Engine is unchanged: `engine.subscribe()` and the event bus stay
as they are.

The worker-thread-to-GUI handoff becomes a queued cross-thread Qt `Signal`. A
background subscriber on the Engine bus re-emits each event as a signal, and
because the receiver lives on the GUI thread, Qt marshals the slot onto that
thread automatically. This replaces the `queue.Queue` plus `root.after(50)` poll
in [`aircommand/gui/event_pump.py`](../../aircommand/gui/event_pump.py)
(`GuiEventPump`). The spike confirmed this path with a static check (no
`after()`, no `queue.Queue` in the handoff) and a dynamic check (the slot ran on
the GUI thread while the worker emitted from another thread). The live Networks
table is a `QTableView` backed by a `QAbstractTableModel`, updated by mutating the
model and emitting `dataChanged` rather than rebuilding widgets per refresh.

## Why

The user chose the "Go now" option and gave no reason beyond that, which accepts
the framework on the measured numbers. The basis on record is therefore the
reasoning in `03-framework-research.md` and the measured evidence in
`04-spike-results.md`, not a stated motive. The research found PySide6 Qt Widgets
the only surviving option that met all three goals without giving up a single
hard constraint, and the spike then measured it: a 14.5ms mean animation tick with
0 dropped frames, a refresh model update under 1ms, a worst-case event-loop drift
of about 10ms against the 100ms budget, and 0.14 of one core at the spec cadence
(0.27 at eight times the rate). The Mint host run reproduced that on a real
display with no fail condition seen.

## Considered options

The research weighed these; its full list and sources are in
`03-framework-research.md`.

- **PySide6 Qt Widgets (chosen).** Stays in Python with the in-process Engine
  intact, pip-installs into the AppImage flow, and renders on the CPU raster
  engine so the no-GPU VM is the normal case. `QPropertyAnimation` covers the
  sidebar collapse and page transitions, and a virtualized `QTableView` covers the
  live table.
- **Python-driven web UI, pywebview first (the named fallback).** The Grid Watch
  mockup is already HTML and CSS, so the animations are close to done, but
  pywebview adds a Python-to-JS serialization boundary where there is none today
  and needs the system WebKitGTK that pip cannot carry, which fights the one-file
  AppImage promise on a fresh VM. It also has the weakest behavior of any option
  for hundreds of rows under software rendering. NiceGUI is heavier still (an
  in-process web server plus a child-process window). Taking this fallback is the
  user's decision if a Kali run ever shows a fail condition.
- **GPU-first toolkits, ruled out by the no-GPU VM.** Qt Quick / QML, GTK4 with
  libadwaita, Flutter, Flet, Dear PyGui, and Kivy all lead with GPU rendering and
  degrade or fail under llvmpipe, and several also add a new language, an IPC
  boundary, or AppImage packaging friction. Electron and a Tauri plus Python
  sidecar add a separate runtime and the same web-host dependency as the web
  option. Textual is a terminal UI and cannot render the pixel design.

## Consequences

- **AppImage size.** The spike built a baseline AppImage and one with
  PySide6-Essentials from the same python-appimage base and measured a 73.9 MiB
  compressed delta. The research estimated 150 to 250 MB, so the real cost is
  well below that estimate; the spike corrects it.
- **A new host dependency, `libxcb-cursor0`.** Qt's xcb platform plugin needs it,
  and it was missing on the user's Mint host (the spike aborted until `sudo apt
  install libxcb-cursor0`). The setup and packaging docs need to add it alongside
  the existing native-tool apt line.
- **ADR [0020](0020-appimage-packaging.md)'s AppImage build changes.** The build
  adds `PySide6-Essentials` to the bundle; `customtkinter` is dropped. ADR 0020's
  text still describes the CustomTkinter bundle and will need updating when the
  port lands.
- **A Qt Style Sheet gotcha, recorded so it is not rediscovered.** Qt's `.cell`
  and `QPushButton.nav` selectors match the widget's C++ type name, not a dynamic
  `class` property, so the Grid Watch theme has to use the attribute form
  `QFrame[class="cell"]` and `QPushButton[class="nav"]`. The spike verified the
  theme applies by sampling rendered pixels.
- **Scope is unchanged.** This decision does not touch the scope boundaries in
  ADR [0001](0001-scope-boundaries.md) or the privilege model in ADR 0002.

## Reopen conditions

The go is conditional on the Kali VM, which is unverified. Reopen this decision if
any of these appear on the Kali VM or anywhere else: visible jank on the sidebar
collapse, a refresh that blocks input or pegs a CPU core, or a need for QML or a
GPU to hit the frame budget. If that happens, the fallback above (a Python web UI,
pywebview first) is the user's call.
