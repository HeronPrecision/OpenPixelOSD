/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * video_config.h - Single place for all analog-video tuning (levels in millivolts).
 *
 * Everything here depends on the board's analog front end (input 75R termination, the
 * SN74LVC1G3157 input-select switch's DC pedestal, the OPAMP1 x2 output gain + 75R
 * back-termination) and on the camera. If the video fails to lock, or the OSD brightness
 * looks wrong, THIS is the one file to retune. Every value may also be overridden at build
 * time with -D<NAME>=<value> (see CMakeLists.txt cache vars).
 *
 * NOTE: this header is included from main.h *after* the STM32 LL headers (it needs the
 * LL_COMP_HYSTERESIS_* macros). Do not #include it directly - include "main.h".
 */
#ifndef VIDEO_CONFIG_H
#define VIDEO_CONFIG_H

/* ---- Sync detection --------------------------------------------------------------------
 * DAC1_CH1 sets the '-' reference of COMP3 (camera sync, PA0) and COMP4 (generator sync, PB0).
 * Sync is detected when the video dips BELOW this level, so it must sit between the sync tip
 * and the blanking level.
 *   FC1 measured (DC-coupled, at VDIN): sync tip ~216 mV, blanking ~480 mV -> 350 mV midpoint.
 *   (The historical firmware used ~162 mV - below the FC1 sync tip - so sync never triggered.)
 */
#ifndef VIDEO_SYNC_THRESHOLD_MV
#define VIDEO_SYNC_THRESHOLD_MV   350
#endif

/* COMP3/COMP4 input hysteresis: LL_COMP_HYSTERESIS_LOW (10 mV) / _MEDIUM (40 mV) / _HIGH (70 mV).
 * Raise if back-porch noise or sync-edge ringing double-triggers the sync comparators. */
#ifndef VIDEO_SYNC_HYSTERESIS
#define VIDEO_SYNC_HYSTERESIS     LL_COMP_HYSTERESIS_LOW
#endif

/* ---- OSD pixel output levels -----------------------------------------------------------
 * Overlay pixel brightness, programmed into DAC3_CH1. These are the levels at the DAC; OPAMP1's
 * x2 gain and the 75R back-termination divider net to unity into a 75R load, so they are
 * effectively what the monitor sees. For 1 Vpp CVBS: black ~ blanking, white near peak. Tune
 * if OSD text is too dim/bright or the black background box isn't opaque enough. */
#ifndef VIDEO_DAC_BLACK_MV
#define VIDEO_DAC_BLACK_MV        450
#endif
#ifndef VIDEO_DAC_GRAY_MV
#define VIDEO_DAC_GRAY_MV         650
#endif
#ifndef VIDEO_DAC_WHITE_MV
#define VIDEO_DAC_WHITE_MV        900
#endif

#endif /* VIDEO_CONFIG_H */
