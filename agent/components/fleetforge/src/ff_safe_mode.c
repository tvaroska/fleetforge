/*
 * ff_safe_mode — rapid-crash safe mode (R5-fw-6, R5-spec-1 D13). See include/ff_safe_mode.h
 * for the contract and ff_safe_mode_internal.h for the announce's read.
 *
 * Four properties, each one a way to get this silently wrong:
 *
 * 1. THE STATE LIVES IN ONE RTC_NOINIT RECORD, NEVER IN NVS. RTC memory survives panic,
 *    watchdog and software resets and is lost at power-on, which IS the spec's "a power cycle
 *    leaves". No flash wear, available before NVS and netif, and the bootloop test image's
 *    "none of our code writes flash" holds. A bad magic or crc (power-on garbage, or a crash
 *    in the middle of a write) is a fresh record: the fail-safe direction, no safe mode.
 * 2. ONLY A CONFIRMED IMAGE COUNTS, AND ONLY ITS OWN CRASHES. The record names the slot of
 *    the boot that wrote it; another slot (a successful update, a rollback, the bootloader
 *    putting the old image back) starts afresh. An image still in PENDING_VERIFY never
 *    counts: its crashes are the bootloader's business. A USB-flashed image (no otadata
 *    entry) counts as confirmed.
 * 3. THE LIFETIME BREADCRUMB IS A FREERTOS TASK, NEVER AN esp_timer. "Three resets within
 *    60 s" is T3 - T1 <= 60 s, which is the sum of the lifetimes of the two boots that BEGAN
 *    with a counted reset. A boot that began with one measures its own lifetime with a
 *    one-second task (an upper bound: over-estimating makes safe mode harder to enter). A
 *    healthy board never runs it. Not an esp_timer: QEMU does not reset that peripheral with
 *    the CPU, and an alarm pending across a reset panics the next boot in
 *    esp_timer_impl_init (docs/runbooks/agent-qemu.md), which would turn every counted crash
 *    in the emulator into a loop and leave the rule unprovable.
 * 4. A BROWNOUT IS NEITHER A CRASH NOR AN EXIT. It clears the count and keeps the latch. Every
 *    other reset (software, the reset pin, USB, JTAG, a deep-sleep wake) leaves: in safe mode
 *    only the agent runs, and every esp_restart() it makes is a reboot, an apply or a
 *    rollback, all of which the spec lists as exits. So no verb path knows about safe mode.
 *
 * Written at boot, before any agent task exists, and afterwards only by the breadcrumb task:
 * no lock.
 */

#include "ff_safe_mode_internal.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "esp_attr.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_rom_crc.h"
#include "esp_system.h"
#include "ff_identity_internal.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ff-safemode";

/* R5-spec-1 D13: three counted resets within 60 s. */
#define FF_SAFE_MODE_CRASHES 3u
#define FF_SAFE_MODE_WINDOW_S 60u
/* The lifetimes between the first and the last counted reset of the window. */
#define FF_SAFE_MODE_LIVES (FF_SAFE_MODE_CRASHES - 1u)
_Static_assert(FF_SAFE_MODE_CRASHES >= 2u && FF_SAFE_MODE_CRASHES <= 255u,
               "the crash count is a saturating uint8_t, and one crash is not a pattern");
_Static_assert(FF_SAFE_MODE_WINDOW_S >= 10u && FF_SAFE_MODE_WINDOW_S * FF_SAFE_MODE_LIVES <= 0xffffu,
               "the window is whole seconds and the lifetimes sum in 16 bits");

#define FF_SAFE_MODE_MAGIC 0x46465336u /* 'FFS6' */

#define ALIVE_TASK_STACK 2048
#define ALIVE_TASK_PRIO 5

typedef struct {
    uint32_t magic;                       /* FF_SAFE_MODE_MAGIC */
    uint32_t slot;                        /* running partition address of the boot that wrote it */
    uint8_t crashes;                      /* consecutive counted resets on `slot`, saturating */
    uint8_t latched;                      /* 1 while in safe mode */
    uint16_t alive_s;                     /* upper bound of the CURRENT boot's lifetime, capped */
    uint16_t life_s[FF_SAFE_MODE_LIVES];  /* lifetimes of the last crashed boots, oldest first */
    uint32_t crc;                         /* esp_rom_crc32_le over every byte before it */
} ff_safe_mode_rtc_t;

static RTC_NOINIT_ATTR ff_safe_mode_rtc_t s_rtc;

static bool s_checked;
static bool s_latched;

static uint32_t record_crc(void)
{
    return esp_rom_crc32_le(0, (const uint8_t *)&s_rtc, offsetof(ff_safe_mode_rtc_t, crc));
}

/* After EVERY write of s_rtc: a record whose crc does not match is read as power-on garbage. */
static void seal(void)
{
    s_rtc.magic = FF_SAFE_MODE_MAGIC;
    s_rtc.crc = record_crc();
}

static bool record_is_valid(void)
{
    return s_rtc.magic == FF_SAFE_MODE_MAGIC && s_rtc.crc == record_crc();
}

static void fresh_record(void)
{
    memset(&s_rtc, 0, sizeof s_rtc);
}

/* Exactly the spec's four. Not brownout (property 4), not cpu_lockup (not in the spec's list). */
static bool is_counted(esp_reset_reason_t reason)
{
    switch (reason) {
    case ESP_RST_PANIC:
    case ESP_RST_INT_WDT:
    case ESP_RST_TASK_WDT:
    case ESP_RST_WDT:
        return true;
    default:
        return false;
    }
}

/* Property 2. NEW, PENDING_VERIFY, INVALID and ABORTED are not confirmed. */
static bool image_is_confirmed(const esp_partition_t *running)
{
    if (running == NULL) {
        return false;
    }
    esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
    if (esp_ota_get_state_partition(running, &state) != ESP_OK) {
        return true; /* no otadata entry: flashed over USB, confirmed by definition */
    }
    return state == ESP_OTA_IMG_VALID || state == ESP_OTA_IMG_UNDEFINED;
}

/* Property 3: this boot's lifetime, in whole seconds, rounded up, until the window has passed.
 * No log, no NVS, no lock, no esp_timer. */
static void alive_task(void *arg)
{
    (void)arg;
    for (uint32_t elapsed = 1; elapsed < FF_SAFE_MODE_WINDOW_S; elapsed++) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        s_rtc.alive_s = (uint16_t)(elapsed + 1u);
        seal();
    }
    vTaskDelete(NULL);
}

static void start_breadcrumb(void)
{
    s_rtc.alive_s = 1;
    seal();
    if (xTaskCreate(alive_task, "ff_alive", ALIVE_TASK_STACK, NULL, ALIVE_TASK_PRIO, NULL) != pdPASS) {
        /* Unknown counts as the whole window: never toward entering. */
        s_rtc.alive_s = FF_SAFE_MODE_WINDOW_S;
        seal();
    }
}

bool ff_safe_mode_check(void)
{
    if (s_checked) {
        return s_latched;
    }
    s_checked = true;

    const esp_reset_reason_t reason = esp_reset_reason();
    const char *reason_name = ff_identity_reset_reason_name(reason);
    const esp_partition_t *running = esp_ota_get_running_partition();
    const uint32_t slot = running != NULL ? running->address : 0;

    const bool valid = record_is_valid();
    const bool was_latched = valid && s_rtc.latched != 0;
    const bool slot_changed = valid && s_rtc.slot != slot;
    bool counted = false;
    bool entered = false;

    if (!valid || reason == ESP_RST_POWERON || slot_changed || !image_is_confirmed(running)) {
        fresh_record();
    } else if (is_counted(reason)) {
        counted = true;
        for (unsigned i = 0; i + 1u < FF_SAFE_MODE_LIVES; i++) {
            s_rtc.life_s[i] = s_rtc.life_s[i + 1u];
        }
        s_rtc.life_s[FF_SAFE_MODE_LIVES - 1u] = s_rtc.alive_s;
        if (s_rtc.crashes < UINT8_MAX) {
            s_rtc.crashes++;
        }
        uint32_t window_s = 0;
        for (unsigned i = 0; i < FF_SAFE_MODE_LIVES; i++) {
            window_s += s_rtc.life_s[i];
        }
        if (s_rtc.latched == 0 && s_rtc.crashes >= FF_SAFE_MODE_CRASHES &&
            window_s <= FF_SAFE_MODE_WINDOW_S) {
            s_rtc.latched = 1;
            entered = true;
        }
    } else if (reason == ESP_RST_BROWNOUT) {
        /* Property 4: the count restarts, the latch stays. */
        s_rtc.crashes = 0;
        memset(s_rtc.life_s, 0, sizeof s_rtc.life_s);
    } else {
        fresh_record();
    }

    s_latched = s_rtc.latched != 0;
    s_rtc.slot = slot;
    if (counted && !s_latched) {
        start_breadcrumb();
    } else {
        s_rtc.alive_s = FF_SAFE_MODE_WINDOW_S;
        seal();
    }

    if (entered) {
        ESP_LOGW(TAG, "SAFE MODE: three crash resets within %u s on this confirmed image "
                      "(last: %s). Only the fleetforge agent runs; the firmware's own code is "
                      "held until an update, a reboot command or a power cycle",
                 FF_SAFE_MODE_WINDOW_S, reason_name);
    } else if (s_latched) {
        ESP_LOGW(TAG, "still in SAFE MODE (last reset: %s): only the fleetforge agent runs",
                 reason_name);
    } else if (counted) {
        const unsigned last_life = s_rtc.life_s[FF_SAFE_MODE_LIVES - 1u];
        if (last_life < FF_SAFE_MODE_WINDOW_S) {
            ESP_LOGW(TAG, "the last boot ended in %s after <= %u s: crash reset %u of %u on "
                          "this image (three within %u s hold it in safe mode)",
                     reason_name, last_life, (unsigned)s_rtc.crashes, FF_SAFE_MODE_CRASHES,
                     FF_SAFE_MODE_WINDOW_S);
        } else {
            ESP_LOGW(TAG, "the last boot ended in %s after %u s or more (or unmeasured): crash "
                          "reset %u of %u on this image (three within %u s hold it in safe "
                          "mode)",
                     reason_name, FF_SAFE_MODE_WINDOW_S, (unsigned)s_rtc.crashes,
                     FF_SAFE_MODE_CRASHES, FF_SAFE_MODE_WINDOW_S);
        }
    } else if (was_latched) {
        if (slot_changed) {
            ESP_LOGI(TAG, "safe mode left: another image is running");
        } else if (reason == ESP_RST_SW) {
            ESP_LOGI(TAG, "safe mode left: a software restart (a reboot command, an update or "
                          "a rollback)");
        } else {
            ESP_LOGI(TAG, "safe mode left: reset by %s", reason_name);
        }
    }
    return s_latched;
}

bool ff_safe_mode_active(void)
{
    return s_checked && s_latched;
}
