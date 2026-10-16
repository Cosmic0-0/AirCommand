#!/usr/bin/env bash
# Build AirCommand-x86_64.AppImage. See docs/adr/0020-appimage-packaging.md.
#
# Bundles CPython 3.12, PySide6-Essentials (the Qt Widgets libraries), and the
# aircommand package into a single AppImage. The native tools (aircrack-ng
# suite, nmap, hashcat, hcxpcapngtool, iw) stay on the host and are NOT bundled,
# and Qt's xcb plugin needs libxcb-cursor0 on the host, so a fresh VM still
# needs:  sudo apt install aircrack-ng iw nmap hashcat hcxtools libxcb-cursor0
#
# Linux x86_64 only. Needs: bash, curl or wget, sha256sum, and network access
# the first time (to fetch the pinned base image, appimagetool, and the pip
# dependencies). Re-running is cheap: pinned downloads are cached and checksum-
# verified, not re-fetched.
#
# Output:  <repo>/dist/AirCommand-x86_64.AppImage   (dist/ is gitignored)
# Override the download/build cache with AIRCOMMAND_APPIMAGE_CACHE=/some/dir.
set -euo pipefail

# --- pinned, checksum-verified downloads --------------------------------------
# python-appimage (niess): a relocatable CPython 3.12 built on glibc 2.17 for
# forward portability. PySide6-Essentials is pip-installed into it below.
PY_URL="https://github.com/niess/python-appimage/releases/download/python3.12/python3.12.15-cp312-cp312-manylinux2014_x86_64.AppImage"
PY_SHA256="a6a9bf619a3e21c5623be144d8ac4e458e1a5b5c133b1cc99b142d6e71b82c76"
# appimagetool, pinned release (not the rolling "continuous" tag).
AT_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage"
AT_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"

# --- paths --------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
OUTPUT_DIR="${REPO_ROOT}/dist"
OUTPUT="${OUTPUT_DIR}/AirCommand-x86_64.AppImage"

# Downloads and the AppDir live outside the source tree, each download in its own
# directory, so untrusted archives never land in the repo and nothing here is
# committed.
CACHE_DIR="${AIRCOMMAND_APPIMAGE_CACHE:-${XDG_CACHE_HOME:-${HOME}/.cache}/aircommand-appimage}"
PY_DIR="${CACHE_DIR}/python-appimage"
AT_DIR="${CACHE_DIR}/appimagetool"
BUILD_DIR="${CACHE_DIR}/build"
APPDIR="${BUILD_DIR}/AirCommand.AppDir"

say()  { printf '\033[1;36m[build]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[build] error:\033[0m %s\n' "$*" >&2; exit 1; }

# --- preflight ----------------------------------------------------------------
[ "$(uname -s)" = "Linux" ]  || die "Linux only (this is $(uname -s))."
[ "$(uname -m)" = "x86_64" ] || die "x86_64 only (this is $(uname -m))."
command -v sha256sum >/dev/null || die "sha256sum not found."
if   command -v curl >/dev/null; then DL=(curl -fsSL -o);
elif command -v wget >/dev/null; then DL=(wget -qO);
else die "need curl or wget."; fi

# fetch <url> <sha256> <dest-file>: cached, checksum-verified, idempotent.
fetch() {
  local url="$1" sha="$2" dest="$3"
  if [ -f "$dest" ] && printf '%s  %s\n' "$sha" "$dest" | sha256sum -c - >/dev/null 2>&1; then
    say "cached $(basename "$dest")"
    return
  fi
  say "downloading $(basename "$dest")"
  rm -f "$dest"
  "${DL[@]}" "$dest" "$url"
  printf '%s  %s\n' "$sha" "$dest" | sha256sum -c - >/dev/null 2>&1 \
    || die "checksum mismatch for $(basename "$dest"). Refusing to build from an unverified download."
}

mkdir -p "${PY_DIR}" "${AT_DIR}" "${BUILD_DIR}" "${OUTPUT_DIR}"

PY_APPIMAGE="${PY_DIR}/python-base.AppImage"
AT_APPIMAGE="${AT_DIR}/appimagetool.AppImage"
fetch "${PY_URL}" "${PY_SHA256}" "${PY_APPIMAGE}"
fetch "${AT_URL}" "${AT_SHA256}" "${AT_APPIMAGE}"
chmod +x "${PY_APPIMAGE}" "${AT_APPIMAGE}"

# --- assemble the AppDir ------------------------------------------------------
say "extracting the Python base"
rm -rf "${APPDIR}" "${BUILD_DIR}/squashfs-root"
( cd "${BUILD_DIR}" && "${PY_APPIMAGE}" --appimage-extract >/dev/null )
mv "${BUILD_DIR}/squashfs-root" "${APPDIR}"

say "installing aircommand and its dependencies into the bundle"
# pip builds a local dir in place, which would drop build/ and *.egg-info into
# the repo. Install from a copy in the build dir so the source tree stays clean.
SRC_COPY="${BUILD_DIR}/src"
rm -rf "${SRC_COPY}"
mkdir -p "${SRC_COPY}"
cp -R "${REPO_ROOT}/pyproject.toml" "${REPO_ROOT}/aircommand" "${SRC_COPY}/"
"${APPDIR}/usr/bin/python3.12" -m pip install \
  --no-cache-dir --disable-pip-version-check --no-warn-script-location \
  "${SRC_COPY}"

say "installing the launcher, desktop entry, and icon"
# The base ships a Python launcher/desktop/icon; swap in AirCommand's.
rm -f "${APPDIR}/AppRun" "${APPDIR}/.DirIcon"
rm -f "${APPDIR}"/python*.desktop "${APPDIR}/python.png"
[ -f "${SCRIPT_DIR}/aircommand.png" ] || "${APPDIR}/usr/bin/python3.12" "${SCRIPT_DIR}/make-icon.py" "${SCRIPT_DIR}/aircommand.png"
install -m 0755 "${SCRIPT_DIR}/AppRun"            "${APPDIR}/AppRun"
install -m 0644 "${SCRIPT_DIR}/aircommand.desktop" "${APPDIR}/aircommand.desktop"
install -m 0644 "${SCRIPT_DIR}/aircommand.png"     "${APPDIR}/aircommand.png"
ln -s aircommand.png "${APPDIR}/.DirIcon"

# --- package ------------------------------------------------------------------
say "packaging ${OUTPUT}"
rm -f "${OUTPUT}"
# --appimage-extract-and-run so appimagetool needs no FUSE on the build host.
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 \
  "${AT_APPIMAGE}" --no-appstream "${APPDIR}" "${OUTPUT}"
chmod +x "${OUTPUT}"

say "done: ${OUTPUT}"
echo
echo "Run it (as your normal user, NOT with sudo):"
echo "    ${OUTPUT}"
echo "If the host lacks FUSE 2 (recent Ubuntu/Mint/Kali), either:"
echo "    sudo apt install libfuse2    # or libfuse2t64 on 24.04-based releases"
echo "or run without FUSE:"
echo "    ${OUTPUT} --appimage-extract-and-run"
echo "The native tools are not bundled, and Qt's xcb plugin needs libxcb-cursor0."
echo "On a fresh VM install them once:"
echo "    sudo apt install aircrack-ng iw nmap hashcat hcxtools libxcb-cursor0"
