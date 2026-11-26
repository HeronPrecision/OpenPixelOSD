/* SPDX-License-Identifier: GPL-2.0-only */
/**
 * Copyright (C) 2025 Vitaliy N <vitaliy.nimych@gmail.com>
 */
#include "main.h"
#include "msp_displayport.h"
#include "system.h"
#include "video_gen.h"
#include "video_overlay.h"
#include <stdbool.h>

#if defined(BUILD_VARIANT_VTX)
#include "rtc6705.h"
#include <rf_pa.h>
#endif
#include <stdio.h>

#if defined(HIGH_RAM)
#include "video_graphics.h"
extern bool new_field;
#endif

/* True while the internal sync generator is running (no external camera sync seen for ~1 s);
 * false while an external (camera) video signal is locked and in use. Defined in video_gen.c. */
extern volatile bool video_gen_enabled;

#define DEBUG_LOOP_INTERVAL 100 // milliseconds
#define LOGO_TIMEOUT_MS 4000 // 4 seconds

/* Status LED (PC6, driven by TIM8_CH1 1 kHz hardware PWM -- see tim.c) doubles as an
 * external-video-lock indicator:
 *   - On boot it emits LED_BOOT_BLINKS confirmation blinks (LED_BLINK_MS on / off each) so a
 *     human can see the firmware is alive.
 *   - It then stays OFF until an external (camera) video signal is locked, at which point it
 *     runs the slow breathing envelope below (fade-in/fade-out over 4 s, peaking at
 *     LED_PEAK_PCT duty). When the external signal is lost the internal sync generator takes
 *     over and the LED goes dark again.
 * The carrier runs entirely in hardware (no interrupt), so the video path is untouched; the
 * main loop just steps the PWM duty. "External signal locked" is read from video_gen_enabled:
 * false == generator off == camera signal in use, true == generator running because no camera
 * sync was seen for ~1 s (the TIM4 watchdog). */
#define LED_PWM_ARR      999u  // must match TIM8 Autoreload in TIM8_Init()
#define LED_ENV_STEP_MS  40u   // 100 entries * 40 ms = 4 s breath period
#define LED_PEAK_PCT     10u   // brightest point of the breath (% duty); perceived brighter than 10% (LED response is nonlinear)
#define LED_BOOT_BLINKS  3u    // confirmation blinks emitted once after reset
#define LED_BLINK_MS     200u  // on/off duration of each confirmation blink (ms)
#define LED_BLINK_PCT    100u  // boot-blink brightness (% duty): full-on so the 3 confirmation
                               // blinks read crisp and unmistakable, clearly apart from the soft
                               // (LED_PEAK_PCT) breathing that only starts once they finish

/* 100-entry breathing envelope: raised-cosine, gamma 2.2 (perceptually smooth). */
static const uint8_t LED_BREATH_LUT[100] = {
      0,   0,   0,   0,   0,   0,   0,   0,   0,   0,
      1,   1,   1,   2,   2,   3,   4,   5,   6,   8,
     10,  12,  14,  16,  19,  22,  25,  28,  32,  35,
     39,  43,  47,  52,  56,  60,  64,  69,  73,  76,
     80,  84,  87,  90,  92,  95,  97,  98,  99, 100,
    100, 100,  99,  98,  97,  95,  92,  90,  87,  84,
     80,  76,  73,  69,  64,  60,  56,  52,  47,  43,
     39,  35,  32,  28,  25,  22,  19,  16,  14,  12,
     10,   8,   6,   5,   4,   3,   2,   2,   1,   1,
      1,   0,   0,   0,   0,   0,   0,   0,   0,   0,
};
#define LED_ENV_LEN ((uint8_t)(sizeof(LED_BREATH_LUT) / sizeof(LED_BREATH_LUT[0])))

/* Map a 0..100 "brightness" value to the TIM8_CH1 compare register (0..LED_PWM_ARR+1). */
static inline void led_set_pct(uint8_t pct)
{
    LL_TIM_OC_SetCompareCH1(TIM8, (uint32_t)pct * (LED_PWM_ARR + 1u) / 100u);
}

void led_update(void);
void logo_timeout_check(void);

// Captured once at boot from RCC_CSR: why the MCU last reset. Shown on the splash so an
// unexpected reset (e.g. brown-out as the FC/camera rails settle) is visible rather than
// looking like a firmware glitch. The firmware itself never triggers a reset (no software
// reset, no IWDG/WWDG), so anything other than PIN/BOR here would be a real surprise.
const char *g_reset_cause = "?";
static void capture_reset_cause(void)
{
    if      (LL_RCC_IsActiveFlag_LPWRRST()) g_reset_cause = "LPWR";
    else if (LL_RCC_IsActiveFlag_WWDGRST()) g_reset_cause = "WWDG";
    else if (LL_RCC_IsActiveFlag_IWDGRST()) g_reset_cause = "IWDG";
    else if (LL_RCC_IsActiveFlag_SFTRST())  g_reset_cause = "SFT";
    else if (LL_RCC_IsActiveFlag_OBLRST())  g_reset_cause = "OBL";
    else if (LL_RCC_IsActiveFlag_BORRST())  g_reset_cause = "BOR/POR";
    else if (LL_RCC_IsActiveFlag_PINRST())  g_reset_cause = "PIN";
    LL_RCC_ClearResetFlags(); // so the NEXT boot reports its own cause
}

void debug_print_loop(void)
{
    static uint32_t last_tick = 0;

    if ((HAL_GetTick() - last_tick) >= DEBUG_LOOP_INTERVAL) {
        last_tick = HAL_GetTick();
        // Loop debug printf here
    }
}


int main (void)
{
    HAL_Init();
    capture_reset_cause();
    SystemClock_Config();
    gpio_init();
    TIM8_Init(); // status-LED breathing: TIM8_CH1 hardware PWM on PC6
    dma_init();
    adc_init();

    video_overlay_init();
#if defined(HIGH_RAM)
    video_graphics_init();
    video_draw_text_system_font(FONT_SYSTEM_WIDTH * 2, VIDEO_HEIGHT - FONT_SYSTEM_HEIGHT, "WAITING MSP...");
    video_graphics_draw_complete();
#endif
    msp_displayport_init();

#if defined(BUILD_VARIANT_VTX)
    if(rtc6705_init()) {
        printf("rtc6705 detected\r\n");
        rtc6705_set_frequency(5880); // TODO: remove after implementing configuration saving to flash

        rf_pa_init();
        rf_pa_set_power_level(RF_PA_PWR_20mW);
    }
#endif

    while (1)
    {
        msp_loop_process();
        led_update();
        debug_print_loop();
#ifdef OSD_AUTO_SYNC_CAL
        video_sync_cal_step();
#endif
        logo_timeout_check();
#ifdef OSD_TUNING_MODE
        video_tuning_step();
#endif
#ifdef OSD_BOOT_ANIMATION
        video_boot_animation_update();
#endif

#if 0 // TODO: remove later
// For test only - 3D cube animation
#if defined(HIGH_RAM)
        if (new_field == false) {
            video_draw_3d_cube_animation();
        }
#endif
#endif

    }
}

/* Drive the status LED. Called every main-loop iteration; a small state machine that first
 * emits the boot confirmation blinks, then tracks external video lock via video_gen_enabled
 * and either breathes (locked) or stays dark (no external signal). */
void led_update(void)
{
    enum { LED_BOOT, LED_OFF, LED_BREATHING };
    static uint8_t state = LED_BOOT;
    static uint32_t last_tick = 0;
    static uint8_t idx = 0;     // breathing LUT index, or boot-blink half-cycle counter

    const uint32_t now = HAL_GetTick();

    switch (state) {
    case LED_BOOT:
        if ((now - last_tick) >= LED_BLINK_MS) {
            last_tick = now;
            // Full-bright on/off so the blinks are unmistakable and distinct from the soft
            // breathing -- never blends with it, because breathing only starts (LED_BREATHING)
            // after this phase completes.
            led_set_pct(((idx & 1u) == 0u) ? LED_BLINK_PCT : 0u);  // even half-cycle = LED on
            if (++idx >= (LED_BOOT_BLINKS * 2u)) {
                idx = 0;
                state = LED_OFF;            // confirmation done; go dark until a signal appears
            }
        }
        break;

    case LED_OFF:
        if (video_gen_enabled == false) {   // external camera signal locked
            idx = 0;
            last_tick = now;
            state = LED_BREATHING;
        } else {
            led_set_pct(0u);                // no external signal: stay dark
        }
        break;

    case LED_BREATHING:
        if (video_gen_enabled == true) {    // external signal lost -> generator took over
            led_set_pct(0u);
            state = LED_OFF;
        } else if ((now - last_tick) >= LED_ENV_STEP_MS) {
            last_tick = now;
            if (++idx >= LED_ENV_LEN) {
                idx = 0;
            }
            led_set_pct((uint8_t)((uint32_t)LED_BREATH_LUT[idx] * LED_PEAK_PCT / 100u));
        }
        break;
    }
}

void logo_timeout_check(void)
{
#ifdef OSD_TUNING_MODE
    // Tuning mode: hold the splash/logo on screen indefinitely so the threshold-sweep
    // readout (video_draw_tuning_splash) stays visible for interactive debugging.
    return;
#else
    static uint32_t boot_time = 0;
    static bool timeout_checked = false;
    extern bool show_logo;

    // Initialize boot time on first call
    if (boot_time == 0) {
        boot_time = HAL_GetTick();
    }
#ifdef OSD_AUTO_SYNC_CAL
    // Quiet seek: nothing is shown until the calibration releases the splash; the splash then
    // runs for LOGO_TIMEOUT_MS counted from that moment.
    extern bool g_splash_pending;
    extern uint32_t g_splash_release_tick;
    if (g_splash_pending) {
        return;
    }
    boot_time = g_splash_release_tick;
#endif

    // Check if LOGO_TIMEOUT_MS has elapsed, clear logo and version string if so
    if (!timeout_checked && (HAL_GetTick() - boot_time) >= LOGO_TIMEOUT_MS) {
        show_logo = false;
        // Clear the canvas to remove version string
        canvas_char_clean();
        canvas_char_draw_complete();
        timeout_checked = true;
    }
#endif
}
