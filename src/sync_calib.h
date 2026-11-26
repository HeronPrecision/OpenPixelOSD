/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * sync_calib.h - pick the COMP3 sync threshold from a sweep. No firmware dependencies, so
 * tools/sync_calib_test.c can run the same code on a host against recorded camera data.
 *
 * The firmware sweeps the threshold (DAC1_CH1) and, at each step, counts the intervals between
 * COMP3 rising edges (the end of every sync pulse):
 *   good  = interval in the line window (59..65 us)  -> the line parser would count a line
 *   other = interval that is neither a line nor a vsync half-line -> chatter
 * A threshold that sits inside the colour burst or the picture produces chatter and lets the
 * line parser miss lines (on the RC4 camera, 350 mV recognised ZERO lines; 140..190 mV got all).
 *
 * Choice: a threshold is usable if it recognises (nearly) as many lines as the best step of the
 * sweep. Among contiguous runs of usable thresholds take the widest one (most margin against
 * level drift) and use its centre. Ties go to the run with less chatter.
 */
#ifndef SYNC_CALIB_H
#define SYNC_CALIB_H

#include <stdint.h>

#define SYNC_CAL_MAX_POINTS   64
#define SYNC_CAL_MIN_GOOD     200u   /* per dwell: below this there is no real lock anywhere */

typedef struct {
    uint16_t th_mv;     /* threshold under test, ascending in the array */
    uint32_t good;      /* line-window intervals counted during the dwell */
    uint32_t other;     /* chatter intervals counted during the dwell */
} sync_cal_point_t;

/* Returns 1 and fills *out_mv (chosen threshold) and *width_mv (width of the usable run) when a
 * usable window exists, 0 otherwise (caller keeps its default threshold). */
static inline int sync_cal_pick(const sync_cal_point_t *p, int n, uint16_t *out_mv, uint16_t *width_mv)
{
    uint32_t max_good = 0;
    int i;

    for (i = 0; i < n; i++) {
        if (p[i].good > max_good) {
            max_good = p[i].good;
        }
    }
    if (max_good < SYNC_CAL_MIN_GOOD) {
        return 0;
    }
    const uint32_t need = max_good - max_good / 10u;      /* >= 90 % of the best step */

    int best_a = -1, best_b = -1;
    uint32_t best_other = 0;
    i = 0;
    while (i < n) {
        if (p[i].good < need) {
            i++;
            continue;
        }
        int a = i;
        uint32_t other = 0;
        while (i < n && p[i].good >= need) {
            other += p[i].other;
            i++;
        }
        int b = i - 1;
        int len = b - a;
        int best_len = best_a < 0 ? -1 : best_b - best_a;
        if (len > best_len || (len == best_len && other < best_other)) {
            best_a = a;
            best_b = b;
            best_other = other;
        }
    }
    if (best_a < 0) {
        return 0;
    }
    *out_mv = (uint16_t)(((uint32_t)p[best_a].th_mv + p[best_b].th_mv) / 2u);
    *width_mv = (uint16_t)(p[best_b].th_mv - p[best_a].th_mv);
    return 1;
}

#endif /* SYNC_CALIB_H */
