#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Host-side preview and validator for the power-up OSD wave.

Nothing here is flashed. The point is to iterate on the boot animation and catch geometry
defects without a board on the bench.

    tools/wave_preview.py check      geometry assertions over every frame (exit 1 on failure)
    tools/wave_preview.py show       ASCII dump, every Nth frame
    tools/wave_preview.py front 30   one frame, leading edge at diagonal 30
    tools/wave_preview.py play       animate in the terminal
    tools/wave_preview.py glyphs     measured ink coverage per glyph, for picking the ramp
    tools/wave_preview.py html       self-contained animated preview, real font, true aspect

Every tunable is read out of the firmware headers at run time -- the ramp, band depth and
timing from src/boot_wave.h, the canvas size from src/canvas_char.h, the glyph bitmaps from
the linked font face. Edit the header, re-run this, see the result. The only thing mirrored
rather than read is the two-line diagonal formula itself (d = front - (row + col)); it is
called out in boot_wave.h so the pair stays in step.

Add `legacy` as a first argument to render the superseded dithered implementation instead,
which is what the checks below were written against.
"""

import base64
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")


# --------------------------------------------------------------------- firmware constants

def _read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fp:
        return fp.read()


def _define_int(text, name):
    m = re.search(r"^#define\s+%s\s+\(?\s*(\d+)" % re.escape(name), text, re.M)
    if not m:
        sys.exit("wave_preview: could not find #define %s" % name)
    return int(m.group(1))


class Config:
    """Everything the preview needs, lifted from the firmware headers."""

    def __init__(self):
        canvas = _read(os.path.join(SRC, "canvas_char.h"))
        wave = _read(os.path.join(SRC, "boot_wave.h"))

        self.rows = _define_int(canvas, "ROW_SIZE")
        self.cols = _define_int(canvas, "COLUMN_SIZE")
        self.depth = _define_int(wave, "BOOT_WAVE_DEPTH")
        self.duration_ms = _define_int(wave, "BOOT_WAVE_DURATION_MS")
        self.frame_ms = _define_int(wave, "BOOT_WAVE_FRAME_MS")

        m = re.search(r"^#define\s+BOOT_WAVE_RAMP\s+\{(.*?)\}", wave, re.M | re.S)
        if not m:
            sys.exit("wave_preview: could not find #define BOOT_WAVE_RAMP")
        self.ramp = [c for c in re.findall(r"'(\\?.)'", m.group(1))]
        self.ramp = [c.replace("\\", "") for c in self.ramp]

        if len(self.ramp) != self.depth:
            sys.exit("wave_preview: BOOT_WAVE_RAMP has %d entries but BOOT_WAVE_DEPTH is %d"
                     % (len(self.ramp), self.depth))

        self.face = os.environ.get("FACE", "terminus")
        self.font_path = os.path.join(SRC, "fonts", "font_data_%s.c" % self.face)

    # Total distance the front travels: past the last diagonal by the full band depth, so
    # the tail clears the bottom-right corner before the animation ends.
    @property
    def span(self):
        return (self.rows - 1) + (self.cols - 1) + self.depth

    @property
    def n_frames(self):
        return self.duration_ms // self.frame_ms + 1


CFG = Config()

# Font geometry (src/fonts/font_data.h). One character cell is FONT_W x FONT_H pixels.
FONT_W, FONT_H, FONT_STRIDE, FONT_BPR = 12, 18, 64, 3


# ------------------------------------------------------------------------ frame rendering

LEGACY = False


def front_at(frame):
    """Leading-edge diagonal at frame index `frame`."""
    elapsed = frame * CFG.frame_ms
    span = (CFG.rows + CFG.cols + 24) if LEGACY else CFG.span
    if elapsed >= CFG.duration_ms:
        return span
    return elapsed * span // CFG.duration_ms


def _legacy_density(d):
    """The superseded taper: density buckets by distance behind the front."""
    if d < 0:
        return 0
    if d < 4:
        return 2   # leading edge (quarter)
    if d < 6:
        return 3   # half
    if d < 18:
        return 4   # core (full)
    if d < 20:
        return 3   # half
    if d < 24:
        return 2   # trailing edge (quarter)
    return 0


def render(front):
    """One frame as a list of row strings, exactly as video_boot_animation_update() draws it."""
    rows = []
    for r in range(CFG.rows):
        line = []
        for c in range(CFG.cols):
            d = front - r - c
            if LEGACY:
                # Dither cell PRESENCE, keyed on (r + c) -- the same quantity that selects
                # the density bucket. This is the defect: the key is constant along each
                # diagonal, so a "dithered" diagonal is fully on or fully off.
                dens = _legacy_density(d)
                if dens == 4:
                    lit = True
                elif dens == 3:
                    lit = ((r + c) & 1) == 0
                elif dens == 2:
                    lit = ((r + c) % 4) == 0
                else:
                    lit = False
                line.append("+" if lit else " ")
            else:
                line.append(CFG.ramp[d] if 0 <= d < CFG.depth else " ")
        rows.append("".join(line))
    return rows


# ---------------------------------------------------------------------------- ASCII output

def print_grid(grid, frame, front, note=""):
    if frame is None:
        head = "front=%2d" % front
    else:
        head = "frame %3d  t=%4d ms  front=%2d" % (frame, frame * CFG.frame_ms, front)
    print("%s%s" % (head, ("   %s" % note) if note else ""))
    bar = "    +" + "-" * CFG.cols + "+"
    print(bar)
    for r, line in enumerate(grid):
        print("%3d |%s|" % (r, line))
    print(bar)


# ----------------------------------------------------------------------------- validation

def column_extents(grid):
    """Per column: (top row, bottom row, has_gap). top/bot are None for an empty column."""
    out = []
    for c in range(CFG.cols):
        lit = [r for r in range(CFG.rows) if grid[r][c] != " "]
        if not lit:
            out.append((None, None, False))
        else:
            gap = (lit[-1] - lit[0] + 1) != len(lit)
            out.append((lit[0], lit[-1], gap))
    return out


def check_edge(edge, clamp_at, name, fails):
    """Both band edges must read as one straight angled line.

    Walking left to right the boundary row steps down by exactly one per column, repeating
    a value only where it is pinned against the top or bottom of the screen. Anything else
    is the defect this tool exists to catch: a column reaching further than its neighbours.
    """
    prev, prev_c = None, None
    for c, row in enumerate(edge):
        if row is None:
            continue
        if prev is not None:
            if c != prev_c + 1:
                fails.append("%s edge: lit columns are not contiguous (gap before col %d)"
                             % (name, c))
            else:
                step = prev - row
                if step == 0 and prev != clamp_at:
                    fails.append("%s edge: flat step col %d->%d at row %d, but not clamped "
                                 "to row %d" % (name, prev_c, c, prev, clamp_at))
                elif step not in (0, 1):
                    fails.append("%s edge: col %d row %d -> col %d row %d is a step of %d, "
                                 "expected 1" % (name, prev_c, prev, c, row, step))
        prev, prev_c = row, c


def check_frame(grid):
    fails = []
    ext = column_extents(grid)

    for c, (top, bot, gap) in enumerate(ext):
        if gap:
            fails.append("column %d has a gap in its lit run (detached cells)" % c)
            break

    check_edge([e[0] for e in ext], 0, "trailing", fails)
    check_edge([e[1] for e in ext], CFG.rows - 1, "leading", fails)

    if not LEGACY:
        for c, (top, bot, _) in enumerate(ext):
            if top is None:
                continue
            length = bot - top + 1
            clamped = (top == 0) or (bot == CFG.rows - 1)
            if length > CFG.depth:
                fails.append("column %d spans %d rows, deeper than the %d-diagonal band"
                             % (c, length, CFG.depth))
                break
            if length < CFG.depth and not clamped:
                fails.append("column %d spans only %d of %d rows and is not clamped to a "
                             "screen edge" % (c, length, CFG.depth))
                break
    return fails


def cmd_check(_args):
    max_reports, reported, total = 6, 0, 0

    print("checking %d frames   grid %dx%d   depth %d   %d ms at %d ms/frame   ramp %s"
          % (CFG.n_frames, CFG.cols, CFG.rows, CFG.depth, CFG.duration_ms, CFG.frame_ms,
             "".join(CFG.ramp)))
    if LEGACY:
        print("(rendering the superseded dithered implementation)")

    # A blank inside the ramp punches an unlit diagonal through the middle of the band.
    for i, ch in enumerate(CFG.ramp):
        if ch == " ":
            print("\nFAIL  BOOT_WAVE_RAMP[%d] is blank: an unlit diagonal inside the band "
                  "shows up as detached strands." % i)
            total += 1

    prev_front, max_step = None, 0
    for f in range(CFG.n_frames):
        front = front_at(f)
        grid = render(front)

        fails = check_frame(grid)
        if prev_front is not None:
            if front < prev_front:
                fails.append("front went backwards (%d -> %d)" % (prev_front, front))
            max_step = max(max_step, front - prev_front)
        prev_front = front

        total += len(fails)
        if fails and reported < max_reports:
            reported += 1
            print("\nFAIL  frame %d: %s" % (f, fails[0]))
            for extra in fails[1:4]:
                print("      also: %s" % extra)
            print_grid(grid, f, front)

    last = render(front_at(CFG.n_frames - 1))
    if any(ch != " " for line in last for ch in line):
        total += 1
        print("\nFAIL  the wave has not left the screen by the end of the animation")
        print_grid(last, CFG.n_frames - 1, front_at(CFG.n_frames - 1))

    if max_step > 1:
        print("\nwarning: the front advances up to %d diagonals per frame, so the wave will "
              "look steppy.\n         Raise BOOT_WAVE_DURATION_MS above %d ms, or lower "
              "BOOT_WAVE_FRAME_MS." % (max_step, CFG.span * CFG.frame_ms))

    if total == 0:
        print("\nPASS  every frame: both edges unbroken straight lines, no gaps, no "
              "detached cells.")
        return 0
    print("\n%d failure(s)%s" % (total, " (output truncated)" if total > reported else ""))
    return 1


# ------------------------------------------------------------------------------ font / ink

_FONT_CACHE = {}


def font_bytes():
    if "d" not in _FONT_CACHE:
        text = _read(CFG.font_path)
        body = text[text.index("{", text.index("font_data[")):]
        # Every glyph is labelled with a `/* Hex 0xNN */` comment; strip comments first or
        # those labels get parsed as font bytes and shift the whole table.
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        body = re.sub(r"//[^\n]*", "", body)
        data = bytes(int(h, 16) for h in re.findall(r"0x([0-9a-fA-F]{2})", body))
        if len(data) != 256 * FONT_STRIDE:
            sys.exit("wave_preview: %s parsed as %d bytes, expected %d"
                     % (CFG.font_path, len(data), 256 * FONT_STRIDE))
        _FONT_CACHE["d"] = data
    return _FONT_CACHE["d"]


def glyph_pixels(code):
    """Yield the 2bpp class of every pixel: 0 black, 1 transparent, 2 white, 3 gray (px_t)."""
    data = font_bytes()
    base = code * FONT_STRIDE
    for y in range(FONT_H):
        for x in range(FONT_W):
            bit = x * 2
            byte = data[base + y * FONT_BPR + (bit >> 3)]
            yield (byte >> (6 - (bit & 7))) & 3


def glyph_weight(ch):
    """Perceived weight on screen: white counts full, gray half, black not at all (the
    outline is darker than the video behind it, so it adds no brightness)."""
    counts = [0, 0, 0, 0]
    for px in glyph_pixels(ord(ch)):
        counts[px] += 1
    return (counts[2] + 0.5 * counts[3]) * 100.0 / (FONT_W * FONT_H), counts


def cmd_glyphs(args):
    candidates = args[0] if args else " .,:;-~=+*^oXO0#%8@$WM"
    print("face: %s\n" % CFG.face)
    print("glyph  hex   white  gray  black  transp   weight%")
    for ch in candidates:
        weight, counts = glyph_weight(ch)
        print("  %s    0x%02X   %5d %5d  %5d   %5d    %6.1f"
              % ("_" if ch == " " else ch, ord(ch), counts[2], counts[3], counts[0],
                 counts[1], weight))

    print("\nBOOT_WAVE_RAMP weights -- should rise to the crest then fall back:")
    print("  " + "  ".join("%s=%.1f" % (ch, glyph_weight(ch)[0]) for ch in CFG.ramp))
    return 0


# ----------------------------------------------------------------------------- HTML output

HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>OpenPixelOSD boot wave</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;padding:24px;background:#14161a;color:#d6dae0;
      font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}
 h1{font-size:15px;font-weight:600;margin:0 0 4px;letter-spacing:.04em}
 p.sub{margin:0 0 18px;color:#7d848e;max-width:70ch}
 #wrap{max-width:940px}
 #screen{width:100%;aspect-ratio:4/3;display:block;background:#4a4d52;
         border:1px solid #2c3036;border-radius:4px;image-rendering:pixelated}
 .row{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:14px}
 button,select{font:inherit;background:#22262c;color:#d6dae0;border:1px solid #363b43;
      border-radius:4px;padding:5px 12px;cursor:pointer}
 button:hover,select:hover{background:#2b3038}
 input[type=range]{flex:1;min-width:220px;accent-color:#6aa9ff}
 label{display:flex;gap:6px;align-items:center;color:#9aa1ab;cursor:pointer}
 #stat{color:#7d848e;margin-top:12px;white-space:pre}
 .lead{color:#6aa9ff}.trail{color:#ff8f5e}
</style></head><body><div id="wrap">
<h1>OpenPixelOSD &mdash; power-up wave%%TITLE%%</h1>
<p class="sub">Real font glyphs (%%FACE%%), real %%FW%%&times;%%FH%% field, 4:3 aspect.
Frames generated by <code>tools/wave_preview.py</code> from <code>src/boot_wave.h</code>.
Edge guides mark where the <span class="lead">leading</span> and
<span class="trail">trailing</span> boundaries should fall &mdash; if the drawn edge leaves
the line, that is the bug.</p>
<canvas id="screen" width="%%FW%%" height="%%FH%%"></canvas>
<div class="row">
 <button id="play">Pause</button>
 <input type="range" id="seek" min="0" max="%%LASTFRAME%%" value="0">
 <select id="speed"><option value="0.25">0.25x</option><option value="0.5">0.5x</option>
 <option value="1" selected>1x</option></select>
</div>
<div class="row">
 <label><input type="checkbox" id="guides"> edge guides</label>
 <label><input type="checkbox" id="cells"> character grid</label>
</div>
<div id="stat"></div>
</div>
<script>
const ROWS=%%ROWS%%, COLS=%%COLS%%, FW=%%GW%%, FH=%%GH%%, DEPTH=%%DEPTH%%,
      FRAME_MS=%%FRAMEMS%%;
const FRAMES=%%FRAMES%%, FRONTS=%%FRONTS%%;
const raw=atob("%%FONT%%"), FONT=new Uint8Array(raw.length);
for(let i=0;i<raw.length;i++)FONT[i]=raw.charCodeAt(i);
const STRIDE=FONT.length/256, BPR=(FW*2)/8;

/* Glyph atlas, 16x16 glyphs. 2bpp classes: 0 black, 1 transparent, 2 white, 3 gray. */
const atlas=document.createElement('canvas');
atlas.width=16*FW; atlas.height=16*FH;
{const ax=atlas.getContext('2d'), img=ax.createImageData(atlas.width,atlas.height);
 for(let code=0;code<256;code++){
  const gx=(code%16)*FW, gy=(code>>4)*FH;
  for(let y=0;y<FH;y++)for(let x=0;x<FW;x++){
   const bit=x*2, b=FONT[code*STRIDE+y*BPR+(bit>>3)], px=(b>>(6-(bit&7)))&3;
   const o=(((gy+y)*atlas.width)+(gx+x))*4;
   let v=0,a=255;
   if(px===1){a=0;} else if(px===2){v=255;} else if(px===3){v=150;}
   img.data[o]=v; img.data[o+1]=v; img.data[o+2]=v; img.data[o+3]=a;
  }}
 ax.putImageData(img,0,0);}

const cv=document.getElementById('screen'), cx=cv.getContext('2d');
cx.imageSmoothingEnabled=false;
const $=id=>document.getElementById(id);
let frame=0, playing=true, last=0, acc=0;

function guides(front){
 /* Ideal band boundaries: row+col = front (leading) and front-DEPTH+1 (trailing),
    drawn through cell centres and extended past both screen edges. */
 for(const [k,color] of [[front,'#6aa9ff'],[front-DEPTH+1,'#ff8f5e']]){
  cx.strokeStyle=color; cx.lineWidth=1; cx.beginPath();
  cx.moveTo(-FW/2, (k+1)*FH+FH/2); cx.lineTo(COLS*FW+FW/2, (k-COLS)*FH+FH/2); cx.stroke();
 }
}
function cells(){
 cx.strokeStyle='rgba(255,255,255,.10)'; cx.lineWidth=1; cx.beginPath();
 for(let c=1;c<COLS;c++){cx.moveTo(c*FW+.5,0);cx.lineTo(c*FW+.5,ROWS*FH);}
 for(let r=1;r<ROWS;r++){cx.moveTo(0,r*FH+.5);cx.lineTo(COLS*FW,r*FH+.5);}
 cx.stroke();
}
function draw(){
 const s=FRAMES[frame], front=FRONTS[frame];
 cx.fillStyle='#4a4d52'; cx.fillRect(0,0,cv.width,cv.height);
 if($('cells').checked) cells();
 for(let r=0;r<ROWS;r++)for(let c=0;c<COLS;c++){
  const code=s.charCodeAt(r*COLS+c);
  if(code===32) continue;
  cx.drawImage(atlas,(code%16)*FW,(code>>4)*FH,FW,FH,c*FW,r*FH,FW,FH);
 }
 if($('guides').checked) guides(front);
 $('seek').value=frame;
 $('stat').textContent='frame '+String(frame).padStart(3)+'   t='+
   String(frame*FRAME_MS).padStart(4)+' ms   front='+String(front).padStart(2)+
   '   band spans diagonals '+(front-DEPTH+1)+'..'+front;
}
function tick(t){
 if(playing){ if(last) acc+=(t-last)*parseFloat($('speed').value); last=t;
  while(acc>=FRAME_MS){acc-=FRAME_MS; frame=(frame+1)%FRAMES.length;}
  draw(); } else last=t;
 requestAnimationFrame(tick);
}
$('play').onclick=()=>{playing=!playing; $('play').textContent=playing?'Pause':'Play';};
$('seek').oninput=e=>{playing=false;$('play').textContent='Play';frame=+e.target.value;draw();};
$('guides').onchange=$('cells').onchange=draw;
draw(); requestAnimationFrame(tick);
</script></body></html>
"""


def cmd_html(args):
    path = args[0] if args else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             "wave_preview.html")
    frames, fronts = [], []
    for f in range(CFG.n_frames):
        front = front_at(f)
        fronts.append(front)
        # Ramp glyphs are free-form, so escape before embedding in a JS string literal.
        frames.append("".join(render(front)).replace("\\", "\\\\").replace('"', '\\"'))

    subs = {
        "TITLE": " (legacy)" if LEGACY else "",
        "FACE": CFG.face,
        "FW": str(CFG.cols * FONT_W),
        "FH": str(CFG.rows * FONT_H),
        "GW": str(FONT_W),
        "GH": str(FONT_H),
        "ROWS": str(CFG.rows),
        "COLS": str(CFG.cols),
        "DEPTH": str(24 if LEGACY else CFG.depth),
        "FRAMEMS": str(CFG.frame_ms),
        "LASTFRAME": str(CFG.n_frames - 1),
        "FRAMES": "[" + ",".join('"%s"' % s for s in frames) + "]",
        "FRONTS": "[" + ",".join(str(v) for v in fronts) + "]",
        "FONT": base64.b64encode(font_bytes()).decode("ascii"),
    }
    html = HTML_TEMPLATE
    for key, value in subs.items():
        html = html.replace("%%" + key + "%%", value)

    with open(path, "w", encoding="utf-8") as fp:
        fp.write(html)
    print("wrote %s  (%d frames, %.0f KB)" % (path, CFG.n_frames,
                                              os.path.getsize(path) / 1024.0))
    return 0


# ------------------------------------------------------------------------------------ main

def cmd_show(args):
    step = int(args[0]) if args else 8
    for f in range(0, CFG.n_frames, max(step, 1)):
        front = front_at(f)
        print_grid(render(front), f, front)
    return 0


def cmd_front(args):
    front = int(args[0]) if args else 0
    print_grid(render(front), None, front)
    return 0


def cmd_play(args):
    fps = int(args[0]) if args else 30
    for f in range(CFG.n_frames):
        front = front_at(f)
        sys.stdout.write("\033[H\033[2J")
        print_grid(render(front), f, front)
        sys.stdout.flush()
        time.sleep(1.0 / max(fps, 1))
    return 0


COMMANDS = {
    "check": cmd_check,
    "show": cmd_show,
    "front": cmd_front,
    "play": cmd_play,
    "glyphs": cmd_glyphs,
    "html": cmd_html,
}


def main(argv):
    global LEGACY

    args = list(argv[1:])
    if args and args[0] == "legacy":
        LEGACY = True
        args.pop(0)

    name = args.pop(0) if args else "check"
    if name in ("-h", "--help", "help") or name not in COMMANDS:
        print(__doc__.strip())
        return 0 if name in ("-h", "--help", "help") else 2
    return COMMANDS[name](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
