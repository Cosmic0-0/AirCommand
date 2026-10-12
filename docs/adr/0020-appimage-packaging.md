# Ship AirCommand as an AppImage that bundles Python, not the native tools

**Status: implemented and verified on this Linux Mint 22.3 machine. The build
runs, the bundled interpreter's `_tkinter` opens a real `Tk()` root, and
`import customtkinter` plus the app entry point load from inside the bundle. Not
yet run on a genuinely fresh VM, and the privileged capture and crack paths
still need real monitor-mode hardware this pass could not exercise. See
Consequences.**

The user wants one file to copy to a new VM and run, instead of installing a
system Python, a virtualenv, and the pip dependencies by hand every time. An
AppImage fits that. It is a single executable that carries its own Python. This
record fixes what the AppImage carries, what it still expects from the host, and
how that squares with the launch-time sudo model in ADR-0002.

## Decision

Build from the python-appimage (niess) manylinux2014 CPython 3.12 base. That
base ships a relocatable interpreter with a working `_tkinter` and Tcl/Tk 8.6,
which is the one hard part of packaging a CustomTkinter app. It is built against
glibc 2.17, so one build runs on older and newer hosts alike. We pip-install the
`aircommand` package and its one runtime dependency (`customtkinter`, which
pulls in `darkdetect` and `packaging`) into that base, CustomTkinter's bundled
theme and font assets included, and repackage with appimagetool.

Bundled: CPython 3.12, Tcl/Tk, `_tkinter`, `customtkinter` and its assets, the
`aircommand` package.

Left on the host: the aircrack-ng suite (`airodump-ng`, `aireplay-ng`,
`aircrack-ng`, `airmon-ng`), `iw`, `nmap`, `hashcat`,
`hcxpcapngtool`, and `systemctl`. The app finds these on `PATH`. Privileged
calls resolve through sudo's own `secure_path`. A fresh VM still needs one apt
install for them. The AppImage removes the Python setup, not the native-tool
setup, and the README says so.

Why the native tools stay on the host:

- They touch things an AppImage cannot carry usefully. `airmon-ng`,
  `airodump-ng`, and `aireplay-ng` drive kernel wifi drivers, `rfkill`, and
  monitor mode. `nmap`'s SYN scans need raw sockets and its own data files.
  `hashcat` needs the host's OpenCL or CPU runtime and GPU drivers. Bundling
  them would hide the host dependency, not remove it.
- The sudo model (ADR-0002) runs each privileged leaf tool as root via `sudo -n
  <tool>`. Those have to be real host binaries. An AppImage is a read-only FUSE
  mount that root cannot read by default, so a root process reading a tool from
  inside the mount can fail. Host tools sidestep that.
- Licensing. `nmap`'s license restricts redistribution and the aircrack-ng
  suite is GPL. Leaving both as host packages avoids taking on that
  redistribution burden for a convenience build.

The launcher (`AppRun`) runs the app as the normal user through the base image's
own `usr/bin/python3.12` wrapper, which already sets the Tcl/Tk and SSL
environment. It adds two things. It appends `/usr/sbin:/sbin` to `PATH` so
unprivileged host tools such as `iw` resolve on any VM, and it sets
`PYTHONSAFEPATH=1` so the current directory cannot shadow the bundled
`aircommand` package. The database and working directory keep their defaults
under `~/.aircommand/`, which sit outside the read-only mount and are created on
first launch, so nothing there changes.

## Considered options

- **Bundle everything, native tools included.** Rejected. It would not remove
  the host's kernel-driver, GPU-runtime, or raw-socket needs, it collides with
  the sudo-reads-a-file-under-FUSE problem, and it takes on nmap and GPL
  redistribution for no real gain.
- **A zipapp or shiv single `.pyz`.** Rejected for this request. It carries
  neither the interpreter nor Tcl/Tk, so a fresh VM would still need a matching
  Python and a system Tk. It does not solve the stated problem.
- **A PyInstaller one-file binary.** Rejected. Tcl/Tk bundling is fragile, and
  the output is glibc-sensitive without an old build host, which is the exact
  portability problem the manylinux base already solves.
- **(Chosen)** The python-appimage base, bundle Python and the app, leave the
  native tools as documented host packages, sudo model unchanged.

## Consequences

- A new `packaging/appimage/` directory holds the builder (`build.sh`), the
  launcher (`AppRun`), the desktop entry, a placeholder icon, and the icon
  generator. No core engine or GUI code changed.
- Build output goes to `dist/` (gitignored). Downloaded build tools are pinned
  by version and SHA-256 and cached outside the repo.
- The one-file promise is honest only for the Python layer. A fresh VM still
  needs `sudo apt install aircrack-ng iw nmap hashcat hcxtools`. There is
  no in-app preflight that names a missing tool today. A missing unprivileged
  tool surfaces as a `FileNotFoundError`-backed job error, a missing privileged
  one as a non-zero `sudo` exit. A `shutil.which` preflight would be a
  worthwhile core-code follow-up and is out of scope here.
- Running a type-2 AppImage needs FUSE 2 (`libfuse.so.2`), which recent Ubuntu,
  Mint, and Kali do not install by default. They ship FUSE 3. Either `sudo apt
  install libfuse2` (packaged as `libfuse2t64` on 24.04-based releases) once, or
  run with `--appimage-extract-and-run`, which needs no FUSE.
- The AppImage is launched as the normal user (`./AirCommand-x86_64.AppImage`),
  never with sudo. The in-app dialog collects the sudo password as before, so
  the elevation code needs no change for the AppImage.
- Verified on this machine only. Not yet run on a clean VM, and the privileged
  capture and crack paths still need real monitor-mode hardware this environment
  cannot provide, the same standing caveat as docs/final-touches.md.
