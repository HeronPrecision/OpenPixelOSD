#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Generate a 12x18 2bpp OSD font (font_data[]-compatible) from a TTF/OTF/TTC.

Targets the font slot the FC1 board actually renders — `font_data[]` (see
merge_font.py) — NOT the 8x12 font_system (dead code on STM32G431).

Output layout is the EXACT one consumed by squash_canvas_raw_pixel_buff() in
src/video_overlay.c:
  - 12 px wide x 18 px tall, 2 bits/pixel, MSB-first pair order, 3 bytes/row.
  - 54 bytes/glyph used, padded to a 64-byte stride (pad bytes = 0x55 transparent).
  - 2bpp values per px_t (src/main.h): 0=BLACK, 1=TRANSPARENT, 2=WHITE, 3=GRAY.

Design — NATIVE HINTED RENDERING (the fidelity-preserving path):
  The earlier supersample -> Lanczos-downscale -> threshold pipeline destroyed
  letterform fidelity (E's bars merged into a C; fractional resampling shifted
  stems into asymmetry). This version NEVER resamples:
    - Render each glyph at its NATIVE pixel size with FreeType's own grid-fitting
      / hinting (how OS text stays readable at small sizes), then binarize.
    - Pick the LARGEST native size at which the worst-case glyph still fits the
      cell's safe box, so nothing is ever scaled DOWN. Search is over integer
      point sizes only.
    - Place glyphs with INTEGER offsets only (per-glyph horizontal centering,
      one constant vertical translate for baseline alignment). No fractional
      resize anywhere -> no resampling artifacts.
  Result: solid white fill (+ optional 1px black outline), no gray AA, no clip,
  and the source face's actual character preserved instead of a degenerate trace.

Outputs (prefix-based):
  <prefix>.bin          raw 16384-byte font (consumed by merge_font.py)
  <prefix>.h            drop-in C header (section(".font"), font_data[16384])
  <prefix>.png          16x16 contact sheet of all 256 glyphs
  <prefix>_splash.png   mock of the boot splash drawn with these glyphs

Example:
  .venv/bin/python python/make_pixel_font_2bpp.py \\
      --ttf ~/Library/Fonts/AdwaitaSans-Regular.ttf --weight 600 \\
      --label "Adwaita Sans" --prefix python/candidates/adwaita
"""

import argparse
import sys
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ---- Cell geometry (must match src/fonts/font_bf_default.h) ------------------
W, H = 12, 18
STRIDE = 64
GLYPH_COUNT = 256
PAD_BYTE = 0x55                         # all-transparent row (01 01 01 01)

# Oversized render canvas (px). Same for every glyph so baselines share a frame;
# large enough that no glyph clips at any searched size (<= SIZE_MAX).
CANVAS = 48
OXY = 10                               # draw origin (same for all glyphs)

# Code points that are inherently bilaterally mirror-symmetric AND safe to
# mirror (no diagonal slash, no thin centered stem, no angled strokes). Mirroring
# fixes sub-pixel asymmetry (lopsided O, off-centre +). Keep this set SMALL:
# mirroring '0' wrecks its slash, 'I' doubles a centered stem, and angled letters
# like 'W'/'V' don't mirror cleanly. So only simple, featureless, centered shapes.
SYMMETRIC = {
    0x4F,        # O
    0x2B, 0x2A,  # + *
}

# ---- 2bpp values per px_t (src/main.h) --------------------------------------
PX_BLACK = 0
PX_TRANSPARENT = 1
PX_WHITE = 2
PX_GRAY = 3  # unused in solid mode; kept for decode fidelity

USAGE_EXAMPLE = """
Example:
  .venv/bin/python python/make_pixel_font_2bpp.py \\
      --ttf ~/Library/Fonts/AdwaitaSans-Regular.ttf --weight 600 \\
      --label "Adwaita Sans" --prefix python/candidates/adwaita
"""


class ExampleArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write(f"error: {message}\n")
        self.print_help(sys.stderr)
        sys.stderr.write(USAGE_EXAMPLE + "\n")
        sys.exit(2)


# ----------------------------------------------------------------------------
# Native rendering
# ----------------------------------------------------------------------------
def make_font(font_path, size, weight=0):
    font = ImageFont.truetype(font_path, size, index=0)
    if weight:
        # Variable-font heft (wght axis). Ignored for non-variable fonts.
        try:
            font.set_variation_by_axes([float(weight)])
        except Exception:
            pass
    return font


def render_mask(ch, font, threshold, embolden, close=0):
    """Native FreeType render of one glyph -> binary 'L' mask (0 or 255).

    Drawn at the fixed OXY origin on the shared CANVAS, so all glyphs at a given
    size share one baseline. No resampling. Optional morphological CLOSING
    (dilate->erode) seals 1px gaps in thin strokes so the exterior flood-fill
    can't leak into the glyph body (which would otherwise speckle it with
    outline pixels — e.g. Terminus 'G').
    """
    img = Image.new("L", (CANVAS, CANVAS), 0)
    ImageDraw.Draw(img).text((OXY, OXY), ch, font=font, fill=255)
    mask = img.point(lambda v: 255 if v >= threshold else 0, mode="1").convert("L")
    for _ in range(embolden):
        mask = mask.filter(ImageFilter.MaxFilter(3))
    for _ in range(close):
        dil = mask.filter(ImageFilter.MaxFilter(3))
        mask = dil.filter(ImageFilter.MinFilter(3))
    return mask


def stamp_mask(grid, mask_crop, hx, py):
    """Stamp a binary crop (white) into the WxH grid at integer (hx, py)."""
    nw, nh = mask_crop.size
    sp = mask_crop.load()
    for y in range(nh):
        gy = py + y
        if gy < 0 or gy >= H:
            continue
        for x in range(nw):
            gx = hx + x
            if 0 <= gx < W and sp[x, y]:
                grid[gy][gx] = PX_WHITE


def _exterior(grid):
    """Transparent cells connected (4-conn) to the cell border = the OUTSIDE.

    Enclosed transparent regions (letter counters, e.g. the triangle in an 'A',
    the hole in an 'O') are NOT exterior — they must stay transparent, not get an
    outline. This is what keeps the outline off the glyph body.
    """
    from collections import deque
    ext = [[False] * W for _ in range(H)]
    q = deque()
    for x in range(W):
        for y in (0, H - 1):
            if grid[y][x] == PX_TRANSPARENT and not ext[y][x]:
                ext[y][x] = True
                q.append((x, y))
    for y in range(H):
        for x in (0, W - 1):
            if grid[y][x] == PX_TRANSPARENT and not ext[y][x]:
                ext[y][x] = True
                q.append((x, y))
    while q:
        x, y = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < W and 0 <= ny < H and not ext[ny][nx] and grid[ny][nx] == PX_TRANSPARENT:
                ext[ny][nx] = True
                q.append((nx, ny))
    return ext


def add_outline(grids, iters, mode="exterior"):
    """Ring each white pixel with an `iters`-px black outline.

    mode='exterior': only transparent pixels OUTSIDE the glyph (connected to the
        cell border) become black. Interior counters stay transparent — needed
        for faces whose thin strokes have gaps that would otherwise let the
        outline leak inside and speckle the body.
    mode='full': every transparent neighbour of a white pixel becomes black
        (outline on BOTH sides of each stroke). Gives more black, so glyphs stay
        visible on white backgrounds. Fine for faces with solid, gap-free strokes
        (e.g. Terminus) where there is nothing to leak.
    """
    if iters <= 0:
        return grids
    out = []
    for grid in grids:
        cur = [row[:] for row in grid]
        for _ in range(iters):
            ext = _exterior(cur) if mode == "exterior" else None
            new = [row[:] for row in cur]
            for y in range(H):
                row = cur[y]
                for x in range(W):
                    if row[x] != PX_WHITE:
                        continue
                    for dy in (-1, 0, 1):
                        ny = y + dy
                        if ny < 0 or ny >= H:
                            continue
                        nrow = cur[ny]
                        nnew = new[ny]
                        extrow = ext[ny] if ext else None
                        for dx in (-1, 0, 1):
                            nx = x + dx
                            if 0 <= nx < W and nrow[nx] == PX_TRANSPARENT:
                                if ext is None or extrow[nx]:
                                    nnew[nx] = PX_BLACK
            cur = new
        out.append(cur)
    return out


# ----------------------------------------------------------------------------
# Packing (2bpp, MSB-first, 3 bytes/row) — matches the consumer exactly
# ----------------------------------------------------------------------------
def pack_glyph(grid):
    data = bytearray()
    for y in range(H):
        bits = 0
        for x in range(W):
            bits = (bits << 2) | (grid[y][x] & 0x3)
        data.append((bits >> 16) & 0xFF)
        data.append((bits >> 8) & 0xFF)
        data.append(bits & 0xFF)
    if len(data) < STRIDE:
        data += bytes([PAD_BYTE]) * (STRIDE - len(data))
    return bytes(data[:STRIDE])


def unpack_glyph(data):
    grid = [[PX_TRANSPARENT] * W for _ in range(H)]
    for y in range(H):
        b0, b1, b2 = data[y * 3], data[y * 3 + 1], data[y * 3 + 2]
        bits = (b0 << 16) | (b1 << 8) | b2
        for x in range(W):
            grid[y][x] = (bits >> (22 - 2 * x)) & 0x3
    return grid


# ----------------------------------------------------------------------------
# Output: .bin / .h
# ----------------------------------------------------------------------------
def char_for_comment(byte_val, encoding):
    try:
        ch = bytes([byte_val]).decode(encoding, errors="strict")
    except Exception:
        return ""
    if ch == "'":
        return "\\'"
    if ch == "\\":
        return "\\\\"
    if unicodedata.category(ch).startswith("C"):
        return ""
    return ch


def write_bin(path, glyphs):
    path.write_bytes(b"".join(glyphs))


def write_header(path, glyphs):
    lines = [
        "#ifndef FONT_DATA_H",
        "#define FONT_DATA_H",
        "#include <stdint.h>",
        "",
        "#define FONT_WIDTH      (12)",
        "#define FONT_HEIGHT     (18)",
        "",
        "#define FONT_BPP        (2) // 2bits per pixel",
        "#define FONT_CHARS      (256)",
        "#define BYTES_PER_ROW   ((FONT_WIDTH * FONT_BPP) / 8)     // = 3",
        "#define BYTES_PER_CHAR  ((BYTES_PER_ROW) * (FONT_HEIGHT)) // = 54",
        "#define FONT_STRIDE     (sizeof(font_data) / FONT_CHARS)  // = 64",
        "",
        '__attribute__((section(".font")))',
        "const uint8_t font_data[16384] = {",
    ]
    per_line = 16
    for code in range(GLYPH_COUNT):
        label = char_for_comment(code, "cp1251")
        lines.append(f"/* Hex 0x{code:02X}, Char '{label}' */")
        g = glyphs[code]
        for i in range(0, STRIDE, per_line):
            chunk = g[i:i + per_line]
            sep = "" if (code == GLYPH_COUNT - 1 and i + per_line >= STRIDE) else ","
            lines.append("    " + ", ".join(f"0x{b:02X}" for b in chunk) + sep)
    lines.append("};")
    lines.append("")
    lines.append("#endif // FONT_DATA_H")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------
# Output: contact sheet + splash mock PNGs
# ----------------------------------------------------------------------------
_SHADE = {PX_TRANSPARENT: (24, 24, 28), PX_BLACK: (0, 0, 0),
          PX_WHITE: (235, 235, 235), PX_GRAY: (150, 150, 150)}


def glyph_image(grid, scale):
    img = Image.new("RGB", (W * scale, H * scale), _SHADE[PX_TRANSPARENT])
    for y in range(H):
        for x in range(W):
            img.putpixel((x, y), _SHADE[grid[y][x]])
    return img.resize((W * scale, H * scale), Image.NEAREST)


def write_contact_sheet(path, grids, label, scale=6):
    cols, rows = 16, 16
    cell_w, cell_h = W * scale, H * scale
    pad = 6
    img = Image.new("RGB", (cols * (cell_w + pad) + pad,
                            rows * (cell_h + pad) + pad + 24), (10, 10, 12))
    d = ImageDraw.Draw(img)
    d.text((pad, 4), f"{label}  -  12x18 2bpp  solid native (.=transparent #=white K=black)",
           fill=(200, 200, 200))
    for code in range(GLYPH_COUNT):
        cx = code % cols
        cy = code // cols
        x = pad + cx * (cell_w + pad)
        y = 24 + pad + cy * (cell_h + pad)
        img.paste(glyph_image(grids[code], scale), (x, y))
    img.save(path)


def write_splash_mock(path, grids, scale=6):
    lines = ["HERON PRECISION", "HW: CHICKADEE", "FW: 1.0.0"]
    col0 = 8
    bw = (col0 + max(len(s) for s in lines) + 4) * W
    bh = len(lines) * H + 4 * 2
    base = Image.new("RGB", (bw, bh), (12, 12, 14))
    for li, s in enumerate(lines):
        for ci, ch in enumerate(s):
            code = ord(ch)
            if code >= GLYPH_COUNT:
                continue
            g = grids[code]
            x = (col0 + ci) * W
            y = 4 + li * H
            for yy in range(H):
                for xx in range(W):
                    base.putpixel((x + xx, y + yy), _SHADE[g[yy][xx]])
    img = base.resize((bw * scale, bh * scale), Image.NEAREST)
    d = ImageDraw.Draw(img)
    d.text((4, 2), "boot splash mock", fill=(180, 180, 180))
    img.save(path)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = ExampleArgumentParser(
        description="Generate a crisp 12x18 2bpp OSD font (.bin/.h/.png) from a TTF")
    ap.add_argument("--ttf", required=True)
    ap.add_argument("--label", default="", help="face name for PNG titles")
    ap.add_argument("--prefix", required=True, help="output base path (no extension)")
    ap.add_argument("--size-min", type=int, default=9)
    ap.add_argument("--size-max", type=int, default=18)
    ap.add_argument("--threshold", type=int, default=128,
                    help="ink threshold (lower keeps thin strokes solid)")
    ap.add_argument("--embolden", type=int, default=0, help="stroke dilation iters")
    ap.add_argument("--close", type=int, default=0,
                    help="morphological closing iters (seals 1px stroke gaps; try 1 for Terminus)")
    ap.add_argument("--outline", type=int, default=1, help="black outline ring iters (0=none)")
    ap.add_argument("--outline-mode", choices=["exterior", "full"], default="exterior",
                    help="exterior = ring only the outside (clean counters); "
                         "full = ring both sides (more black, for solid-stroke fonts like Terminus)")
    ap.add_argument("--weight", type=int, default=0,
                    help="variable-font wght axis (e.g. 600 = semibold); 0 = default instance")
    ap.add_argument("--no-mirror", action="store_true",
                    help="disable bilateral mirror-symmetry enforcement on symmetric glyphs")
    ap.add_argument("--encoding", default="cp1251")
    args = ap.parse_args()

    if len(sys.argv) == 1:
        ap.print_help(sys.stderr)
        sys.stderr.write(USAGE_EXAMPLE + "\n")
        sys.exit(1)

    O = max(0, args.outline)
    safe_w = W - 2 * O
    safe_h = H - 2 * O

    def decode(code):
        try:
            return bytes([code]).decode(args.encoding, errors="strict")
        except Exception:
            return "�"

    # ---- Search the largest native size at which the worst glyph fits the cell.
    chosen = None
    for size in range(args.size_max, args.size_min - 1, -1):
        font = make_font(args.ttf, size, args.weight)
        masks = []
        bboxes = []
        for code in range(GLYPH_COUNT):
            mask = render_mask(decode(code), font, args.threshold, args.embolden, args.close)
            masks.append(mask)
            bboxes.append(mask.getbbox())
        nonempty = [b for b in bboxes if b]
        if not nonempty:
            continue
        max_w = max(b[2] - b[0] for b in nonempty)
        tmin = min(b[1] for b in nonempty)
        bmax = max(b[3] for b in nonempty)
        block_h = bmax - tmin
        if max_w <= safe_w and block_h <= safe_h:
            chosen = (size, masks, bboxes, tmin, bmax)
            break  # largest fitting size
    if chosen is None:
        # Fall back to the smallest size and accept the fit (rare).
        size = args.size_min
        font = make_font(args.ttf, size, args.weight)
        masks, bboxes = [], []
        for code in range(GLYPH_COUNT):
            m = render_mask(decode(code), font, args.threshold, args.embolden, args.close)
            masks.append(m)
            bboxes.append(m.getbbox())
        nonempty = [b for b in bboxes if b]
        tmin = min(b[1] for b in nonempty)
        bmax = max(b[3] for b in nonempty)
        chosen = (size, masks, bboxes, tmin, bmax)
        print(f"  warning: no size fit safe box; using size {size}")

    size, masks, bboxes, tmin, bmax = chosen
    base_y = (H - (bmax - tmin)) // 2

    # ---- Place glyphs with integer offsets only (no resampling).
    grids = []
    for code in range(GLYPH_COUNT):
        grid = [[PX_TRANSPARENT] * W for _ in range(H)]
        b = bboxes[code]
        if b:
            l, t, r, bb = b
            crop = masks[code].crop(b)
            nw, nh = r - l, bb - t
            hx = (W - nw) // 2
            py = base_y + (t - tmin)
            stamp_mask(grid, crop, hx, py)
        grids.append(grid)

    # Enforce bilateral mirror-symmetry on inherently-symmetric glyphs (O, 0, +,
    # H, I, ...): mirror the left half onto the right half. Done before outlining
    # so the outline is symmetric too.
    if not args.no_mirror:
        for code in SYMMETRIC:
            g = grids[code]
            for y in range(H):
                row = g[y]
                for x in range(W // 2, W):
                    row[x] = row[W - 1 - x]

    grids = add_outline(grids, O, args.outline_mode)
    glyphs = [pack_glyph(g) for g in grids]

    # ---- round-trip self-check
    for code in range(GLYPH_COUNT):
        if unpack_glyph(glyphs[code]) != grids[code]:
            sys.exit(f"SELF-CHECK FAILED: glyph 0x{code:02X} round-trip mismatch")

    prefix = Path(args.prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    write_bin(prefix.with_suffix(".bin"), glyphs)
    write_header(prefix.with_suffix(".h"), glyphs)
    write_contact_sheet(prefix.with_suffix(".png"), grids, args.label or prefix.name)
    write_splash_mock(prefix.parent / (prefix.name + "_splash.png"), grids)

    print(f"[{args.label or prefix.name}] native size={size} thr={args.threshold} "
          f"bold={args.embolden} outline={O} weight={args.weight or '-'}")
    print(f"  wrote {prefix}.bin, {prefix}.h, {prefix}.png, {prefix}_splash.png  "
          f"(self-check OK, {len(glyphs)*STRIDE} bytes)")


if __name__ == "__main__":
    main()
