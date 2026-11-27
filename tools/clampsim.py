#!/usr/bin/env python3
"""Simulate an input clamp + low-pass in front of the sync comparator, on a scope capture.

Usage:  python3 -I tools/clampsim.py CAPTURE.csv [--tip-mv 250] [--fc-mhz 1.6] [--gain 1] [--step-mv 5.47]
                                      [--hyst-mv 10]

Takes a 'time,volts' CSV (Siglent SDS, see analyze_waveform.py), shifts the sync tip to --tip-mv
(an ideal sync-tip clamp), scales by --gain about the tip, applies a single-pole low-pass at
--fc-mhz (0 = none) and sweeps the comparator threshold in --step-mv steps. Prints the widest
threshold range that gives exactly ONE falling edge over the capture (the sync pulse, with no
burst or picture chatter) = the usable window. Companion to analyze_waveform.py for targets that
clamp the video before the comparator (video-clamp branches).

Limits: ideal clamp only (tip exactly at --tip-mv, no diode drop, leakage or droop over the
line), and a capture of one line says nothing about vsync/serration. Stdlib only.
"""
import argparse
import csv
import math


def load(path):
    t, v = [], []
    for r in csv.reader(open(path, newline="")):
        try:
            t.append(float(r[0]))
            v.append(float(r[1]))
        except (ValueError, IndexError):
            pass
    if len(v) < 1000:
        raise SystemExit("no usable samples in %s" % path)
    return t, v


def lpf(x, dt, fc):
    if not fc:
        return x[:]
    a = dt / (dt + 1 / (2 * math.pi * fc))
    y = [x[0]]
    for s in x[1:]:
        y.append(y[-1] + a * (s - y[-1]))
    return y


def falls(y, th, h):
    state, n = y[0] > th, 0
    for s in y:
        if state and s < th - h:
            state, n = False, n + 1
        elif not state and s > th + h:
            state = True
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--tip-mv", type=float, default=250.0, help="clamped sync tip level")
    ap.add_argument("--fc-mhz", type=float, default=1.6, help="low-pass corner, 0 = none")
    ap.add_argument("--gain", type=float, default=1.0)
    ap.add_argument("--step-mv", type=float, default=5.47, help="comparator DAC step")
    ap.add_argument("--hyst-mv", type=float, default=10.0)
    a = ap.parse_args()

    t, v = load(a.csv)
    dt = t[1] - t[0]
    tip = sorted(v)[len(v) // 100]          # ~1st percentile = sync tip
    x = [(s - tip) * a.gain + a.tip_mv / 1000 for s in v]
    y = lpf(x, dt, a.fc_mhz * 1e6)
    step, hyst = a.step_mv / 1000, a.hyst_mv / 1000
    runs, cur = [], []
    k = int(0.05 / step)
    while k * step < 1.2:
        th = k * step
        if falls(y, th, hyst) == 1:
            cur.append(th)
        elif cur:
            runs.append(cur)
            cur = []
        k += 1
    if cur:
        runs.append(cur)
    print("tip %.0f mV (raw %.3f V) gain %.1f  fc %s  hyst %.0f mV  step %.2f mV"
          % (a.tip_mv, tip, a.gain, "none" if not a.fc_mhz else "%.1f MHz" % a.fc_mhz, a.hyst_mv, a.step_mv))
    if not runs:
        print("no threshold gives a single clean edge")
        return
    r = max(runs, key=len)
    print("plateau %.0f..%.0f mV, width %.0f mV (%d steps)" % (r[0] * 1e3, r[-1] * 1e3, (r[-1] - r[0]) * 1e3, len(r)))


if __name__ == "__main__":
    main()
