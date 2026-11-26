# `chickadee` branch — status and build notes (2026-10-02)

This branch is the Heron Precision build of OpenPixelOSD for the Chickadee flight
controller's "HYDRA" OSD section (STM32G431CBU6, no USB, talks to the flight controller over
UART only). It is what runs on shipping boards. `master` tracks upstream.

## What differs from upstream (in order)

| Commit | Change |
|---|---|
| 78da120 | Hardware adaptation: USB stack removed, OPAMP1 as PGA x2 for the 75 R back-terminated output, comparator sync threshold raised to 350 mV, camera/generator arbitration fixed. Commit message has the measurements. |
| 8facec9, b15b565, b959323 | Heron Precision / Chickadee splash: logo bitmap, text lines, `HW:` label, uppercase. |
| c43e1a6 | Status LED breathing via TIM8 hardware PWM. |
| 3663204 | Terminus font rendered natively; `OSD_FONT` build selector (`default` or `terminus`). |
| 6b25707 | Boot `+` wave animation (`OSD_BOOT_ANIMATION`, geometry in `src/boot_wave.h`, host preview in `tools/wave_preview.py`); boot LED blinks and video-lock breathing; sync threshold as a runtime variable; `OSD_TUNING_MODE` for live threshold sweeps. |
| 3d12f8a | `doc/vector-osd.md`: design notes for the `chickadee-vectors` branch. |

## Building the release image

```
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DTARGET_MCU=STM32G431
cmake --build build
# -> build/OpenPixelOSD_STM32G431.bin (131072 bytes)
```

Defaults are `OSD_FONT=terminus`, `OSD_BOOT_ANIMATION=ON`, no threshold/hysteresis
overrides. The reference image built from this tree on 2026-08-09 has md5
`c1548861af608d8583f80df96e3520a2`; a clean rebuild of commit 6b25707 reproduces it byte for
byte. Sanity check a binary with `strings -n 5 x.bin | grep -i heron`.

Diagnostic variants (`CMakeLists.txt`): `VIDEO_PASSTHROUGH_TEST=ON` (static camera
passthrough, no overlay), `VIDEO_CAMERA_ONLY=ON` (no generator fallback), `OSD_TUNING_MODE=ON`
(hold splash, sweep the sync threshold with on-screen readout), `VIDEO_SYNC_THRESHOLD_MV`,
`VIDEO_SYNC_HYSTERESIS`.

## How it is deployed

The flight controller's host MCU carries this binary inside its own firmware and programs
the G431 through the ST UART bootloader at power-up (compare, erase, write, verify), then
talks MSP DisplayPort to it at 115200 8N1. That host firmware lives in a separate private
repository. Two behaviours of this firmware matter to it:

- `MSP_SET_OSD_CANVAS` (30 x 16) is sent only in reply to the **first** DisplayPort heartbeat
  after boot. A host that missed it must reset the G431 to get another.
- The OSD listens roughly 4.5 s after reset. Splash (`LOGO_TIMEOUT_MS`, 4 s) then the boot
  wave repaint and finally clear the canvas, so anything drawn before about 8 s after reset
  is wiped; the host redraws periodically.

## Runtime behaviour worth knowing

- Status LED: three full-brightness blinks after every reset, then dark while no camera sync
  is locked, slow breathing once it is. No camera means dark, which is normal.
- Sync threshold default 350 mV (`VIDEO_SYNC_THRESHOLD_MV` in `src/video_config.h`), chosen on
  2026-07-21 from a sweep of 100 to 500 mV plus 325 mV, 350 mV with HIGH hysteresis and a
  camera-only build. It is now a runtime variable, so future tuning does not need rebuilds.
- Font: terminus was picked on 2026-07-27 after rendering adwaita, iosevka, menlo, plexmono
  and terminus candidates with the `python/` scripts.

## Sync threshold auto-calibration (2026-11-26)

`OSD_AUTO_SYNC_CAL` (CMake option, default ON; forced off in tuning and passthrough builds).
At power-up, and whenever a camera appears after the internal generator was running, the firmware
sweeps COMP3's threshold (DAC1_CH1) from 40 to 600 mV in 10 mV steps, 40 ms each. The TIM2
interrupt counts line-length intervals (59 to 65 us) versus chatter per step, and
`src/sync_calib.h` picks the centre of the widest window in which nearly every line is
recognised. If no window exists the fixed `VIDEO_SYNC_THRESHOLD_MV` (350) stays. The generator
(COMP4) always uses the fixed value. The splash is held back until the sweep ends and then
shows `SYNC nnnMV AUTO` and `WIN lo-hiMV` (or `SYNC 350MV DEFAULT`).

Why: the RC4 camera has its sync tip at ~110 mV and colour burst down to ~180 mV, so the old
fixed 350 mV sat inside the burst and the line parser recognised no lines at all (vertical
wander, blurred text). Evidence and per-camera numbers: `doc/camera-waveforms/`. Any change to
sync detection must be rechecked against every capture listed there. Host test:
`cc -o /tmp/t tools/sync_calib_test.c && /tmp/t`.

Known limits: a camera that produces no edges at all at the default threshold never starts the
sweep; signals that swing below 0 V need AC coupling and a clamp in hardware first.

Related fix: the linker's `.ccmram_bss` is NOLOAD and the startup code never zeroed it, so CCM
variables (`video_gen_enabled`, `new_field`, `msp_uart`, ...) started with random or stale data.
Both startup files now zero-fill it.

## Open items

1. `BOOT_WAVE_DURATION_MS` in `src/boot_wave.h` is 4000, a "10x for testing" value. The
   intended value is about 400. Changing it changes the shipped binary.
2. `chickadee-vectors` (cut at 3663204): rasterised artificial horizon reconstructed from the
   DisplayPort glyphs, plus a vector boot animation. Research only; see `doc/vector-osd.md`.
3. Gitignored working directories (`build*/`, `sync-sweep/`, `python/candidates/`,
   `tools/*.html`) are regenerable from this tree and are not part of the repo.
