#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Render font sheets for the compiled OSD faces over several backgrounds.

Reads the ACTUAL shipped font data (src/fonts/font_data_<face>.c — i.e. after the
BF-symbol-preserving merge), so lowercase slots show the real preserved Betaflight
symbols, not the candidate face's lowercase. This is exactly what the device draws.

Output (python/candidates/, gitignored QA artifacts):
  sheet_<face>.png       full sheet: splash + alphabet + digits on 6 backgrounds
  _compare_faces.png     all faces' splash side-by-side on key backgrounds

Glyph compositing is faithful to the DAC pipeline:
  PX_TRANSPARENT (0b01) -> show the background (video passthrough)
  PX_WHITE  (0b10)      -> white ink
  PX_GRAY   (0b11)      -> gray AA edge
  PX_BLACK  (0b00)      -> black outline
So over black video the black outline vanishes and white carries the glyph; over
white video the reverse. The gradient band shows every luminance at once.
"""

import importlib.util
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# import merge_font.parse_base
spec = importlib.util.spec_from_file_location("merge_font", HERE / "merge_font.py")
merge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge)

# import glyph geometry + unpack from the generator
spec = importlib.util.spec_from_file_location("gen", HERE / "make_pixel_font_2bpp.py")
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)

W, H = G.W, G.H  # 12 x 18
STRIDE = G.STRIDE

# forced pixel colors (transparent handled by not stamping)
FORCE = {G.PX_BLACK: (0, 0, 0), G.PX_WHITE: (240, 240, 240), G.PX_GRAY: (135, 135, 135)}

BACKGROUNDS = [
    ("black", (0, 0, 0)),
    ("white", (255, 255, 255)),
    ("mid-gray", (128, 128, 128)),
    ("sky-blue", (96, 160, 215)),
    ("grass-green", (60, 110, 55)),
    ("gradient dark->light", "gradient"),
]

LINES = [
    "HERON PRECISION",
    "HW: CHICKADEE  FW: 1.0.0",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
    "0123456789  .,:;!?'()[]<>/+-*=%@#$&",
]

FACES = [
    ("default", "default (original Betaflight)"),
    ("terminus", "Terminus (pixel font)"),
]

SCALE = 6
LABEL_W = 200
PAD = 10


def load_face(face):
    path = REPO / "src" / "fonts" / f"font_data_{face}.c"
    return bytes(merge.parse_base(path))


def unpack_glyph(data, code):
    g = data[code * STRIDE:(code + 1) * STRIDE]
    return G.unpack_glyph(g)


def stamp_line(img, data, s, x0, y0):
    """Stamp one text line onto img; transparent pixels leave the bg intact."""
    for ci, ch in enumerate(s):
        code = ord(ch)
        if code >= 256:
            continue
        grid = unpack_glyph(data, code)
        for y in range(H):
            row = grid[y]
            for x in range(W):
                v = row[x]
                if v == G.PX_TRANSPARENT:
                    continue
                img.putpixel((x0 + ci * W + x, y0 + y), FORCE[v])


def make_band(data, bg, width):
    """One background band with all LINES stamped."""
    if bg == "gradient":
        band = Image.new("RGB", (width, len(LINES) * H + PAD * 2))
        px = band.load()
        for x in range(width):
            t = x / max(1, width - 1)
            c = int(255 * t)
            for y in range(band.height):
                px[x, y] = (c, c, c)
    else:
        band = Image.new("RGB", (width, len(LINES) * H + PAD * 2), bg)
    for li, s in enumerate(LINES):
        stamp_line(band, data, s, PAD, PAD + li * H)
    return band.resize((band.width * SCALE, band.height * SCALE), Image.NEAREST)


def render_sheet(face, label, data, out):
    content_w = max(len(s) for s in LINES) * W + PAD * 2
    band_w = max(content_w, 360)
    bands = [(name, make_band(data, bg, band_w)) for name, bg in BACKGROUNDS]

    total_w = LABEL_W + band_w * SCALE + PAD
    total_h = 40 + sum(b.height for _, b in bands) + PAD * len(bands)
    sheet = Image.new("RGB", (total_w, total_h), (12, 12, 14))
    d = ImageDraw.Draw(sheet)
    d.text((PAD, 8), f"{label}  -  12x18 2bpp  (white=ink, gray=AA, black=outline; "
                     f"transparent = video passthrough)", fill=(210, 210, 210))
    y = 36
    for name, b in bands:
        sheet.paste(b, (LABEL_W, y))
        d.text((PAD, y + b.height // 2 - 6), name, fill=(200, 200, 200))
        y += b.height + PAD
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(f"  {out.name}  ({sheet.size[0]}x{sheet.size[1]})")


def render_compare(faces_data, out):
    """All faces' splash on black / white / gradient, stacked for direct compare."""
    splash = ["HERON PRECISION", "HW: CHICKADEE  FW: 1.0.0"]
    bgs = [("on BLACK", (0, 0, 0)), ("on WHITE", (255, 255, 255)), ("on GRADIENT", "gradient")]
    content_w = max(len(s) for s in splash) * W + PAD * 2
    band_w = content_w
    label_w = 170
    rows = []
    for face, flabel, data in faces_data:
        for bname, bg in bgs:
            if bg == "gradient":
                band = Image.new("RGB", (band_w, len(splash) * H + PAD * 2))
                px = band.load()
                for x in range(band_w):
                    c = int(255 * x / max(1, band_w - 1))
                    for yy in range(band.height):
                        px[x, yy] = (c, c, c)
            else:
                band = Image.new("RGB", (band_w, len(splash) * H + PAD * 2), bg)
            for li, s in enumerate(splash):
                stamp_line(band, data, s, PAD, PAD + li * H)
            rows.append((f"{flabel}  {bname}", band.resize(
                (band.width * SCALE, band.height * SCALE), Image.NEAREST)))
    total_w = label_w + band_w * SCALE + PAD
    total_h = 36 + sum(b.height for _, b in rows) + PAD * len(rows)
    img = Image.new("RGB", (total_w, total_h), (12, 12, 14))
    d = ImageDraw.Draw(img)
    d.text((PAD, 8), "Splash comparison - all faces on 3 backgrounds", fill=(210, 210, 210))
    y = 34
    for lbl, b in rows:
        img.paste(b, (label_w, y))
        d.text((PAD, y + b.height // 2 - 6), lbl, fill=(200, 200, 200))
        y += b.height + PAD
    img.save(out)
    print(f"  {out.name}  ({img.size[0]}x{img.size[1]})")


def main():
    outdir = REPO / "python" / "candidates"
    outdir.mkdir(parents=True, exist_ok=True)
    faces_data = []
    for face, label in FACES:
        print(f"rendering {face} ...")
        data = load_face(face)
        assert len(data) == 16384, f"{face}: {len(data)} bytes"
        faces_data.append((face, label, data))
        render_sheet(face, label, data, outdir / f"sheet_{face}.png")
    render_compare(faces_data, outdir / "_compare_faces.png")
    print(f"\nDone -> {outdir}/sheet_{{default,plexmono,adwaita,iosevka}}.png + _compare_faces.png")


if __name__ == "__main__":
    main()
