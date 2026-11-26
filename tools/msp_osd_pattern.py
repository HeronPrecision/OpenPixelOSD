#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyserial"]
# ///
"""Draw test patterns on the OpenPixelOSD over a serial adapter, using MSP DisplayPort.

  uv run tools/msp_osd_pattern.py --list-ports
  uv run tools/msp_osd_pattern.py --port /dev/cu.usbserial-XXXX --pattern all
  uv run tools/msp_osd_pattern.py --pattern checker --dry-run        # preview in the terminal, no serial

Wiring: a 3.3 V USB-UART adapter to the OSD's UART (adapter TX -> OSD RX, adapter RX -> OSD TX,
common ground), 115200 8N1. Do not run this while a flight controller is driving the same UART.

Protocol (src/msp_displayport.c): MSP v1 frame '$' 'M' '<' size cmd payload crc, crc = XOR of size,
cmd and payload. MSP_DISPLAYPORT (182) sub-commands: 0 heartbeat, 2 clear, 3 write string
{row, col, attr, text...}, 4 draw. The FIRST heartbeat after the OSD boots makes it answer
MSP_SET_OSD_CANVAS (188) {cols, rows}; if you miss that reply, reset the OSD to get another.
The canvas is 30 columns x 16 rows. The OSD wipes the canvas ~8 s after boot (splash + boot wave),
so the tool redraws every second.

Notes: the default OSD font has no lowercase (codes 0x61-0x7A are Betaflight symbol glyphs), so
text is uppercased unless --raw is given. The firmware does not bounds-check col + length on
DRAW_STRING, so this tool always clamps strings to the row width.
"""
import argparse
import sys
import time

COLS, ROWS = 30, 16
MSP_DISPLAYPORT = 182
MSP_SET_OSD_CANVAS = 188
DP_HEARTBEAT, DP_RELEASE, DP_CLEAR, DP_WRITE_STRING, DP_DRAW = 0, 1, 2, 3, 4


# ---------------------------------------------------------------- MSP framing
def msp_frame(cmd, payload=b""):
    size = len(payload)
    if size > 255:
        raise ValueError("payload too large")
    crc = size ^ cmd
    for b in payload:
        crc ^= b
    return b"$M<" + bytes([size, cmd]) + bytes(payload) + bytes([crc])


def dp(sub, extra=b""):
    return msp_frame(MSP_DISPLAYPORT, bytes([sub]) + extra)


class FrameParser:
    """Minimal MSP v1 parser for the OSD's replies ('$M<' or '$M>' header)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.state, self.buf = 0, []

    def feed(self, data):
        out = []
        for b in data:
            if self.state == 0:
                self.state = 1 if b == 0x24 else 0
            elif self.state == 1:
                self.state = 2 if b == 0x4D else 0
            elif self.state == 2:
                self.state = 3 if b in (0x3C, 0x3E, 0x21) else 0
            elif self.state == 3:
                self.size, self.buf, self.state = b, [], 4
            elif self.state == 4:
                self.cmd, self.state = b, (6 if self.size == 0 else 5)
            elif self.state == 5:
                self.buf.append(b)
                if len(self.buf) == self.size:
                    self.state = 6
            elif self.state == 6:
                crc = self.size ^ self.cmd
                for x in self.buf:
                    crc ^= x
                if crc == b:
                    out.append((self.cmd, bytes(self.buf)))
                self.reset()
        return out


# ---------------------------------------------------------------- canvas model
class Canvas:
    def __init__(self, raw=False):
        self.raw = raw
        self.cells = [[0x20] * COLS for _ in range(ROWS)]

    def put(self, row, col, text):
        if isinstance(text, str):
            text = text if self.raw else text.upper()
            text = text.encode("latin-1", "replace")
        for i, b in enumerate(text):
            if 0 <= row < ROWS and 0 <= col + i < COLS:
                self.cells[row][col + i] = b

    def frames(self):
        """clear, one write-string per non-blank run of each row (clamped to the row), draw."""
        out = [dp(DP_CLEAR)]
        for r, row in enumerate(self.cells):
            idx = [i for i, b in enumerate(row) if b != 0x20]
            if not idx:
                continue
            a, b = idx[0], idx[-1]
            run = bytes(row[a:b + 1])[: COLS - a]
            out.append(dp(DP_WRITE_STRING, bytes([r, a, 0]) + run))
        out.append(dp(DP_DRAW))
        return out

    def preview(self):
        def ch(b):
            return chr(b) if 0x20 <= b < 0x7F else "?"
        top = "+" + "-" * COLS + "+"
        return "\n".join([top] + ["|" + "".join(ch(b) for b in row) + "|" for row in self.cells] + [top])


# ---------------------------------------------------------------- patterns
def p_border(c, t):
    c.put(0, 0, "+" + "-" * (COLS - 2) + "+")
    c.put(ROWS - 1, 0, "+" + "-" * (COLS - 2) + "+")
    for r in range(1, ROWS - 1):
        c.put(r, 0, "|")
        c.put(r, COLS - 1, "|")
    c.put(ROWS // 2 - 1, 1, "BORDER".center(COLS - 2))
    c.put(ROWS // 2, 1, ("%dx%d" % (COLS, ROWS)).center(COLS - 2))


def p_grid(c, t):
    c.put(0, 0, "".join(str(i % 10) for i in range(COLS)))
    for r in range(1, ROWS):
        c.put(r, 0, "%d" % (r % 10))
        c.put(r, 1, "." * (COLS - 1))
    c.put(0, 0, "".join(str(i % 10) for i in range(COLS)))


def p_checker(c, t):
    # Alternating glyphs: good for judging horizontal jitter and vertical stability.
    for r in range(ROWS):
        c.put(r, 0, "".join("#" if (r + col) % 2 == 0 else "." for col in range(COLS)))


def p_stripes(c, t):
    for r in range(ROWS):
        c.put(r, 0, ("#" if r % 2 == 0 else "=") * COLS)


def p_charset(c, t):
    # All 256 font codes, 16 per row (columns 7..22); label rows with the high nibble.
    for hi in range(16):
        c.put(hi, 2, "%X" % hi)
        c.put(hi, 4, "-")
        c.put(hi, 7, bytes((hi << 4) | lo for lo in range(16)))


def p_banner(c, t):
    msg = t.text or "MSP OSD TEST"
    c.put(ROWS // 2 - 1, (COLS - len(msg)) // 2, msg)
    stamp = time.strftime("%H:%M:%S")
    c.put(ROWS // 2 + 1, (COLS - len(stamp)) // 2, stamp)


def p_marquee(c, t):
    # Text scrolls one column per frame on a few rows: visible if line timing or vsync wanders.
    msg = (t.text or "OPENPIXELOSD  ") * 4
    off = t.frame % len(msg)
    line = (msg[off:] + msg[:off])[:COLS]
    for r in (2, 7, 12):
        c.put(r, 0, line)
    c.put(0, 0, ("FRAME %d" % t.frame).ljust(COLS))


PATTERNS = {
    "border": p_border, "grid": p_grid, "checker": p_checker, "stripes": p_stripes,
    "charset": p_charset, "banner": p_banner, "marquee": p_marquee,
}


class Ctx:
    text = ""
    frame = 0


# ---------------------------------------------------------------- serial session
def list_ports():
    from serial.tools import list_ports as lp
    ports = list(lp.comports())
    if not ports:
        print("no serial ports found")
    for p in ports:
        print("%-28s %s" % (p.device, p.description))


def open_port(path, baud):
    import serial
    return serial.Serial(path, baud, bytesize=8, parity="N", stopbits=1, timeout=0.05)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", help="serial device (see --list-ports)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--pattern", default="all", help="one of %s or 'all' (cycle)" % ", ".join(PATTERNS))
    ap.add_argument("--text", default="", help="text for banner / marquee")
    ap.add_argument("--hold", type=float, default=4.0, help="seconds per pattern when cycling (default 4)")
    ap.add_argument("--duration", type=float, default=0.0, help="stop after this many seconds (0 = run until Ctrl-C)")
    ap.add_argument("--raw", action="store_true", help="do not uppercase text (lowercase codes are symbol glyphs)")
    ap.add_argument("--dry-run", action="store_true", help="no serial: print a terminal preview and the first frames")
    ap.add_argument("--list-ports", action="store_true")
    a = ap.parse_args()

    if a.list_ports:
        list_ports()
        return 0
    names = list(PATTERNS) if a.pattern == "all" else [a.pattern]
    for n in names:
        if n not in PATTERNS:
            ap.error("unknown pattern %r" % n)

    ctx = Ctx()
    ctx.text = a.text

    def render(name):
        c = Canvas(raw=a.raw)
        PATTERNS[name](c, ctx)
        return c

    if a.dry_run:
        for n in names:
            c = render(n)
            print("== %s ==" % n)
            print(c.preview())
            fr = c.frames()
            print("%d frames, %d bytes; first: %s\n" % (len(fr), sum(len(f) for f in fr), fr[0].hex(" ")))
        return 0

    if not a.port:
        ap.error("--port is required (or use --dry-run / --list-ports)")
    ser = open_port(a.port, a.baud)
    parser = FrameParser()
    t_start = time.time()
    last_hb = 0.0
    last_draw = 0.0
    cur = 0
    cur_since = t_start
    canvas_seen = False

    def poll_reply():
        nonlocal canvas_seen
        data = ser.read(256)
        for cmd, payload in parser.feed(data):
            if cmd == MSP_SET_OSD_CANVAS and len(payload) >= 2:
                canvas_seen = True
                print("OSD canvas reply: %d columns x %d rows" % (payload[0], payload[1]))

    ser.write(dp(DP_HEARTBEAT))
    deadline = time.time() + 2.0
    while time.time() < deadline and not canvas_seen:
        poll_reply()
    if not canvas_seen:
        print("no canvas reply (normal if the OSD was already opened by a host since it booted); drawing anyway")
    print("drawing %s; Ctrl-C to stop" % ("all patterns" if len(names) > 1 else names[0]))

    try:
        while True:
            now = time.time()
            if a.duration and now - t_start >= a.duration:
                break
            if len(names) > 1 and now - cur_since >= a.hold:
                cur = (cur + 1) % len(names)
                cur_since = now
            animated = names[cur] == "marquee"
            if now - last_hb >= 0.5:
                ser.write(dp(DP_HEARTBEAT))
                last_hb = now
            if now - last_draw >= (0.1 if animated else 1.0):
                ctx.frame += 1
                for f in render(names[cur]).frames():
                    ser.write(f)
                last_draw = now
            poll_reply()
    except KeyboardInterrupt:
        pass
    finally:
        ser.write(dp(DP_CLEAR))
        ser.write(dp(DP_DRAW))
        ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
