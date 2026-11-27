# Camera waveform reference

Scope captures of the composite video the OSD sees, with the numbers that matter for sync
detection (COMP3 threshold, `VIDEO_SYNC_THRESHOLD_MV` in `src/video_config.h`). Add every new
camera/board capture here. **Any change to sync detection must be re-checked against every
capture in this index**, so tuning for one camera does not break an older tested one.

Analyse a capture with `python3 -I tools/analyze_waveform.py CAPTURE.csv`. It prints the sync
tip level and how many falling edges the comparator would produce at each threshold (ideal: one
per line).

Raw CSVs are large (12 MB) and live in OneDrive, not in git:
`OneDrive-HeronPrecision/Projects/Chip Design/Chickadee/OSD Camera Waveforms/<capture folder>/`
(original scope output also kept in `~/Personal/fpv analog camera waveforms/`).

## Capture index

| ID | Date | Board / build | Camera | Probe point | Scope | Files |
|---|---|---|---|---|---|---|
| FC1-ref | 2026-07-21 | FC1, chickadee threshold sweep | (tested cameras, model not recorded) | VDIN, DC coupled | (not recorded) | Numbers only, from commit 78da120 |
| RC4-A | 2026-11-26 | RC4 `001E003E-3434510D-37323639`, passthrough / OSD builds | **TBD (model not recorded)** | RC4 video input, straight off the camera | Siglent SDS824X HD, C1, DC 1 MΩ, 10X, 500 mV/div, 20 µs/div, 2 GSa/s, 400 kpts, trigger falling edge 192 mV | `SDS824X_HD_CSV_C1_1.csv` (sha256 `a2f5258b…c01f54`), `2026-11-26-rc4-A-single.png`, `2026-11-26-rc4-A-persistence.png` |

Notes/markers column to fill as tests are added (what was running, OSD on/off, what the cursors
marked): RC4-A single = clean 3-line capture (Stop); persistence = many lines overlaid.

## Reference levels (volts at the probe point)

| | FC1-ref | RC4-A |
|---|---|---|
| Sync tip | ~0.216 | **~0.11** (range 0.06–0.14, 4.5 µs wide) |
| Blanking / back porch | ~0.480 | **~0.40** |
| Colour burst swing | n/a | **0.18 – 0.62** (centred on blanking, ~±0.22 V) |
| Active picture | n/a | 0.31 – 0.93 (peak 1.26 over a 200 µs window) |
| Line period | | 63.6 µs (3 lines in the capture) |
| Current threshold (350 mV) | midpoint of tip and blanking, worked | **inside the burst and picture range** |

## Finding for RC4-A: why the OSD is vertically unstable and blurred

The firmware threshold is 350 mV. On this camera the colour burst dips as low as ~0.18 V and
the active picture as low as ~0.31 V, so a 350 mV threshold sits **inside the burst and picture
swing**. The comparator fires many times per line instead of once (analysis: 95 falling edges per
3 lines at 350 mV, ideal 3; 464 at 400 mV). Every spurious edge re-triggers the line timing (TIM2
capture, TIM1 external trigger), so line start jitters (horizontal blur on the text edges) and the
line/field counter, which accepts only 59–65 µs and 29–33 µs spacings, mis-counts (vertical
wander).

Falling edges per 200 µs capture, 10 mV hysteresis (ideal 3):

| Threshold (mV) | 130 | 140 | 150 | 160 | 170 | 180 | 190 | 200 | 250 | 300 | **350** | 400 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Edges | 38 | 20 | 17 | 14 | 16 | 12 | 14 | 21 | 39 | 39 | **95** | 464 |

Best window is 140–190 mV (midpoint ~165 mV) but even there ~12–16 edges remain; the sync-tip
ceiling (~0.14–0.15 V) and burst floor (~0.18 V) are only ~30–40 mV apart, with ringing on the
tip (e.g. an undershoot to -0.046 V 2 µs into the second sync pulse). A threshold change alone
will not make this clean.

## Compatibility constraint

A fixed threshold cannot serve both references: ~165 mV (good for RC4-A) is **below** the FC1-ref
sync tip (216 mV), so those cameras would never lock. 350 mV (good for FC1-ref) chatters on RC4-A.
Options, in order of preference:

1. **Auto-calibrate the threshold** at boot / on lock loss: sweep the DAC1 threshold, count COMP3
   edges per field, pick the middle of the stable plateau (one edge per line). Camera-agnostic;
   keep 350 mV as the fallback when no plateau is found.
2. **Filter the video before COMP3** (RC low-pass, corner around 1 MHz) to remove burst and picture
   detail from the sync path; makes any threshold far more forgiving (hardware change).
3. Use the comparator blanking source to ignore the back porch after each sync edge.

## Result

`OSD_AUTO_SYNC_CAL` (option 1 above) is implemented (`src/sync_calib.h`, `src/video_overlay.c`,
host test `tools/sync_calib_test.c`). On RC4-A it locks and the OSD text is stable; the splash
glitches briefly while the sweep runs, then recovers. The splash shows the picked threshold
(`SYNC nnnMV AUTO`, `WIN lo-hiMV`). FC1-ref cameras are covered by the algorithm
(synthetic test only); re-verify on the original FC1 cameras before shipping to them.

Still open: the camera model for RC4-A (the index row says model not recorded).

## 2026-11-27 — another FC's OSD output (bipolar, 0 V-referenced)

File: `2026-11-27-fc-osd-out-bipolar.csv` (Siglent SDS824X HD, CH1, 20 µs, 0.5 ns/sample; analysis in
`-analysis.txt`). Probed at another FC's OSD output. Reported: the MS1207 analog-to-USB adapter
handles it, our RC4 does not.

Levels (one line): sync tip **-0.53 V**, back porch / blanking **-0.29 V**, colour burst swings
**-0.48..-0.09 V** (centred on blanking), picture/white **~0 V (-0.1..+0.06)**. Sync depth only
~240 mV, whole signal ~0.55 Vpp and **entirely at or below 0 V**.

Why the RC4 fails: the G431 input (PA7 -> OPAMP PGA -> PA2/COMP3) is DC-coupled and cannot go
below 0 V; every negative level is clipped, so there is no sync to see. This is not fixable by
the DAC threshold or the auto-calibration (which sweeps 40..600 mV). Even if level-shifted, the
burst troughs (-0.48 V) come within ~50 mV of the tip, so the usable window is ~20 mV wide
(-496..-476 mV raw) and a low-pass/blanking filter on the sync path would be needed too.

Needed in hardware (input side of the OSD module): AC-couple (≈ 1 µF-10 µF series cap) and a sync-tip
clamp to a bias (e.g. tip clamped to ~+0.2 V), so any DC reference becomes tip-low, positive.
That is what the MS1207's front end does. Gain may also be needed (sync only 240 mV here).

### Clamped-input simulation (video-clamp targets), `tools/clampsim.py`

`python3 -I tools/clampsim.py doc/camera-waveforms/2026-11-27-fc-osd-out-bipolar.csv [--fc-mhz 1.6] [--gain 2]`
shifts the tip to 0.25 V (ideal clamp), applies a single-pole low-pass and sweeps the comparator
threshold in 5.47 mV steps (MSPM0 COMP DAC). Plateau = thresholds giving exactly one falling edge.
Re-run it on every new capture, as with `analyze_waveform.py`.

| Filter (gain x1, 10 mV hyst.) | Plateau | Width |
|---|---|---|
| none | none usable (the "886..892" it prints is a trace-start artefact) | ~5 mV |
| 1.6 MHz (1k + 100 pF) | 284..465 mV | 181 mV (34 steps) |
| 1.0 MHz (1k + 150 pF) | 284..481 mV | 197 mV (37 steps) |
| 1.6 MHz, gain x2 | 306..667 mV | 361 mV |

Decision (FC1 RC6 / OSD module, `video-clamp` branches): keep 1k + 100 pF, no gain stage. A sharper
edge keeps line-start timing, and so horizontal text stability, better than 15 mV of extra window.
The plateau sits at about 280-465 mV, so the centre is near 375 mV. Range edges move 1:1 with the
real clamped tip level (the tip is taken as the 1st-percentile sample, -0.557 V here).

Caveats: **one line only** (no vsync/serration), and an **ideal clamp** (tip exactly 0.25 V, no
diode drop, leakage or droop over the line). If the RC4 bodge is built, scope the clamped node and
record the real tip level and its droop over a line.
