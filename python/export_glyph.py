#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Export a glyph from a compiled font_data_<face>.c to an editable text override.

Usage:
  .venv/bin/python python/export_glyph.py <face> <0xHEX>
  e.g. .venv/bin/python python/export_glyph.py terminus 0x47

Writes python/overrides/<face>_0xHEX.txt — 18 rows x 12 cols where:
  . = transparent (video passthrough)   # = white (ink)   K = black (outline)
Edit that file, then re-run merge_font.py for the face to apply it. The file is
packed VERBATIM (no auto-outline), so what you draw is exactly what renders.
"""
import importlib.util
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

spec = importlib.util.spec_from_file_location("g", HERE / "make_pixel_font_2bpp.py")
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

_CH = {0: "K", 1: ".", 2: "#", 3: "~"}


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: export_glyph.py <face> <0xHEX>\n e.g. export_glyph.py terminus 0x47")
    face = sys.argv[1]
    code = int(sys.argv[2], 16)
    src = REPO / "src" / "fonts" / f"font_data_{face}.c"
    if not src.exists():
        sys.exit(f"error: {src} not found (build/merge the face first)")
    txt = src.read_text()
    body = re.sub(r"/\*.*?\*/", "", txt, flags=re.DOTALL).split("=", 1)[1]
    data = bytes([int(t, 16) for t in re.findall(r"0x([0-9A-Fa-f]+)", body)][-16384:])
    grid = g.unpack_glyph(data[code * 64:code * 64 + 64])

    out = HERE / "overrides" / f"{face}_0x{code:02X}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    header = (f"# {face} glyph 0x{code:02X} ({chr(code)!r}) - editable. 18 rows x 12 cols.\n"
              f"# Legend:  . transparent   # white   K black\n"
              f"# This file is packed verbatim (no auto-outline). Re-run merge_font to apply.\n")
    out.write_text(header + "\n".join("".join(_CH[v] for v in row) for row in grid) + "\n")
    print(f"wrote {out}  ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
