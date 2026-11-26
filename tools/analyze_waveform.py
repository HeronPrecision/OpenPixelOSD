#!/usr/bin/env python3
"""Analyse a composite-video scope capture (Siglent SDS CSV, or any 'time,volts' CSV) and show
what the COMP3 sync comparator would do with it at different thresholds.

Usage:  python3 -I tools/analyze_waveform.py CAPTURE.csv [--hyst-mv 10]

Prints:
  * sync tip / blanking / burst / picture levels,
  * for a sweep of comparator thresholds, how many falling edges the comparator would produce
    (ideal = exactly one per line), so chatter from colour burst or picture content shows up,
  * the threshold window that gives the cleanest result and its midpoint.

Stdlib only. Levels are in volts at the probe point (record where you probed in doc/camera-waveforms/).
"""
import argparse
import bisect
import csv
import statistics


def load(path):
    t, v = [], []
    with open(path, newline="") as f:
        for row in csv.reader(f):
            if len(row) >= 2:
                try:
                    t.append(float(row[0]))
                    v.append(float(row[1]))
                except ValueError:
                    pass
    if len(v) < 1000:
        raise SystemExit("no usable samples found in %s" % path)
    return t, v


def falling_edges(v, th, hyst):
    """Count comparator falling edges (video dips below th-hyst) with hysteresis."""
    lo, hi = th - hyst, th + hyst
    state = v[0] > th
    falls = []
    for i, x in enumerate(v):
        if state and x < lo:
            state = False
            falls.append(i)
        elif (not state) and x > hi:
            state = True
    return falls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--hyst-mv", type=float, default=10.0, help="comparator hysteresis (LL_COMP_HYSTERESIS_LOW ~10 mV)")
    a = ap.parse_args()
    t, v = load(a.csv)
    dt = t[1] - t[0]
    hyst = a.hyst_mv / 1000.0
    print("samples %d, span %.1f us, dt %.2f ns, min %.3f V, max %.3f V" % (len(v), (t[-1] - t[0]) * 1e6, dt * 1e9, min(v), max(v)))

    # Levels: the lowest dense cluster is the sync tip.
    bins = {}
    for x in v:
        bins[round(x / 0.01)] = bins.get(round(x / 0.01), 0) + 1
    peak = max(bins.values())
    dense = sorted(k for k, c in bins.items() if c > 0.25 * peak)
    tip_bin = dense[0]
    tip = [x for x in v if abs(x - tip_bin * 0.01) < 0.04]
    tip_med, tip_max = statistics.median(tip), max(tip)
    print("sync tip   : median %.3f V, range %.3f..%.3f V (n=%d)" % (tip_med, min(tip), tip_max, len(tip)))

    # Colour burst / picture dips: lowest level reached in the first 12 us after each tip's rising edge.
    # (Look at the capture itself for the exact back-porch level; this just bounds the sweep.)
    span_us = (t[-1] - t[0]) * 1e6
    lines = max(1, round(span_us / 63.6))

    # Sweep only thresholds that can possibly work: above the tip, up to tip + 0.45 V.
    lo_th = tip_med + 0.01
    hi_th = tip_med + 0.45
    print("\nthreshold sweep (hysteresis %.0f mV); ideal = %d falling edge(s) per capture (one per line):" % (a.hyst_mv, lines))
    print("  th(mV)  falling-edges")
    sweep = []
    th = lo_th
    while th <= hi_th + 1e-9:
        n = len(falling_edges(v, th, hyst))
        sweep.append((th, n))
        print("  %5.0f   %d" % (th * 1000, n))
        th += 0.01
    best = min(n for _, n in sweep)
    runs, cur = [], []
    for th, n in sweep:
        if n <= best + 2:          # tolerate a couple of stray edges
            cur.append(th)
        else:
            if cur:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    r = max(runs, key=len)
    print("\nbest window: %.0f..%.0f mV with <= %d falling edges (ideal %d), midpoint %.0f mV"
          % (r[0] * 1000, r[-1] * 1000, best + 2, lines, (r[0] + r[-1]) / 2 * 1000))
    if best + 2 > lines * 2:
        print("WARNING: no threshold gives one clean edge per line; the signal chatters even at the best")
        print("         setting (colour burst, ringing or noise reaching the sync level). A fixed threshold")
        print("         alone will not be stable; consider a filter ahead of the comparator or blanking.")


if __name__ == "__main__":
    main()
