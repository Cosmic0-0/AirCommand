# GUI toolkit options for AirCommand (faster UI plus animations, simplest path)

Date: 2026-10-13. Lens: ponytail (laziest option that actually delivers speed and animation wins; fewest new dependencies, least new language, least new concepts). Training cutoff is June 2026, so current facts below are web-verified and sourced; anything unverified is marked.

## The three goals and the hard constraints they run into

Goals, verbatim: a faster UI than CustomTkinter, real animations and transitions, the simplest path to both. A language change is allowed only if it earns its keep.

Fixed constraints that decide the ranking:

1. The core Engine is an in-process Python object. The GUI talks to it by `engine.subscribe(callback)` and direct method calls. `GuiEventPump` (aircommand/gui/event_pump.py) just moves background-thread events onto Tk's mainloop through a `queue.Queue` drained by `root.after(50, ...)`. Any Python toolkit keeps this exact shape. A different language or a web shell turns that in-process call into inter-process communication (IPC) with the Python core running as a sidecar.
2. The app ships as an AppImage built from the python-appimage base, which pip-installs the package and its deps into a bundled CPython 3.12 (ADR 0020). Anything pip-installable drops into this flow. Anything that needs a Rust/Node/Dart build, or a system library pip cannot carry, fights it.
3. The Kali VM has weak or no 3D acceleration (VMware, llvmpipe software GL). Any GPU-first renderer degrades there. This is a real runtime, not an edge case.
4. The approved design (Grid Watch, docs/design/gui-redesign-gridwatch.md) is already an HTML/CSS mockup. Dark theme, collapsible sidebar (196px to 54px, `.14s ease` width transition, chevron rotate 180deg), five pages, a Networks table that reaches hundreds of rows updated every couple of seconds. The animations wanted are literally written in CSS in the mockup file.
5. sudo at launch, GUI never root (ADR 0002); event-driven core (ADR 0003). Neither moves.

The tension in one line: the web path is the laziest for the animations (the mockup is already CSS) but it adds a serialization boundary and a WebKitGTK host dependency that fights constraints 1, 2 and 3. A native Python widget toolkit keeps the in-process Engine and packages cleanly but makes you re-express the CSS.

## Cuts (not simple, or not a fit for the no-GPU VM)

- Qt Quick / QML (PySide6): the best animation story of all, but GPU-first. The software backend exists (`QT_QUICK_BACKEND=software`) yet is documented as limited, and the performant path is GPU compositing. Wrong for the no-GPU VM. Qt Widgets gives you Qt without that risk.
- GTK4 + libadwaita (PyGObject): stays in Python with nice built-in animations (libadwaita springs, GtkRevealer), but GTK4 is now GPU-first (Vulkan default on Wayland since 4.15/4.16, NGL elsewhere), its software path on VMs is unproven in current sources, and AppImage-ing GTK's gobject-introspection/typelib stack is fiddly (Flatpak is its native channel). Fights constraints 2 and 3.
- Tauri + Python sidecar: Rust shell plus the OS WebKitGTK plus Python as a separate process. New language, plus IPC, plus the same WebKitGTK host dependency as the web path. Not simple.
- Flutter: Dart, GPU-first (Impeller/Skia), Python core becomes a separate FFI or IPC process. New language and runtime, weak on software rendering. Not simple.
- Electron: bundles Chromium plus Node (roughly 150 to 200 MB), Python runs as a separate process over IPC. New language and runtime, heaviest of all. Not simple.
- Dear PyGui: immediate-mode, Linux needs OpenGL 3, no documented software fallback. Under llvmpipe on the no-GPU VM it crawls or fails. Wrong target.
- Kivy: OpenGL ES 2, same GPU dependency, and its custom non-native look fights the pixel-exact Grid Watch spec. Wrong target.
- Textual: terminal UI. Cannot render the Grid Watch pixel design or a true sidebar-width animation. Not a desktop GUI. Mention only.
- Flet (Flutter-in-Python, relevant in 2026, not on the original list): Python API over the Flutter renderer, so no new language, but GPU-first like Flutter and it runs either as a local web app or a bundled Flutter binary outside the pip/AppImage flow. Same VM risk as the web path, without the benefit of reusing the existing HTML mockup. Cut.
- Toga (BeeWare): native Python widgets, but on Linux it is GTK + WebKit, immature, and animation is not a strength. Cut.

## Surviving options

### 1. PySide6 with Qt Widgets (QPropertyAnimation, QTableView)

- In-process or IPC: stays in Python, in-process Engine unchanged. Zero boundary cost. `engine.subscribe()` and the event bus are untouched.
- Animations: Qt's built-in animation framework. `QPropertyAnimation` tweens any QObject/widget property (width, geometry, a `QGraphicsOpacityEffect`'s opacity) over a duration with an easing curve. Sidebar collapse is one width animation; page transition is a fade or slide via opacity/geometry; a live row highlight is a short color animation. For a Tkinter-level developer this is a modest new API, not a new paradigm: widgets, layouts and signals map onto pack/grid and bind. The live-updating table is the easy case here (see below).
- Speed with hundreds of live rows and software rendering: this is Qt Widgets' strongest card. QWidget and QPixmap render through the raster paint engine, which Qt's own docs describe as pure software with the full feature set on all platforms. No GPU is used or needed, so the no-GPU Kali VM is the normal case, not a degraded one. `QTableView` backed by a `QAbstractTableModel` is virtualized and built for exactly this: a few hundred rows refreshed every couple of seconds is routine, and you update the model and emit `dataChanged` rather than rebuilding widgets. This also deletes table.py's fixed-width-CTkEntry alignment hack outright.
- Replaces GuiEventPump with: Qt cross-thread signals. A background thread emits a signal; with a queued connection Qt marshals the call onto the GUI thread automatically. That is less code than the queue-plus-`after(50)` poll, and it is the idiomatic Qt pattern. The worker subscribes to the Engine bus exactly as today and re-emits as a Qt signal.
- Packaging: PySide6 is pip-installable (PySide6-Essentials, 6.12.0 as of 2026-10-08, on Qt 6.11.x, LGPL), so it drops straight into the python-appimage flow. Cost is size: Qt shared libraries add an estimated 150 to 250 MB (unverified, measure in the spike; trim by using Essentials and excluding unused Qt modules). darkdetect/packaging-style baggage is gone; customtkinter is dropped.
- New things to learn, one line: Qt's widget/layout/signal model and Qt Style Sheets (QSS, a CSS-like subset) for the Grid Watch theme.

### 2. Python-driven web UI (pywebview, or NiceGUI) [fallback]

- In-process or IPC: pywebview keeps Python in-process and renders your HTML in a native webview, with a Python<->JS bridge (`js_api`, `window.evaluate_js`) for every event. That bridge is a serialization boundary replacing the clean in-process handoff. NiceGUI is heavier still: it runs an in-process FastAPI/Uvicorn server with a Vue3/Quasar frontend, and its native window is a pywebview child process talking over a pipe, so the UI reaches the Engine over localhost websockets.
- Animations: the laziest of any option. The Grid Watch mockup is already HTML/CSS with the sidebar and chevron transitions written. CSS is the best tool in existence for the exact animations wanted. Essentially done for pywebview (serve the existing markup, wire data in). NiceGUI makes you re-express the layout in its Python DSL, which loses the direct CSS reuse.
- Speed with hundreds of live rows and software rendering: the weakest case. On Linux the webview is WebKitGTK under llvmpipe on the no-GPU VM. CSS animations are usually acceptable there, but a few hundred DOM rows re-rendered every 2s is the stress point; you would batch DOM updates and likely virtualize. Workable, not a sure thing.
- Replaces GuiEventPump with: a push into JS. pywebview: the Engine subscriber calls `window.evaluate_js(...)` (thread-safe) to deliver each event to the page. NiceGUI: `ui.timer` or its async loop. More moving parts than a Qt signal.
- Packaging: the real problem against the AppImage goal. pip does not carry WebKitGTK; on Linux pywebview needs the system `webkit2gtk-4.1` (plus PyGObject), and frozen/AppImage builds routinely ship without the backend and silently fall back to browser mode (documented in pywebview and NiceGUI issues). On a fresh Kali VM that breaks the one-file promise unless you apt-install the webview or accept launching in the system browser. NiceGUI bundles its server but still needs a Chrome 89+ class engine to render.
- New things to learn, one line: HTML/CSS/JS plus a Python-to-JS bridge (pywebview), or NiceGUI's Python UI DSL plus its server/async model.

## Recommendation

Primary: PySide6 with Qt Widgets. It is the only surviving option that meets all three goals without giving up a single hard constraint. It is faster than CustomTkinter for the live table (virtualized QTableView on the CPU raster engine, which is exactly the no-GPU VM case), it has a real built-in animation framework (QPropertyAnimation) that covers the sidebar collapse, page transitions and row highlights, and it is the simplest option that keeps everything else still: same in-process Engine, same event bus, same pip/AppImage packaging. For a developer who knows Tkinter, Qt Widgets is the smallest conceptual jump that still delivers speed and animation, because widgets/layouts/signals map onto concepts already in hand. Staying in Python wins here, plainly.

Boundary cost: none. The Engine stays in-process, `subscribe()` is unchanged, and only event_pump.py's mechanism changes (queue plus `after` poll becomes a Qt signal emitted from the subscriber thread). Of the 18 gui files, the Engine-wiring logic ports nearly as is: which Engine methods each view calls, which events it subscribes to, the gating and state-machine logic (gridwatch handoff section 1 is explicit that this wiring must be preserved, not redesigned). What gets rewritten is widget construction per view (CTk widgets become Qt widgets), theme.py (CTk theme becomes QSS), and table.py (replaced wholesale by QTableView plus a model, which also removes the column-drift hack). In short: the "what calls what" ports; the "how it is drawn" is rebuilt.

Fallback: Python-driven web UI, pywebview first (serve the Grid Watch HTML you already have; NiceGUI only if you would rather write the UI in Python and accept a web server plus a child-process window). Take this only if the WebKitGTK host dependency is acceptable, meaning you are willing to apt-install the webview on the Kali VM or launch in the system browser instead of promising a single self-contained AppImage. Its payoff is that the animations are already written in CSS; its price is a Python-to-JS boundary where there is none today, and the weakest behavior of all options for hundreds of rows under software rendering.

## Smallest spike to confirm (one afternoon, one screen)

Build the Discovery and Targets page in PySide6 Qt Widgets:

1. Collapsible sidebar with a `QPropertyAnimation` on its width (196 to 54 px) and a chevron rotation.
2. The Networks-seen `QTableView` backed by a `QAbstractTableModel`.
3. A background thread standing in for the Engine (or the real `NetworkDiscovered` subscription if hardware is handy) that pushes about 300 rows and updates them every 2 seconds, delivered to the model through a queued cross-thread signal (this is the GuiEventPump replacement under test).
4. The Grid Watch dark theme applied as QSS.

Run it inside the actual GPU-less Kali VM (llvmpipe), not just on the Mint host.

Pass if, under software rendering:
- the sidebar collapse animates with no visible stutter (confirm by eye plus a frame-time log, target roughly 60fps);
- the 300-row table sustains the 2s refresh while clicks and scrolling stay responsive (input latency under about 100ms) and the refresh does not peg a CPU core;
- the worker-thread-to-model handoff works through a Qt signal with no `after()`-style polling.

Fail if any of: visible jank on the collapse under llvmpipe, a table refresh that blocks input or saturates CPU, or you find you need QML/GPU to hit the frame budget.

Fold in one packaging check: `pip install PySide6-Essentials` into the python-appimage base, build, and record the AppImage size and that `import PySide6` plus a real `QApplication` start from inside the bundle. Pass if it builds and launches; note the size delta.

## Sources

- PySide6 6.12.0 (2026-10-08), Qt 6.11.x, LGPL: https://pypi.org/project/PySide6-Essentials/ ; https://www.piwheels.org/project/pyside6/ ; https://www.freshports.org/devel/pyside6 ; https://en.wikipedia.org/wiki/PySide
- Qt raster paint engine is pure software, default for QWidget/QPixmap, full feature set on all platforms: https://doc.qt.io/qtforpython-6/PySide6/QtGui/QPainter.html ; https://doc.qt.io/qt-6.7/qpaintengine.html ; https://wiki.qt.io/Qt5GraphicsOverview
- QPropertyAnimation tweening widget properties with easing: https://www.pythonguis.com/tutorials/qpropertyanimation/ ; https://www.pythonguis.com/tutorials/pyqt6-animated-widgets/
- Qt Quick is GPU-first; software backend exists but is limited, GPU compositing is the performant path: https://www.qt.io/blog/2016/08/15/the-qt-quick-graphics-stack-in-qt-5-8 ; https://forum.qt.io/topic/144764/is-qml-gpu-accelerated-by-default ; https://www.kdab.com/qt-quick-without-a-gpu-i-mx6-ull/
- GTK4 GPU-first (Vulkan default on Wayland 4.15/4.16, NGL elsewhere): https://www.phoronix.com/news/GTK-4.15-Vulkan-Default ; https://blog.gtk.org/2024/01/28/new-renderers-for-gtk/
- pywebview needs system WebKitGTK on Linux; frozen/AppImage builds routinely miss the backend: https://pywebview.flowrl.com/guide/installation.html ; https://github.com/r0x0r/pywebview/discussions/1818 ; https://archlinux.org/packages/extra/any/python-pywebview/
- NiceGUI native mode = FastAPI/Uvicorn + Vue3/Quasar + pywebview child process; needs Chrome 89+ class engine: https://nicegui.io/documentation/section_configuration_deployment ; https://github.com/zauberzeug/nicegui/discussions/1714
- Dear PyGui Linux needs OpenGL 3 (GPU), no documented software fallback; llvmpipe is CPU software GL: https://dearpygui.com/ ; https://gist.github.com/tstellanova/5e971f53b129eed4d24395357c768420
- Tauri 2 uses the OS webview, Python would be a sidecar; Electron ~150-200 MB with bundled Chromium: https://noqta.tn/en/blog/tauri-2-desktop-apps-rust-web-technologies-2026 ; https://www.forasoft.com/blog/article/electron-desktop-app-development-guide-for-business

## Unverified / uncertain

- PySide6 AppImage size delta (estimate 150 to 250 MB) is not measured; measure in the spike.
- Qt raster per-frame cost for about 300 rows at a 2s cadence under llvmpipe is not benchmarked; that is exactly what the spike measures.
- GTK4/libadwaita performance on llvmpipe in a VM is not established by any current source found (marked uncertain), but GPU-first default plus AppImage friction rank it below Qt regardless.
- No official pywebview statement found on the minimum WebKitGTK version for 2026; the backend-missing-in-AppImage failure is documented.
- Some version-date sources disagreed slightly (Wikipedia lagged PyPI on the PySide6 version).
