/*
 * ff_safe_window — the registration and the freeze of the firmware's safe-window rule
 * (R5-fw-5). See include/ff_safe_window.h for the maker API and ff_safe_window_internal.h for
 * the hooks ff_mqtt.c and ff_ota.c call: every agent reboot (an apply, a rollback, the
 * `rollback` and `reboot` commands) waits until ff_safe_window_is_safe() says true.
 *
 * Three properties, each one a way to get this silently wrong:
 *
 * 1. THE RULE IS CALLED OUTSIDE THE LOCK, from task context only. ff_safe_window_is_safe()
 *    copies the pointer under the spinlock and calls it after leaving: the maker's code may
 *    take its own locks or log, and nothing of that may run inside a portMUX critical section.
 *    The esp_timer path (the confirm deadline) asks only ff_safe_window_registered(), which
 *    never calls the rule: a slow rule there would stall every timer.
 * 2. NOTHING HERE LOGS BUT A REFUSAL AND THE FREEZE. The two hooks run every 250 ms while a
 *    reboot waits, and the registered check runs in the esp_timer task.
 * 3. FROZEN AT SESSION START (ff_safe_window_freeze, from ff_mqtt_run before the first
 *    connect), the self-test's precedent (R5-fw-1): what the session announces (`safe_window`,
 *    R5-spec-1 D9) and what it waits for are the same rule. A later registration is a
 *    deterministic refusal, not a race with the session.
 */

#include "ff_safe_window_internal.h"

#include <stdbool.h>
#include <stddef.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ff-safe";

/* Guarded by s_lock. Set once, never cleared. */
static ff_safe_window_fn s_fn;
static void *s_ctx;
static bool s_frozen;
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;

esp_err_t ff_set_safe_window_cb(ff_safe_window_fn fn, void *ctx)
{
    if (fn == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    bool frozen;
    bool taken;
    portENTER_CRITICAL(&s_lock);
    frozen = s_frozen;
    taken = s_fn != NULL;
    if (!frozen && !taken) {
        s_fn = fn;
        s_ctx = ctx;
    }
    portEXIT_CRITICAL(&s_lock);

    if (frozen) {
        ESP_LOGE(TAG, "safe-window rule refused: the fleetforge session has started; register "
                      "right after fleetforge_start()/begin()");
        return ESP_ERR_INVALID_STATE;
    }
    if (taken) {
        ESP_LOGE(TAG, "safe-window rule refused: already registered; one rule per image");
        return ESP_ERR_INVALID_STATE;
    }
    return ESP_OK;
}

bool ff_safe_window_freeze(void)
{
    static bool s_logged;
    portENTER_CRITICAL(&s_lock);
    s_frozen = true;
    const bool registered = s_fn != NULL;
    const bool log_now = !s_logged;
    s_logged = true;
    portEXIT_CRITICAL(&s_lock);

    if (log_now) {
        if (registered) {
            ESP_LOGI(TAG, "a safe-window rule is registered: every reboot this agent makes "
                          "waits for it");
        } else {
            ESP_LOGI(TAG, "no safe-window rule registered: this agent reboots as soon as it "
                          "must");
        }
    }
    return registered;
}

bool ff_safe_window_registered(void)
{
    portENTER_CRITICAL(&s_lock);
    const bool registered = s_fn != NULL;
    portEXIT_CRITICAL(&s_lock);
    return registered;
}

bool ff_safe_window_is_safe(void)
{
    portENTER_CRITICAL(&s_lock);
    const ff_safe_window_fn fn = s_fn;
    void *const ctx = s_ctx;
    portEXIT_CRITICAL(&s_lock);
    if (fn == NULL) {
        return true; /* no rule: every moment is safe */
    }
    /* Property 1: the one call of the rule, outside the lock. */
    return fn(ctx);
}
