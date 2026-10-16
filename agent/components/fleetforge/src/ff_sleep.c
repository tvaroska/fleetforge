/*
 * ff_sleep — the firmware's declared power class and the two sleep helpers (R5-fw-7). See
 * include/ff_sleep.h for the maker API and ff_sleep_internal.h for the hooks ff_mqtt.c and
 * ff_identity.c call.
 *
 * Four properties, each one a way to get this silently wrong:
 *
 * 1. FROZEN AT SESSION START (ff_sleep_freeze, from ff_mqtt_run before the first connect), the
 *    self-test's and the safe window's precedent (R5-fw-1, R5-fw-5): every announce of the
 *    session carries the same class, and a later declaration is a deterministic refusal, not
 *    a race with the session.
 * 2. THE HELPERS NEVER LOG, never touch NVS and never call ff_ota_txn_phase(): a firmware calls
 *    them on every loop pass, and ff_ota_txn_phase()'s staged branch loads (and may erase) the
 *    transaction record, which another task may be writing. The boot pointer and the running
 *    image's otadata state are read through esp_ota_ops (a flash read, never a write).
 * 3. THE SESSION IS ASKED FIRST. Until this boot's first announce is acknowledged
 *    ff_mqtt_sleep_blocker() answers "not checked in" without touching the client, which may
 *    not exist yet; only after that does it read the outbox.
 * 4. FAIL-SAFE FOR SLEEP: an otadata answer this file cannot interpret counts as unconfirmed.
 *    A board that stays awake costs battery; one that sleeps on an unconfirmed image loses
 *    the update.
 */

#include "ff_sleep_internal.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_log.h"
#include "esp_ota_ops.h"
#include "ff_mqtt_internal.h"
#include "ff_ota.h"
#include "ff_telemetry_internal.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ff-sleep";

/* The literal ff_cfg.h does not spell: FF_CFG_DEFAULT_POWER is the other one. */
#define POWER_SLEEPY "sleepy"

/* Guarded by s_lock. Set once, never cleared. */
static ff_power_class_t s_class;
static uint32_t s_wake_s;
static bool s_registered;
static bool s_frozen;
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;

/* One literal per rule of ff_is_ready_for_sleep(), in the order they are checked. Nothing
 * prints them (property 2); they name each rule for the reader and the tests. The session's
 * own rules are ff_mqtt.c's. */
static const char BLOCK_IN_FLIGHT[] = "an update is in progress";
static const char BLOCK_BOOT_ELSEWHERE[] = "a reset would boot another image";
static const char BLOCK_UNCONFIRMED[] = "the running image is not confirmed";
static const char BLOCK_TELEMETRY[] = "telemetry samples are not published yet";

esp_err_t ff_set_power_class(ff_power_class_t power_class, uint32_t wake_s)
{
    const char *invalid = NULL;
    if (power_class == FF_POWER_SLEEPY) {
        if (wake_s < 1 || wake_s > (uint32_t)INT32_MAX) {
            invalid = "sleepy needs a wake interval of 1 to 2147483647 s";
        }
    } else if (power_class == FF_POWER_ALWAYS_ON) {
        if (wake_s != 0) {
            invalid = "always_on takes a wake interval of 0";
        }
    } else {
        invalid = "unknown power class";
    }
    if (invalid != NULL) {
        ESP_LOGE(TAG, "power class refused: %s", invalid);
        return ESP_ERR_INVALID_ARG;
    }

    bool frozen;
    bool taken;
    portENTER_CRITICAL(&s_lock);
    frozen = s_frozen;
    taken = s_registered;
    if (!frozen && !taken) {
        s_class = power_class;
        s_wake_s = wake_s;
        s_registered = true;
    }
    portEXIT_CRITICAL(&s_lock);

    if (frozen) {
        ESP_LOGE(TAG, "power class refused: the fleetforge session has started; set it right "
                      "after fleetforge_start()/begin()");
        return ESP_ERR_INVALID_STATE;
    }
    if (taken) {
        ESP_LOGE(TAG, "power class refused: already set; one per image");
        return ESP_ERR_INVALID_STATE;
    }
    return ESP_OK;
}

void ff_sleep_freeze(const ff_cfg_t *cfg)
{
    static bool s_logged;
    portENTER_CRITICAL(&s_lock);
    s_frozen = true;
    const bool registered = s_registered;
    const ff_power_class_t power_class = s_class;
    const uint32_t wake_s = s_wake_s;
    const bool log_now = !s_logged;
    s_logged = true;
    portEXIT_CRITICAL(&s_lock);

    if (!log_now) {
        return;
    }
    if (registered) {
        ESP_LOGW(TAG, "the firmware declares power_class %s (wake %u s): announced instead of "
                      "ff_cfg's %s (wake_s %d)",
                 power_class == FF_POWER_SLEEPY ? POWER_SLEEPY : FF_CFG_DEFAULT_POWER,
                 (unsigned)wake_s, cfg->power, cfg->wake_s);
    } else {
        ESP_LOGI(TAG, "no power class declared by the firmware: power_class %s from ff_cfg",
                 cfg->power);
    }
}

bool ff_sleep_power_override(const char **power, int *wake_s)
{
    portENTER_CRITICAL(&s_lock);
    const bool active = s_frozen && s_registered;
    const ff_power_class_t power_class = s_class;
    const uint32_t declared_wake_s = s_wake_s;
    portEXIT_CRITICAL(&s_lock);

    if (!active) {
        return false;
    }
    const bool sleepy = power_class == FF_POWER_SLEEPY;
    *power = sleepy ? POWER_SLEEPY : FF_CFG_DEFAULT_POWER;
    *wake_s = sleepy ? (int)declared_wake_s : 0;
    return true;
}

bool ff_is_connected(void)
{
    return ff_mqtt_is_connected();
}

/* VALID, UNDEFINED (a serially flashed image) and "no otadata entry" (USB-flashed, or not an
 * OTA slot) are confirmed: the R5-fw-5/R5-fw-6 rule. NEW, PENDING_VERIFY, INVALID and ABORTED
 * are not, and neither is any other error (property 4). */
static bool running_image_confirmed(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    if (running == NULL) {
        return false;
    }
    esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
    const esp_err_t err = esp_ota_get_state_partition(running, &state);
    if (err == ESP_ERR_NOT_FOUND || err == ESP_ERR_NOT_SUPPORTED) {
        return true;
    }
    if (err != ESP_OK) {
        return false;
    }
    return state == ESP_OTA_IMG_VALID || state == ESP_OTA_IMG_UNDEFINED;
}

/* NULL when a deep sleep now loses nothing, else the first rule that forbids it. */
static const char *sleep_blocker(void)
{
    /* Property 3: checked in, rollback, command reboot, transaction, outbox. */
    const char *session = ff_mqtt_sleep_blocker();
    if (session != NULL) {
        return session;
    }
    if (ff_ota_in_flight()) {
        return BLOCK_IN_FLIGHT;
    }
    /* Staged (on_command waits for `apply`), a released apply, or a running image already
     * marked invalid: a reset now would boot the other slot (D14). */
    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *boot = esp_ota_get_boot_partition();
    if (running == NULL || boot == NULL || boot != running) {
        return BLOCK_BOOT_ELSEWHERE;
    }
    if (!running_image_confirmed()) {
        return BLOCK_UNCONFIRMED;
    }
    if (!ff_telemetry_drained()) {
        return BLOCK_TELEMETRY;
    }
    return NULL;
}

bool ff_is_ready_for_sleep(void)
{
    return sleep_blocker() == NULL;
}
