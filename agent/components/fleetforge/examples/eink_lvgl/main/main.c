/* Fleetforge + LVGL v9 1-bpp e-paper example (ESP-IDF).
 * Renders "Hello (<PROJECT_VER>)" into a monochrome I1 buffer and flushes it to an 800x480
 * e-paper panel once connected to Fleetforge. */
#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "esp_timer.h"
#include "ff_identity.h"
#include "ff_log.h"
#include "ff_safe_window.h"
#include "ff_selftest.h"
#include "ff_sleep.h"
#include "ff_telemetry.h"
#include "fleetforge_start.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lvgl.h"

#ifndef MESSAGE
#define MESSAGE "Hello"
#endif

#define EPD_WIDTH   800
#define EPD_HEIGHT  480
#define EPD_FB_SIZE (EPD_WIDTH * EPD_HEIGHT / 8)

/* Provided by your board's panel driver (e.g. epd_397.c):
 *   1. Drive RST/CS/DC/MOSI/SCLK low before toggling the PMIC LDO rail (prevents ESD back-powering).
 *   2. Hold RST low for >= pdMS_TO_TICKS(20) (at 100 Hz FreeRTOS, pdMS_TO_TICKS(2) == 0!). */
extern esp_err_t epd_show(const uint8_t *fb_1bpp, const char **failed_stage);

/* LVGL v9 LV_COLOR_FORMAT_I1 prepends an 8-byte palette (two 32-bit ARGB8888 entries)
 * ahead of the 48,000-byte 1-bpp bitmap. */
static uint8_t s_lvgl_buf[EPD_FB_SIZE + 8];
static esp_err_t s_flush_err;
static const char *s_flush_stage;
static volatile bool s_busy;

static bool safe_to_reboot(void *ctx) { (void)ctx; return !s_busy; }

static ff_selftest_result_t on_selftest(void *ctx, char *reason, size_t len)
{
    (void)ctx, (void)reason, (void)len;
    return FF_SELFTEST_PENDING;
}

static void epd_flush_cb(lv_display_t *disp, const lv_area_t *area, uint8_t *px_map)
{
    (void)area;
    s_flush_err = epd_show(px_map + 8, &s_flush_stage);
    lv_display_flush_ready(disp);
}

static esp_err_t render_and_show(const char **failed_stage)
{
    lv_init();
    lv_display_t *disp = lv_display_create(EPD_WIDTH, EPD_HEIGHT);
    lv_display_set_color_format(disp, LV_COLOR_FORMAT_I1);
    lv_display_set_buffers(disp, s_lvgl_buf, NULL, sizeof(s_lvgl_buf), LV_DISPLAY_RENDER_MODE_FULL);
    lv_display_set_flush_cb(disp, epd_flush_cb);

    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_white(), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);

    lv_obj_t *label = lv_label_create(scr);
    lv_label_set_text_fmt(label, "%s (%s)", MESSAGE, ff_identity_fw_version());
    lv_obj_set_style_text_color(label, lv_color_black(), 0);
    lv_obj_set_style_text_font(label, &lv_font_montserrat_48, 0);
    lv_obj_center(label);

    s_flush_err = ESP_OK;
    s_flush_stage = "lvgl";
    lv_refr_now(disp);
    *failed_stage = s_flush_stage;
    return s_flush_err;
}

void app_main(void)
{
    fleetforge_start();
    ff_set_selftest(on_selftest, NULL);
    ff_set_safe_window_cb(safe_to_reboot, NULL);
    ff_set_power_class(FF_POWER_ALWAYS_ON, 0);

    while (!ff_is_connected()) {
        vTaskDelay(pdMS_TO_TICKS(200));
    }
    ff_selftest_report(true, NULL);

    s_busy = true;
    int64_t t0 = esp_timer_get_time();
    const char *stage = "unknown";
    esp_err_t err = render_and_show(&stage);
    s_busy = false;

    if (err == ESP_OK) {
        double ms = (double)(esp_timer_get_time() - t0) / 1000.0;
        ff_telemetry("epd_refresh_ms", ms);
        ff_logf('I', "showing \"%s (%s)\" (%.0f ms)", MESSAGE, ff_identity_fw_version(), ms);
    } else {
        ff_logf('E', "display failed at %s: %s", stage, esp_err_to_name(err));
    }

    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(60000));
    }
}
