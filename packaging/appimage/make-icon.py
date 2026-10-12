#!/usr/bin/env python3
"""Generate the placeholder AppImage icon.

The repo ships no artwork, so this draws a plain 256x256 PNG: a dark slate
background with a wifi-style arc glyph. Stdlib only (struct, zlib), no Pillow.
Replace aircommand.png with real artwork whenever one exists; this just gives
appimagetool a valid icon to point at.

Usage: python3 make-icon.py [output.png]   (default: ./aircommand.png)
"""

from __future__ import annotations

import math
import struct
import sys
import zlib

SIZE = 256
BG = (0x1F, 0x24, 0x30)      # slate
ACCENT = (0x4F, 0xC3, 0xB0)  # teal
ORIGIN = (74, 182)           # arc centre, lower-left


def _pixel(x: int, y: int) -> tuple[int, int, int]:
    dx = x - ORIGIN[0]
    dy = ORIGIN[1] - y  # up is positive
    r = math.hypot(dx, dy)
    # Solid dot at the origin.
    if r <= 15:
        return ACCENT
    # Three arcs, but only the upper-right quadrant (the signal fan).
    if dx >= -6 and dy >= -6:
        for radius in (52, 92, 132):
            if abs(r - radius) <= 7:
                return ACCENT
    return BG


def _rows() -> bytes:
    out = bytearray()
    for y in range(SIZE):
        out.append(0)  # filter type 0 (none)
        for x in range(SIZE):
            out.extend(_pixel(x, y))
    return bytes(out)


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def build_png() -> bytes:
    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0)  # 8-bit RGB
    idat = zlib.compress(_rows(), 9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "aircommand.png"
    with open(out, "wb") as handle:
        handle.write(build_png())
    print(f"wrote {out} ({SIZE}x{SIZE})")


if __name__ == "__main__":
    main()
