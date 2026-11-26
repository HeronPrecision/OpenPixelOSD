/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * boot_wave.h - pure geometry of the power-up OSD "wave" flourish.
 *
 * A diagonal band sweeps from the top-left corner to the bottom-right one over
 * BOOT_WAVE_DURATION_MS, right after the splash clears.
 *
 * This header is the one place the look is defined, and it deliberately depends on nothing
 * from the firmware - no HAL, no main.h, no canvas - so tools/wave_preview.py can read every
 * constant below straight out of it and render the animation on a host. Workshop the look
 * here, then, without flashing a board:
 *
 *   tools/wave_preview.py check     assert both band edges stay unbroken straight lines
 *   tools/wave_preview.py show      ASCII frames in the terminal
 *   tools/wave_preview.py html      watch it play, real glyphs, real 4:3 aspect
 *   tools/wave_preview.py glyphs    measured ink coverage, for choosing BOOT_WAVE_RAMP
 *
 * Geometry: a cell at (row, col) sits on diagonal (row + col). `front` is the diagonal the
 * leading edge has reached, so d = front - (row + col) is how many diagonals a cell lies
 * BEHIND that edge. d < 0 is ahead of the wave, d >= BOOT_WAVE_DEPTH is behind its tail.
 * That formula is the one thing wave_preview.py mirrors rather than reads; keep the two in
 * step if it ever changes.
 */
#ifndef BOOT_WAVE_H
#define BOOT_WAVE_H

#include <stdint.h>

#define BOOT_WAVE_DURATION_MS   3000u   /* was 4000 (~33% faster); 10x slower than the original ~400 test value */
#define BOOT_WAVE_FRAME_MS      15u     /* ~66 fps; must divide BOOT_WAVE_DURATION_MS so the last frame is the full sweep */

/* Thickness of the band, in diagonals. Must equal the length of BOOT_WAVE_RAMP. */
#define BOOT_WAVE_DEPTH         8

/* Glyph ramp, indexed by d. Light at the leading edge, heaviest at the crest, light again
 * at the trailing edge, so the band reads as a wave rather than a moving block.
 *
 * EVERY cell inside the band is lit - the taper lives in glyph WEIGHT, never in whether a
 * cell is drawn. That is the whole point. The previous version tapered by dithering cell
 * PRESENCE, keyed on (row + col); but (row + col) is the very quantity that picks the band
 * level, so the key was constant along each diagonal and the "dither" could only switch a
 * whole diagonal fully on or fully off. What it actually drew was detached one-cell-per-row
 * diagonals trailing the band - the vertical-looking strands, and the reason isolated
 * columns appeared to jut out past the trailing edge. At 30x16 character resolution there
 * is no room for a spatial dither in any case: a lit cell is a 12x18 px glyph, far too
 * coarse to average out. Weight is the only lever that keeps both edges an unbroken line.
 *
 * Glyph choice is by measured ink coverage in the linked font - `make -C tools glyphs`
 * prints the table. Keep the ramp monotone by that metric, and avoid lowercase: codes
 * 0x61-0x7A are Betaflight symbol glyphs in every face (see canvas_char_write_upper). */
#define BOOT_WAVE_RAMP  { '.', ':', '+', '#', '#', '+', ':', '.' }

/* Total distance the front travels: past the last diagonal, (rows-1)+(cols-1), by the full
 * band depth, so the tail clears the bottom-right corner before the animation ends. */
static inline int32_t boot_wave_span(int32_t rows, int32_t cols)
{
    return (rows - 1) + (cols - 1) + BOOT_WAVE_DEPTH;
}

/* Leading-edge diagonal at `elapsed_ms` into the animation. */
static inline int32_t boot_wave_front(uint32_t elapsed_ms, int32_t rows, int32_t cols)
{
    const int32_t span = boot_wave_span(rows, cols);

    if (elapsed_ms >= BOOT_WAVE_DURATION_MS) {
        return span;
    }
    return (int32_t)((elapsed_ms * (uint32_t)span) / BOOT_WAVE_DURATION_MS);
}

/* Glyph for a cell d diagonals behind the leading edge, or '\0' if the cell is not in the
 * band (ahead of the wave, or behind its tail). */
static inline char boot_wave_glyph(int32_t d)
{
    static const char ramp[BOOT_WAVE_DEPTH] = BOOT_WAVE_RAMP;

    if (d < 0 || d >= BOOT_WAVE_DEPTH) {
        return '\0';
    }
    return ramp[d];
}

#endif /* BOOT_WAVE_H */
