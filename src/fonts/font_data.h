/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * font_data.h - shared OSD font descriptor for the 12x18 2bpp glyph table.
 *
 * The glyph bytes themselves live in exactly one compiled source file
 * (src/fonts/font_data_<face>.c); CMake's OSD_FONT selects which face is
 * linked in (default | plexmono | adwaita | iosevka). Every face defines the
 * same `font_data[16384]` in section(".font").
 */
#ifndef FONT_DATA_H
#define FONT_DATA_H
#include <stdint.h>

#define FONT_WIDTH      (12)
#define FONT_HEIGHT     (18)

#define FONT_BPP        (2) // 2bits per pixel
#define FONT_CHARS      (256)
#define BYTES_PER_ROW   ((FONT_WIDTH * FONT_BPP) / 8)     // = 3
#define BYTES_PER_CHAR  (BYTES_PER_ROW * FONT_HEIGHT)     // = 54
#define FONT_STRIDE     (sizeof(font_data) / FONT_CHARS)  // = 64

extern const uint8_t font_data[16384];

#endif /* FONT_DATA_H */
