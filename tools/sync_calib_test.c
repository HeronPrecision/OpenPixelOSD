/* Host test for src/sync_calib.h.   cc -Wall -o /tmp/sync_calib_test tools/sync_calib_test.c && /tmp/sync_calib_test
 *
 * Sweep tables are line-interval counts per threshold:
 *   RC4-A  : measured. Real capture (doc/camera-waveforms, RC4-A), lines tiled to 400 and run
 *            through a model of the firmware's comparator + 59..65 us line window. 350 mV gave 0 lines.
 *   FC1-ref: SYNTHETIC. Built from the FC1 levels in commit 78da120 (tip ~216 mV, blanking ~480 mV):
 *            usable between the tip and the burst floor, 350 mV known good.
 *   NOISY  : SYNTHETIC. Chatter everywhere but a narrow 3-step window.
 *   NOLOCK : SYNTHETIC. Nothing locks anywhere (no camera / wrong level) -> must report no pick.
 * Replace the synthetic tables with measured ones as more cameras are captured.
 */
#include <stdio.h>
#include "../src/sync_calib.h"

#define T(th, g, o) { th, g, o }

static const sync_cal_point_t rc4a[] = {
    T(100,399,604), T(110,399,0), T(120,399,0), T(130,399,1200), T(140,399,392), T(150,399,392),
    T(160,399,596), T(170,399,392), T(180,399,392), T(190,399,392), T(200,292,907), T(210,203,996),
    T(220,203,792), T(230,203,792), T(240,399,400), T(250,399,400), T(260,399,400), T(270,399,400),
    T(280,0,1199), T(290,0,1199), T(300,196,807), T(310,196,807), T(320,0,1403), T(330,0,1607),
    T(340,0,1803), T(350,0,1999), T(360,0,1599), T(370,0,1999), T(380,0,2987), T(390,0,3591),
    T(400,0,3003), T(410,0,2791),
};
static const sync_cal_point_t fc1ref[] = {
    T(100,0,4000), T(150,0,3000), T(200,0,800), T(230,380,40), T(250,399,0), T(270,399,0),
    T(290,399,0), T(310,399,0), T(330,399,0), T(350,399,0), T(370,399,0), T(390,399,5),
    T(410,390,30), T(430,380,80), T(450,120,600), T(470,0,2000),
};
static const sync_cal_point_t noisy[] = {
    T(100,50,900), T(150,90,800), T(200,150,700), T(250,399,200), T(260,399,180), T(270,399,200),
    T(280,200,600), T(330,100,900), T(380,60,1200),
};
static const sync_cal_point_t nolock[] = {
    T(100,0,50), T(200,3,60), T(300,0,40), T(400,1,80), T(500,0,30),
};

static int fails;

static void check(const char *name, const sync_cal_point_t *p, int n, int want_ok, int lo, int hi)
{
    uint16_t mv = 0, w = 0;
    int ok = sync_cal_pick(p, n, &mv, &w);
    int pass = (ok == want_ok) && (!ok || (mv >= lo && mv <= hi));
    printf("%-8s ok=%d pick=%3u mV width=%3u mV   expect %s%s   %s\n", name, ok, mv, w,
           want_ok ? "pick in " : "no pick", want_ok ? "" : "", pass ? "PASS" : "FAIL");
    if (want_ok) {
        printf("         (accepted range %d..%d mV)\n", lo, hi);
    }
    if (!pass) {
        fails++;
    }
}

int main(void)
{
    /* RC4-A: widest usable run is 100..190; must NOT pick the 350 mV default, which recognised 0 lines. */
    check("RC4-A", rc4a, (int)(sizeof rc4a / sizeof rc4a[0]), 1, 100, 200);
    check("FC1-ref", fc1ref, (int)(sizeof fc1ref / sizeof fc1ref[0]), 1, 300, 390);
    check("NOISY", noisy, (int)(sizeof noisy / sizeof noisy[0]), 1, 250, 270);
    check("NOLOCK", nolock, (int)(sizeof nolock / sizeof nolock[0]), 0, 0, 0);
    printf("%s\n", fails ? "SOME TESTS FAILED" : "all tests passed");
    return fails ? 1 : 0;
}
