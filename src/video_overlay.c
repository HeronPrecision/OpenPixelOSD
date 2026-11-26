/* SPDX-License-Identifier: GPL-2.0-only */
/**
 * Copyright (C) 2025 Vitaliy N <vitaliy.nimych@gmail.com>
 */
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "video_overlay.h"
#include "system.h"
#include "main.h"
#include "video_gen.h"
#include "logo/logo.h"
#include "canvas_char.h"
#include "video_graphics.h"
#ifdef OSD_BOOT_ANIMATION
#include "boot_wave.h"
#endif
#ifdef OSD_AUTO_SYNC_CAL
#include "sync_calib.h"
#endif
#if defined(LOW_RAM)
#include "fonts/font_data.h"
#endif

#define TIM2_TICK_MS        (1e6f / 170000000)

// OPAMP1 multiplexer constants: raw OPAMP1_CSR values written on-the-fly by DMA to switch the
// mux input per pixel. Mode = non-inverting PGA gain x2 (VMSEL=PGA, PGGAIN=0) to compensate the
// 75R output back-termination (R7) into the 75R load; a unity follower (0x108000E_) would halve
// the output. Keep the gain in sync with OPAMP1_Init() in opamp.c.
#define OPAMP_CONST_IO0     0x108000C1U  // Positive Input IO0 (e.g., PA1)
#define OPAMP_CONST_IO1     0x108000C5U  // Positive Input IO1 (e.g., PA3) - video generator input
#define OPAMP_CONST_IO2     0x108000C9U  // Positive Input IO2 (e.g., PA7) - video input from camera
#define OPAMP_CONST_DAC     0x108000CDU  // Positive Input DAC (internal DAC3_CH1) - pixel brightness

#define PEXELS_PER_LINE     (360)
#define BACK_PORCH_LEN      (26) // + DMA started with delay ~2.5us
#define OFFSET_Y            (16)
#define OFFSET_X            (BACK_PORCH_LEN)
#define LINE_BUF_SZ         (BACK_PORCH_LEN + OFFSET_X + PEXELS_PER_LINE+1)

#define DAC_BLACK           DAC12BIT_FROM_MV(VIDEO_DAC_BLACK_MV)
#define DAC_WHITE           DAC12BIT_FROM_MV(VIDEO_DAC_WHITE_MV)
#define DAC_GRAY            DAC12BIT_FROM_MV(VIDEO_DAC_GRAY_MV)
#define MAX_RENDER_LINE     (305) // for PAL

#define LOGO_OFFSET_X       (120)
#define LOGO_OFFSET_Y       (25)

static uint16_t dac_buff[2][LINE_BUF_SZ];   // DAC double buffer for draw pixel (12-bit CH1)  DMA HALF_WORLD/WORLD
static uint32_t opamp_buff[2][LINE_BUF_SZ]; // double buffer for OPAMP1 multiplexer (32-bit)  DMA WORLD/WORLD
CCMRAM_BSS static bool buf_idx = 0; // current buffer index for double buffering
CCMRAM_DATA static uint32_t video_source = OPAMP_CONST_IO2; // OPAMP_CONST_IO1 - video gen, OPAMP_CONST_IO2 - video input
extern volatile bool video_gen_enabled;
extern char canvas_char_map[2][ROW_SIZE][COLUMN_SIZE];
extern uint8_t active_buffer;
CCMRAM_DATA bool show_logo = true;
CCMRAM_BSS bool new_field = false;

/* Runtime sync-detection threshold (mV). Defaults to VIDEO_SYNC_THRESHOLD_MV; the OSD_TUNING_MODE
 * harness sweeps it live. video_apply_sync_threshold() pushes it to DAC1_CH1 (COMP3/COMP4 '-'). */
uint16_t g_video_sync_threshold_mv = VIDEO_SYNC_THRESHOLD_MV;

#if defined(HIGH_RAM)
extern uint8_t active_video_buffer;
extern uint8_t video_frame_buffer[2][VIDEO_HEIGHT][VIDEO_BYTES_PER_LINE];
#endif

EXEC_RAM static void init_buffers()
{
    for (uint32_t j = 0; j < LINE_BUF_SZ; j++) {
        dac_buff[0][j] = DAC_GRAY;
        opamp_buff[0][j] = video_source;
        dac_buff[1][j] = DAC_GRAY;
        opamp_buff[1][j] = video_source;
    }
}

/* The default OSD font (font_bf_default.h) has no lowercase letters: codes
 * 0x61-0x7A hold Betaflight symbol glyphs (heading/home arrows, unit icons)
 * that a flight controller drives over MSP DisplayPort. Uppercase the splash
 * text so it renders as readable letters instead of those symbols. */
static void canvas_char_write_upper(uint8_t x, uint8_t y, const char *s)
{
    char buf[COLUMN_SIZE];
    uint16_t i = 0;

    for (; s[i] != '\0' && i < COLUMN_SIZE; i++) {
        char c = s[i];
        buf[i] = (c >= 'a' && c <= 'z') ? (char)(c - 'a' + 'A') : c;
    }
    canvas_char_write(x, y, buf, i);
}

static void show_version(void)
{
    char str[COLUMN_SIZE];

    canvas_char_write_upper(8, 9, "Heron Precision");
    canvas_char_write_upper(8, 10, "HW: Chickadee");
    sprintf(str, "FW: %s", FW_VERSION);
    canvas_char_write_upper(8, 11, str);
    canvas_char_draw_complete();
}

/* Push the sync threshold -> DAC1_CH1 (the COMP3/COMP4 '-' reference). Called at init, from
 * video_gen_start/stop, by the tuning sweep and by the auto-calibration whenever the value changes.
 * COMP3 (camera) uses g_video_sync_threshold_mv, which auto-calibration may change. COMP4 (internal
 * generator) keeps the fixed default: its level does not depend on the camera. */
void video_apply_sync_threshold_for(bool generator_active)
{
#ifdef OSD_AUTO_SYNC_CAL
    const uint16_t mv = generator_active ? (uint16_t)VIDEO_SYNC_THRESHOLD_MV : g_video_sync_threshold_mv;
#else
    (void)generator_active;
    const uint16_t mv = g_video_sync_threshold_mv;
#endif
    LL_DAC_ConvertData12RightAligned(DAC1, LL_DAC_CHANNEL_1, DAC12BIT_FROM_MV(mv));
    LL_DAC_TrigSWConversion(DAC1, LL_DAC_CHANNEL_1);
}

void video_apply_sync_threshold(void)
{
    video_apply_sync_threshold_for(video_gen_enabled);
}

#ifdef OSD_AUTO_SYNC_CAL
/* ---- Sync-threshold auto-calibration -------------------------------------------------------
 * Camera levels differ (the RC4 camera has its sync tip at ~110 mV and colour burst down to
 * ~180 mV, the FC1 cameras ~216 mV / 480 mV blanking), and one fixed threshold cannot serve both:
 * at 350 mV the RC4 camera's line parser recognised no lines at all. So at power-up (and whenever
 * a camera appears after the generator was running) we sweep DAC1_CH1 and let the TIM2 interrupt
 * count, per step, line-length intervals (59..65 us) versus chatter. sync_calib.h picks the centre
 * of the widest usable window; if none exists the fixed default stays. See
 * doc/camera-waveforms/ and tools/sync_calib_test.c. */
#define CAL_START_MV     40
#define CAL_END_MV       600
#define CAL_STEP_MV      10
#define CAL_DWELL_MS     40      /* ~2 fields per step */
#define CAL_PROBE_MS     100     /* wait this long for ANY edges before sweeping */
#define CAL_MIN_EDGES    100     /* edges in CAL_PROBE_MS that mean "a camera is there" */
#define CAL_TICKS_PER_US 170u    /* TIM2 clock */

static volatile uint8_t  g_cal_active;
static volatile uint32_t g_cal_edges, g_cal_good, g_cal_other;
uint16_t g_video_cal_threshold_mv;      /* 0 = not calibrated (default in use) */
uint16_t g_video_cal_width_mv;
static uint32_t g_cal_end_tick;

/* Quiet seek: the splash is not drawn until calibration has finished, so the seek shows plain
 * camera video with an empty canvas (the same state as after the splash times out). The line
 * pipeline is NOT touched during the sweep. g_splash_pending gates the splash, the boot animation
 * and the logo timeout (main.c). */
bool     g_splash_pending = true;
uint32_t g_splash_release_tick;
extern bool g_msp_host_seen;

/* Called from TIM2_IRQHandler for every camera edge while a sweep is running. */
static inline void cal_count_interval(uint32_t ticks)
{
    g_cal_edges++;
    if (ticks > 59u * CAL_TICKS_PER_US && ticks < 65u * CAL_TICKS_PER_US) {
        g_cal_good++;                                   /* a line */
    } else if (ticks >= 1u * CAL_TICKS_PER_US &&
               !(ticks > 29u * CAL_TICKS_PER_US && ticks < 33u * CAL_TICKS_PER_US)) {
        g_cal_other++;                                  /* chatter (not a vsync half-line, not a <1 us double trigger) */
    }
}

bool video_sync_cal_busy(void)
{
    return g_cal_active != 0;
}
#endif /* OSD_AUTO_SYNC_CAL */

#ifdef OSD_BOOT_ANIMATION
/* Boot "wave" flourish: a diagonal band sweeps from the top-left corner to the bottom-right
 * one over BOOT_WAVE_DURATION_MS, starting the instant the splash clears, and leaves the
 * canvas clear when it is done.
 *
 * The shape of the band lives in src/boot_wave.h, which has no firmware dependencies so that
 * tools/wave_preview.py can render and validate it on a host:
 *   tools/wave_preview.py check     assert both edges stay unbroken straight lines
 *   tools/wave_preview.py html      watch it, real glyphs, real aspect, in a browser
 * Workshop the look in that header, not here. */
void video_boot_animation_update(void)
{
    static uint8_t  state = 0;       /* 0 = waiting for splash to clear, 1 = running, 2 = done */
    static uint32_t start_tick = 0;
    static uint32_t last_frame = 0;

    if (state == 2) {
        return;
    }
    if (state == 0) {
        if (show_logo
#ifdef OSD_AUTO_SYNC_CAL
            || g_splash_pending
#endif
            ) {
            return;                  /* splash still up (or not yet shown) -- wait */
        }
        start_tick = HAL_GetTick();
        last_frame = start_tick;
        state = 1;
    }

    const uint32_t now     = HAL_GetTick();
    const uint32_t elapsed = now - start_tick;

    if (elapsed >= BOOT_WAVE_DURATION_MS) {
        canvas_char_clean();         /* clear + arm swap */
        canvas_char_draw_complete();
        state = 2;
        return;
    }
    if ((now - last_frame) < BOOT_WAVE_FRAME_MS) {
        return;                      /* throttle to ~60 fps */
    }
    last_frame = now;

    const int32_t front = boot_wave_front(elapsed, ROW_SIZE, COLUMN_SIZE);

    canvas_char_clean();             /* clear active buffer + arm the swap */
    for (uint8_t r = 0; r < ROW_SIZE; r++) {
        for (uint8_t c = 0; c < COLUMN_SIZE; c++) {
            /* Diagonal distance behind the leading edge. Mirrored in wave_preview.py. */
            const char glyph = boot_wave_glyph(front - (int32_t)r - (int32_t)c);
            if (glyph != '\0') {
                canvas_char_map[active_buffer][r][c] = glyph;
            }
        }
    }
    canvas_char_draw_complete();
}
#endif /* OSD_BOOT_ANIMATION */

#ifdef OSD_TUNING_MODE
/* --- Interactive tuning harness (no scope needed) ---------------------------------------
 * With no oscilloscope, the OSD text is the instrument. This slowly auto-sweeps the COMP3 sync
 * threshold across a safe range and prints the current value + the boot reset-cause (PIN/BOR) on
 * the (held) splash, so the warm-reset horizontal smear can be watched sharpen/blur against the
 * number by eye. Enable with -DOSD_TUNING_MODE=ON; splash timeout is disabled under the same flag
 * in main.c::logo_timeout_check.
 *
 * The full splash is redrawn each tick so both canvas double-buffers carry identical text (no
 * flicker as the threshold sweeps). */
#define SWEEP_MIN_MV     260u
#define SWEEP_MAX_MV     440u
#define SWEEP_STEP_MV    20u
#define SWEEP_PERIOD_MS  2000u

void video_draw_tuning_splash(void)
{
    char str[COLUMN_SIZE];
    /* canvas_char_draw_complete() only swaps the display buffer when canvas_write_next_buff is
     * armed (by flush_map/clean, not by canvas_char_write). clean() clears the active buffer to
     * spaces AND re-arms the swap, so the THR line we write here actually publishes. */
    canvas_char_clean();
    canvas_char_write_upper(8, 9, "Heron Precision");
    canvas_char_write_upper(8, 10, "HW: Chickadee");
    snprintf(str, sizeof(str), "FW: %s", FW_VERSION);
    canvas_char_write_upper(8, 11, str);
    snprintf(str, sizeof(str), "THR %dmV RST:%s", (int)g_video_sync_threshold_mv, g_reset_cause);
    canvas_char_write_upper(8, 13, str);
    canvas_char_draw_complete();
}

/* Called every main-loop iteration; steps the threshold every SWEEP_PERIOD_MS. */
void video_tuning_step(void)
{
    static uint32_t last_tick = 0;
    static int8_t dir = 1;
    const uint32_t now = HAL_GetTick();

    if ((now - last_tick) < SWEEP_PERIOD_MS) {
        return;
    }
    last_tick = now;

    int32_t v = (int32_t)g_video_sync_threshold_mv + (int32_t)dir * (int32_t)SWEEP_STEP_MV;
    if (v >= (int32_t)SWEEP_MAX_MV) {
        v = (int32_t)SWEEP_MAX_MV;
        dir = -1;
    } else if (v <= (int32_t)SWEEP_MIN_MV) {
        v = (int32_t)SWEEP_MIN_MV;
        dir = 1;
    }
    g_video_sync_threshold_mv = (uint16_t)v;

    video_apply_sync_threshold();
    video_draw_tuning_splash();
}
#endif /* OSD_TUNING_MODE */

void video_overlay_init(void)
{
#ifdef VIDEO_PASSTHROUGH_TEST
    /* DIAGNOSTIC: static camera passthrough, PA7 (camera in) -> OPAMP1 -> PA2 (video out).
     * All sync detection, the internal video generator and the OSD overlay are skipped, so the
     * ONLY thing exercised is the analog path PA7 -> OPAMP1 -> PA2.
     *
     * OPAMP1 defaults to non-inverting PGA gain x2 to compensate the 75R output back-termination
     * (R7) feeding the downstream 75R load: that series+load divider halves the signal, so a x2
     * buffer restores unity at the load. Build with -DVIDEO_PASSTHROUGH_UNITY for a raw x1
     * follower instead (~half-amplitude / darker output) to compare the two.
     */
    OPAMP1_Init(); // configures PA7 (in) / PA2 (out) analog pins; defaults to PGA gain x2
#ifdef VIDEO_PASSTHROUGH_UNITY
    LL_OPAMP_SetFunctionalMode(OPAMP1, LL_OPAMP_MODE_FOLLOWER); // x1: raw, ~half amplitude
#else
    LL_OPAMP_SetFunctionalMode(OPAMP1, LL_OPAMP_MODE_PGA);
    LL_OPAMP_SetPGAGain(OPAMP1, LL_OPAMP_PGA_GAIN_2_OR_MINUS_1); // x2
#endif
    LL_OPAMP_Enable(OPAMP1);
    return;
#endif
    init_buffers();
    canvas_char_flush_map();

#if defined(HIGH_RAM)
    video_graphics_init();
#endif

    DAC1_Init(); // DAC1_CH1 for video detection
    DAC3_Init(); // DAC3_CH1 for render line
    OPAMP1_Init(); // OPAMP1 as multiplexer for video source selection
    TIM1_Init(); // TIM1 for video line generation
    TIM2_Init(); // TIM2 detect HSYNC VSYNC video input
    TIM3_Init(); // TIM3 detect HSYNC VSYNC video generator
    TIM4_Init(); // TIM4 delay for start video generator
    TIM17_Init(); // TIM17 for video generator output (PWM ~31.8us)
    COMP3_Init(); // COMP3 for video sync detection
    COMP4_Init(); // COMP4 for video generator sync detection

    LL_OPAMP_Enable(OPAMP1);

    LL_COMP_Enable(COMP3);
    LL_COMP_Enable(COMP4);

    LL_TIM_EnableIT_CC1(TIM2);
    LL_TIM_EnableCounter(TIM2);
    LL_TIM_CC_EnableChannel(TIM2, LL_TIM_CHANNEL_CH1);

    LL_TIM_EnableIT_CC1(TIM3);
    LL_TIM_EnableCounter(TIM3);
    LL_TIM_CC_EnableChannel(TIM3, LL_TIM_CHANNEL_CH1);

    LL_DAC_Enable(DAC1, LL_DAC_CHANNEL_1);
    video_apply_sync_threshold();

    LL_DAC_Enable(DAC3, LL_DAC_CHANNEL_1);
    LL_DAC_ConvertData12RightAligned(DAC3, LL_DAC_CHANNEL_1, DAC_BLACK);
    LL_DAC_TrigSWConversion(DAC3, LL_DAC_CHANNEL_1);

#ifndef VIDEO_CAMERA_ONLY
    // TIM4 is the "no camera sync" watchdog that starts the internal video generator.
    // -DVIDEO_CAMERA_ONLY disables it (and thus the generator fallback), so the OSD relies
    // purely on COMP3 camera-sync detection. Diagnostic: isolates the sync-detect path from
    // the camera/generator arbitration that otherwise flip-flops when detection is marginal.
    LL_TIM_EnableIT_UPDATE(TIM4);
    LL_TIM_EnableCounter(TIM4);
#endif

#ifdef OSD_AUTO_SYNC_CAL
    show_logo = false;           /* quiet seek: the splash is released after calibration */
    g_splash_pending = true;
#else
    show_version();
#endif
#ifdef OSD_TUNING_MODE
    video_draw_tuning_splash();   // tuning splash: version + threshold + boot reset-cause
#endif
}

#if defined(LOW_RAM)
EXEC_RAM static void squash_canvas_raw_pixel_buff(char c, uint32_t glyph_row, uint32_t x_off)
{
    register const uint8_t * const glyph = &font_data[(uint8_t)c * FONT_STRIDE];
    register const uint32_t row_offset = glyph_row * BYTES_PER_ROW;
    register uint16_t dac_val;
    register uint32_t opa_val;

    for(register uint32_t col = 0; col < FONT_WIDTH; col++) {
        register uint32_t bitpos     = col * FONT_BPP;
        register uint32_t byte_index = row_offset + (bitpos >> 3);
        register uint32_t bit_offset = bitpos & 0x7;

        register uint8_t raw_byte = glyph[byte_index];
        register uint8_t pixel = (raw_byte >> (6 - bit_offset)) & 0x03;

        switch (pixel) {
        case PX_BLACK: dac_val = DAC_BLACK; opa_val = OPAMP_CONST_DAC; break;
        case PX_WHITE: dac_val = DAC_WHITE; opa_val = OPAMP_CONST_DAC; break;
        case PX_GRAY:  dac_val = DAC_GRAY;  opa_val = OPAMP_CONST_DAC; break;
        default:       dac_val = DAC_BLACK; opa_val = video_source;    break;
        }

        dac_buff[buf_idx][x_off + col] = dac_val;
        opamp_buff[buf_idx][x_off + col] = opa_val;
    }
    opamp_buff[buf_idx][x_off + FONT_WIDTH] = video_source;
}
#endif

void render_overlay_logo_line(uint16_t line)
{
    if (line >= MAX_RENDER_LINE) return;

    // Logo line considering vertical offset
    if (line < LOGO_OFFSET_Y || (line - LOGO_OFFSET_Y) >= LOGO_HEIGHT) {
        // Line outside the logo area — do nothing, keep existing buffer contents
        return;
    }

    uint16_t logo_row = line - LOGO_OFFSET_Y;
    const uint8_t *logo_line_ptr = &logo_data[logo_row * LOGO_ROW_BYTES];

    // Buffer index starting from horizontal offset LOGO_OFFSET_X
    uint16_t buf_index = LOGO_OFFSET_X;

    for (uint16_t logo_col = 0; logo_col < LOGO_WIDTH; logo_col++) {
        if (buf_index >= LINE_BUF_SZ) break;

        uint32_t bit_pos = logo_col * 2;
        uint32_t byte_idx = bit_pos >> 3;
        uint32_t shift = 6 - (bit_pos & 7);

        px_t px = (px_t)((logo_line_ptr[byte_idx] >> shift) & 0x3);

        // If pixel is transparent — skip (keep existing buffer value)
        if (px == PX_TRANSPARENT) {
            buf_index++;
            continue;
        }

        uint16_t dac_val;
        uint32_t opa_val;

        switch (px) {
        case PX_BLACK:       dac_val = DAC_BLACK; opa_val = OPAMP_CONST_DAC; break;
        case PX_WHITE:       dac_val = DAC_WHITE; opa_val = OPAMP_CONST_DAC; break;
        case PX_GRAY:        dac_val = DAC_GRAY;  opa_val = OPAMP_CONST_DAC; break;
        case PX_TRANSPARENT:
        default:             dac_val = DAC_BLACK; opa_val = video_source;    break;
        }

        // Overlay — overwrite only if pixel is not transparent
        dac_buff[buf_idx][buf_index] = dac_val;
        opamp_buff[buf_idx][buf_index] = opa_val;

        buf_index++;
    }
}

#if defined(LOW_RAM)
EXEC_RAM static void render_line(uint16_t line)
{
    CCMRAM_BSS static uint32_t draw_line = 0;
    register uint32_t map_row = 0;
    register uint32_t glyph_row = 0;
    register uint32_t line_parity = 0;
    register uint32_t i = 0;
    register char c = 0;

    // Offset current draw line by Y_OFFSET
    if (line < OFFSET_Y) return;
    draw_line = line - OFFSET_Y;

    // Calculate which character row and which row in the glyph
    map_row    = draw_line / FONT_HEIGHT;  // 0..ROW_SIZE-1
    glyph_row  = draw_line % FONT_HEIGHT;  // 0..FONT_HEIGHT-1

    line_parity = draw_line & 1;

    for (i = 0; i < OFFSET_X; i++) {
        opamp_buff[line_parity][i] = video_source;
    }

    if (map_row >= ROW_SIZE) {
        // Out of screen — just transparent
        for (i = OFFSET_X; i < LINE_BUF_SZ; i++) {
            dac_buff[line_parity][i] = DAC_BLACK;
            opamp_buff[line_parity][i] = video_source;
        }
        return;
    }

    // Render each character of the map
    for (i = 0; i < COLUMN_SIZE; i++) {
        c = canvas_char_map[!active_buffer][map_row][i]; // draw previous char map buffer
        squash_canvas_raw_pixel_buff(c, glyph_row, OFFSET_X + i * FONT_WIDTH);
    }

    opamp_buff[buf_idx][LINE_BUF_SZ-1] = video_source;
}
#endif

#if defined(HIGH_RAM)
EXEC_RAM void render_video_line(uint16_t line)
{
    CCMRAM_BSS static uint32_t draw_line = 0;

    for (register int i = 0; i < OFFSET_X; i++) {
        opamp_buff[buf_idx][i] = video_source;
    }

    if (line < OFFSET_Y) {
        for (register int i = OFFSET_X; i < LINE_BUF_SZ; i++) {
            opamp_buff[buf_idx][i] = video_source;
        }
        return;
    }
    draw_line = line - OFFSET_Y;

    uint8_t *line_ptr = video_frame_buffer[!active_video_buffer][draw_line];

    uint32_t buf_idx_local = OFFSET_X;
    for (uint32_t i = 0; i < VIDEO_BYTES_PER_LINE; i++) {
        uint8_t byte = line_ptr[i];
        for (int shift = 6; shift >= 0; shift -= 2) {
            if (buf_idx_local >= LINE_BUF_SZ) break;

            uint8_t pixel = (byte >> shift) & 0x3;

            uint16_t dac_val;
            uint32_t opa_val;

            switch (pixel) {
            case PX_BLACK:       dac_val = DAC_BLACK; opa_val = OPAMP_CONST_DAC; break;
            case PX_WHITE:       dac_val = DAC_WHITE; opa_val = OPAMP_CONST_DAC; break;
            case PX_GRAY:        dac_val = DAC_GRAY;  opa_val = OPAMP_CONST_DAC; break;
            case PX_TRANSPARENT:
            default:             dac_val = DAC_BLACK; opa_val = video_source; break;
            }

            dac_buff[buf_idx][buf_idx_local] = dac_val;
            opamp_buff[buf_idx][buf_idx_local] = opa_val;

            buf_idx_local++;
        }
    }
    opamp_buff[buf_idx][LINE_BUF_SZ-1] = video_source;
}
#endif

EXEC_RAM static void push_line_to_dma(uint16_t line)
{
    buf_idx = (line & 1);
    // Stop TIM1 and both DMA channels
    LL_TIM_DisableCounter(TIM1);
    LL_DMA_DisableChannel(DMA1, LL_DMA_CHANNEL_1);
    LL_DMA_DisableChannel(DMA1, LL_DMA_CHANNEL_2);

    // Configure length and addresses
    LL_DMA_SetMemoryAddress(DMA1, LL_DMA_CHANNEL_1, (uint32_t)&opamp_buff[!buf_idx][0]); // ! send previous buffer
    LL_DMA_SetDataLength(DMA1, LL_DMA_CHANNEL_1, LINE_BUF_SZ);
    LL_DMA_SetMemoryAddress(DMA1, LL_DMA_CHANNEL_2, (uint32_t)&dac_buff[!buf_idx][0]); // ! send previous buffer
    LL_DMA_SetDataLength(DMA1, LL_DMA_CHANNEL_2, LINE_BUF_SZ);

    // Enable DMA request for DAC
    LL_DAC_EnableDMAReq(DAC3, LL_DAC_CHANNEL_1);

    // Enable both DMA channels
    LL_DMA_EnableChannel(DMA1, LL_DMA_CHANNEL_2); // first start DAC channel
    LL_DMA_EnableChannel(DMA1, LL_DMA_CHANNEL_1);

    LL_TIM_EnableDMAReq_UPDATE(TIM1);

    LL_TIM_EnableCounter(TIM1);

#if defined(HIGH_RAM)
    render_video_line(line); // video frame buffer
#else
    render_line(line); // char canvas map
#endif
    if (show_logo) {
        render_overlay_logo_line(line);
    }

}

EXEC_RAM static inline void pars_video_signal(uint32_t tim_tick)
{
    CCMRAM_BSS static uint16_t video_line = 0;

    register float time_ns = (float)tim_tick * TIM2_TICK_MS;
    if(time_ns > 59.f && time_ns < 65.f) {
        video_line++;
        if (video_line <= MAX_RENDER_LINE) {
            push_line_to_dma(video_line);
        }
        if (new_field == false) {
            new_field = true;
        }
    } else if (time_ns > 29.0f && time_ns < 33.0f) {
        video_line = 0;
        if (new_field == true) {
            new_field = false;
        }
    } else {
        // Do nothing, wait for next sync
    }
}

// Called from COMP3 & TIM2 event (video input)
EXEC_RAM void TIM2_IRQHandler(void)
{
    if (LL_TIM_IsActiveFlag_CC1(TIM2)) {
        TIM4->CNT = 0;
        video_source = OPAMP_CONST_IO2;
        OPAMP1->CSR = video_source;
        if (video_gen_enabled == true) {
            video_gen_stop();
            LL_TIM_DisableCounter(TIM1);
            LL_TIM_SetETRSource(TIM1, LL_TIM_TIM1_ETRSOURCE_COMP3);
        } else {
#ifdef OSD_AUTO_SYNC_CAL
            if (g_cal_active) {
                cal_count_interval(TIM2->CCR1);
            }
#endif
            pars_video_signal(TIM2->CCR1);
        }
        LL_TIM_ClearFlag_CC1(TIM2);
    }
}

// Called from COMP4 & TIM3 event (internal video generator sync).
// IMPORTANT: COMP4 must NEVER start the generator - only the TIM4 watchdog (sustained camera
// absence) starts it. Previously this handler restarted the generator whenever it fired with
// the generator stopped; combined with TIM2 stopping the generator on camera sync, that made
// the source flip-flop between camera and generator whenever a camera was present (the
// oscillation seen on the FC1). Here we only render while the generator is already running;
// with it stopped this is a no-op, so a returning camera cleanly and permanently wins.
EXEC_RAM void TIM3_IRQHandler(void)
{
    if (LL_TIM_IsActiveFlag_CC1(TIM3)) {
        if (video_gen_enabled == true) {
            TIM4->CNT = 0;
            video_source = OPAMP_CONST_IO1;
            OPAMP1->CSR = video_source;
            pars_video_signal(TIM3->CCR1);
        }
        LL_TIM_ClearFlag_CC1(TIM3);
    }
}

// Called when video from the camera is not detected
EXEC_RAM void TIM4_IRQHandler(void)
{
    if (LL_TIM_IsActiveFlag_UPDATE(TIM4)) {
        if (video_gen_enabled == false) {
            video_gen_start();
            LL_TIM_DisableCounter(TIM1);
            LL_TIM_SetETRSource(TIM1, LL_TIM_TIM1_ETRSOURCE_COMP4);
        }
        LL_TIM_ClearFlag_UPDATE(TIM4);
    }
}

#ifdef OSD_AUTO_SYNC_CAL
/* Show the splash (once), unless a flight controller already took over the screen. */
static void splash_release(const char *line12, const char *line13)
{
    if (!g_splash_pending) {
        return;
    }
    g_splash_pending = false;
    g_splash_release_tick = HAL_GetTick();
    if (g_msp_host_seen) {
        return;
    }
    show_version();                              /* rows 9-11, calls canvas_char_draw_complete() */
    if (line12) {
        canvas_char_write_upper(8, 12, line12);
    }
    if (line13) {
        canvas_char_write_upper(8, 13, line13);
    }
    canvas_char_draw_complete();
    show_logo = true;
}

/* Run from the main loop. Waits for a locked camera, sweeps the threshold, applies the pick. */
void video_sync_cal_step(void)
{
    enum { CAL_WAIT_CAMERA, CAL_PROBE, CAL_SWEEP, CAL_DONE };
    static uint8_t state = CAL_WAIT_CAMERA;
    static uint32_t t0 = 0;
    static uint16_t th = 0;
    static uint8_t n = 0;
    static bool prev_gen = false;
    static sync_cal_point_t pts[SYNC_CAL_MAX_POINTS];
    const uint32_t now = HAL_GetTick();

    /* A camera appearing after the generator was running needs a fresh calibration. */
    if (prev_gen && !video_gen_enabled && state == CAL_DONE) {
        state = CAL_WAIT_CAMERA;
    }
    prev_gen = video_gen_enabled;

    /* No camera (the generator took over) or something went wrong: show the splash anyway. */
    if (g_splash_pending && (video_gen_enabled || now > 10000u)) {
        splash_release(NULL, NULL);
    }

    switch (state) {
    case CAL_WAIT_CAMERA:
        if (!video_gen_enabled) {               /* camera edges are stopping the generator */
            g_cal_edges = 0;
            g_cal_active = 1;
            t0 = now;
            state = CAL_PROBE;
        }
        break;

    case CAL_PROBE:                              /* is there really a camera signal? */
        if (video_gen_enabled) {                 /* no: the watchdog started the generator */
            g_cal_active = 0;
            state = CAL_WAIT_CAMERA;
        } else if ((now - t0) >= CAL_PROBE_MS) {
            if (g_cal_edges >= CAL_MIN_EDGES) {
                n = 0;
                th = CAL_START_MV;
                g_video_sync_threshold_mv = th;
                video_apply_sync_threshold();
                g_cal_edges = g_cal_good = g_cal_other = 0;
                t0 = now;
                state = CAL_SWEEP;
            } else {
                g_cal_edges = 0;
                t0 = now;
            }
        }
        break;

    case CAL_SWEEP:
        if (video_gen_enabled) {                 /* lost the camera mid-sweep: back to the default */
            g_cal_active = 0;
            g_video_sync_threshold_mv = VIDEO_SYNC_THRESHOLD_MV;
            video_apply_sync_threshold();
            state = CAL_WAIT_CAMERA;
        } else if ((now - t0) >= CAL_DWELL_MS) {
            g_cal_active = 0;                    /* freeze the counters while we read them */
            pts[n].th_mv = th;
            pts[n].good = g_cal_good;
            pts[n].other = g_cal_other;
            n++;
            th += CAL_STEP_MV;
            if (th > CAL_END_MV || n >= SYNC_CAL_MAX_POINTS) {
                uint16_t mv = 0, width = 0;
                const int ok = sync_cal_pick(pts, n, &mv, &width);
                g_video_cal_threshold_mv = ok ? mv : 0;
                g_video_cal_width_mv = ok ? width : 0;
                g_video_sync_threshold_mv = ok ? mv : (uint16_t)VIDEO_SYNC_THRESHOLD_MV;
                video_apply_sync_threshold();
                g_cal_end_tick = now;
                {
                    char l12[COLUMN_SIZE], l13[COLUMN_SIZE];
                    snprintf(l12, sizeof(l12), ok ? "SYNC %uMV AUTO" : "SYNC %uMV DEFAULT",
                             (unsigned)g_video_sync_threshold_mv);
                    snprintf(l13, sizeof(l13), "WIN %u-%uMV", (unsigned)(mv - width / 2u), (unsigned)(mv + width / 2u));
                    splash_release(l12, ok ? l13 : NULL);
                }
                state = CAL_DONE;
            } else {
                g_video_sync_threshold_mv = th;
                video_apply_sync_threshold();
                g_cal_edges = g_cal_good = g_cal_other = 0;
                g_cal_active = 1;
                t0 = now;
            }
        }
        break;

    case CAL_DONE:
    default:
        break;
    }
}
#endif /* OSD_AUTO_SYNC_CAL */
