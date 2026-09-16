# Vector OSD — design notes for `chickadee-vectors`

Status: **research only, nothing implemented.** Branch `chickadee-vectors` cut at `3663204`.

Goal: replace the character-cell artificial horizon (and later the crosshair) with a smoothly
rasterised line, by *detecting* the AH glyphs the flight controller sends over MSP DisplayPort
and reconstructing the line they encode. Also: a vector boot animation.

## Why reconstruction rather than `MSP_ATTITUDE`

DisplayPort carries no attitude. The obvious alternative — poll `MSP_ATTITUDE` (cmd 108) and
draw our own horizon — means reimplementing each firmware's AHI scaling (BF's `ahMaxPitch` /
`ahMaxRoll` / `ahInvert` and its ~80 % vertical shrink; AP's aspect correction and pitch
scaling) or the horizon won't match what the user configured. Reconstruction inherits all of
that for free, costs no extra UART traffic on the 115200 link, and needs no FC-side config.

The glyph stream is less lossy than it looks: the sub-cell offset is encoded *in the glyph
code*, so each point carries 2-scanline precision, and the FC generated the dots from a line
analytically in the first place. Fitting a line back through them is reconstruction, not
invention.

## Verified: what our own font actually contains

Dumped from `src/fonts/font_data_terminus.c` (which preserves the Betaflight symbol block) with
`tools/wave_preview.py`'s font parser:

| Codes | Content |
|---|---|
| `0x80`–`0x88` | 9 AH bars. 2px-wide white dot, **white row = 2 × (code − 0x80)** → rows 0,2,4,…,16 |
| `0x89` | not a bar (segmented, rows 7–11) |
| `0x8A`, `0x8F` | vertical stubs at cols 10–11 / 0–1 — horizon **sidebar** pieces |
| `0x8B`–`0x8E` | full-width box/sidebar pieces |
| `0x90` | solid block, cols 2–9 |

Note there are **no true vertical AH bar glyphs** in the Betaflight font. That fact is the root
of the ArduPilot problem below.

## Verified: per-firmware symbol tables

### Betaflight — `betaflight/betaflight`

- `SYM_AH_BAR9_0 = 0x80`, nine codes `0x80`–`0x88` (`AH_SYMBOL_COUNT = 9`).
- Encoding: `glyph = SYM_AH_BAR9_0 + (y % 9)`, `row = elemPosY + (y / 9)`, y increasing
  **downward**. So sub-cell offset in scanlines = `(code − 0x80) × 2`.
- Span: 9 columns, `x = -4 … +4` relative to the element position.
- `y = ((-rollAngle * x) / 64) - pitchAngle`, with `pitchAngle = ((pitch * 25) / maxPitch) - 41`,
  roll/pitch constrained to `ahMaxRoll` / `ahMaxPitch` (× 10), sign flipped by `ahInvert`.
- Crosshair: `SYM_AH_CENTER_LINE = 0x72`, `SYM_AH_CENTER = 0x73`,
  `SYM_AH_CENTER_LINE_RIGHT = 0x74` — three adjacent cells.
- No vertical bars.

### ArduPilot — `ArduPilot/ardupilot`, `AP_OSD_MSP_DisplayPort` (BTFL symbol mode)

```cpp
static const uint8_t SYM_AH_H_START = 0x80;   static const uint8_t SYM_AH_H_COUNT = 9;
static const uint8_t SYM_AH_V_START = 0x82;   static const uint8_t SYM_AH_V_COUNT = 6;
static const uint8_t SYM_AH_CENTER_LINE_LEFT = 0x84;
static const uint8_t SYM_AH_CENTER_LINE_RIGHT = 0x84;
static const uint8_t SYM_AH_CENTER = 0x2B;    // '+'
```

Three consequences, all of which break a naive shared implementation:

1. **The vertical range overlaps the horizontal range.** `0x82`–`0x87` sits inside `0x80`–`0x88`.
   AP switches to vertical bars when `|sin(roll)| >= |cos(roll)|` (`draw_horizon`, loop
   `dy = -4…4`), but the BF font has no vertical bar glyphs, so it reuses horizontal ones. At
   steep roll AP's DisplayPort output is genuinely ambiguous — the same code means two things.
2. **AP's sub-cell order is inverted relative to BF:**
   `symbol = SYM_AH_H_START + ((SYM_AH_H_COUNT - 1) - c)` where `c = (fy - floorf(fy)) * COUNT`.
   Higher fraction → *lower* code. BF is the other way round. One shared decode table would put
   the horizon a cell out.
3. **The crosshair reuses an AH bar code.** `SYM_AH_CENTER_LINE_LEFT/RIGHT = 0x84`, which is also
   the middle horizon bar. A naive detector pulls the two crosshair cells in as horizon points.

Other AP facts: horizontal span `dx = -4…4` (9 columns); aspect correction
`get_aspect_ratio_correction() = 12.0/18.0` for MSP DisplayPort; `MSP_FC_VARIANT` = `"ARDU"`;
an alternative iNav symbol mapping exists (`AP_MSP_INAV_FONTS_ENABLED`, `ap_to_inav_symbols_map`).

### iNav — `iNavFlight/inav`

```c
#define SYM_AH_H_START   0x14C   // 332 to 340 Horizontal AHI  (9 codes)
#define SYM_AH_V_START   0x15A   // 346 to 351 Vertical AHI    (6 codes)
#define SYM_AH_CH_CENTER 0x166   // 358 Crosshair center
```

These are **beyond 8 bits** — iNav uses font code pages. **Prerequisite:** our DisplayPort
handler currently discards the attribute byte:

```c
/* src/msp_displayport.c, MSP_DISPLAYPORT_DRAW_STRING */
uint8_t len = data_size - 4;
memcpy(&canvas_char_map[active_buffer][row][col], &payload[4], len);   // payload[3] dropped
```

`payload[3]` carries the attribute/page bits. Until that is handled, iNav's extended glyphs
cannot be rendered *at all*, let alone detected. This is independent of the vector work and
worth fixing on its own merits.

### PX4 — `PX4/PX4-Autopilot`

`msp_osd` sets `osd_artificial_horizon_pos = LOCATION_HIDDEN` — **PX4 never draws a horizon over
DisplayPort.** BF-compatible AH symbols exist in `src/drivers/osd/atxxxx/symbols.h` (`0x80`–`0x87`)
but `msp_osd` does not use them. Nothing to detect; no work required. Out of scope.

## Design implications

1. **Cell centre is the trustworthy signal; sub-cell offset is a per-profile refinement.**
   A cell position is unambiguous under every firmware and gives the line to ±9 px. The sub-cell
   offset sharpens that to ±1 px but only where the profile says the encoding is unambiguous
   (BF always; AP only in horizontal mode). Design the fit so losing the refinement degrades
   resolution rather than breaking correctness.
2. **One decode table per firmware, selected by `MSP_FC_VARIANT`** (cmd 2 → `"BTFL"` / `"ARDU"` /
   `"INAV"`). Master-side request plumbing already exists — see `vtx_msp_request_config()` in
   `src/vtx_msp.c` and `construct_msp_command_v1()` / `uart1_tx_dma()`.
3. **The fit must be outlier-robust**, not plain least-squares, because of AP's `0x84` crosshair
   collision. Reject-and-refit or a median-of-slopes estimator.
4. **Geometric gate before substituting anything:** enough points, roughly one per column,
   contiguous column band, residual under threshold. This is what protects us if a firmware uses
   these codes for something else — arbitrary text will not sit collinear across a band.
5. **Fallback is the absence of an action.** If the gate rejects, write the glyphs exactly as
   today. Worst case is current behaviour, so the failure mode is free.
6. Detection runs once per `MSP_DISPLAYPORT_DRAW_SCREEN`, not per scanline. The per-line cost is a
   span fill, which is *cheaper* than the 9 glyph blits it replaces.

## Sequencing

1. **Vector boot wave** — self-generated, zero protocol dependencies, proves the per-scanline line
   rasterizer. `src/boot_wave.h` is already pure geometry with no firmware deps, and
   `tools/wave_preview.py --html` already renders real 360×288 pixels, so a vector wave previews
   through the identical harness.
2. **Crosshair** — static, fixed codes, first real glyph substitution.
3. **Horizon** — profile tables, robust fit, geometric gate.

Separately, and independent of all of the above: handle `payload[3]` in `DRAW_STRING` so iNav
renders correctly at all.

## Architecture reminder

The G431 (`LOW_RAM`) path has **no framebuffer** — it is already a race-the-beam rasterizer.
`dac_buff[2][413]` + `opamp_buff[2][413]` ≈ 5 KB total, `buf_idx = (line & 1)`: one scanline is
DMA'd out while the next is computed. Budget is ~64 µs per PAL line at 170 MHz ≈ **10,900 cycles**.
RAM is not the constraint (16.5 / 32 KB used, CCM 5.4 / 10 KB); cycles per scanline are.
