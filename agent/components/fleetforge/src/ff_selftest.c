/*
 * ff_selftest — the registration, the freeze and the runner of the firmware's self-test
 * (R5-fw-1). See include/ff_selftest.h for the maker API and ff_selftest_internal.h for the
 * hooks ff_mqtt.c calls. Until R5-fw-2 nothing calls ff_selftest_run(): the registration is
 * recorded and frozen, the runner is built and unwired.
 *
 * Four properties, each one a way to get this silently wrong:
 *
 * 1. OWN TASK. The registered function is called only from selftest_task, never from the
 *    caller of ff_selftest_run(). Not from the esp-mqtt task, where a slow test would stall
 *    the session and the PUBACK of the message being handled. Not from esp_timer, where it
 *    would stall every timer, the confirm timer included.
 * 2. FIRST ANSWER WINS, and anything but PASS/PENDING is FAIL. A report that arrives while
 *    the function is still running beats its return value; a later report or return is
 *    ignored.
 * 3. ff_selftest_report() NEVER LOGS, never allocates and never blocks: a spinlock around a
 *    few stores, and one xSemaphoreGive on a static semaphore. A firmware calls it on every
 *    loop, and R4-fw-3 forwards log lines: a report that logged could recurse through that
 *    forwarder.
 * 4. FROZEN AT SESSION START (ff_selftest_freeze, from ff_mqtt_run before the first connect),
 *    so what the session announces (R5-fw-2) and what it runs are the same thing. A later
 *    registration is a deterministic refusal, not a race with the session.
 *
 * Why a static binary semaphore wakes the runner and not a task notification: a report may
 * race the task's exit, and xTaskNotifyGive on a deleted task's handle is a use-after-free.
 * A static semaphore cannot be freed.
 */

#include "ff_selftest_internal.h"

#include <stdbool.h>
#include <stddef.h>
#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "ff-selftest";

/* The maker's code runs on this stack: a printf with floats, an I2C read. The header
 * promises at least 4 KB. */
#define FF_SELFTEST_STACK 6144
/* Below ff_hb/ff_tel (4) and esp-mqtt (5): a CPU-bound synchronous test must not starve the
 * session that keeps the broker connection. */
#define FF_SELFTEST_PRIO 1
/* The size of ff_txn's detail[64]; R5-fw-2 shortens the reason to fit its grammar. */
#define FF_SELFTEST_REASON_BUF 64
/* How far a report's reason is scanned: a pointer to an unterminated string cannot run
 * away. */
#define FF_SELFTEST_REPORT_SCAN 256

typedef enum {
    ST_IDLE,     /* not asked (yet) */
    ST_STARTING, /* ff_selftest_run() is creating the task */
    ST_RUNNING,  /* the registered function is executing */
    ST_PENDING,  /* it returned FF_SELFTEST_PENDING: waiting for ff_selftest_report() */
    ST_DONE,     /* decided: s_passed / s_reason hold the first answer */
} st_state_t;

/* Guarded by s_lock. */
static ff_selftest_fn s_fn;
static void *s_ctx;
static bool s_frozen;
static st_state_t s_state;
static bool s_passed;
static char s_reason[FF_SELFTEST_REASON_BUF];
static ff_selftest_done_fn s_done;
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;

/* Wakes the runner when a pending self-test is answered. Created once, never deleted. */
static StaticSemaphore_t s_sem_buf;
static SemaphoreHandle_t s_sem;

/* Copy the printable ASCII of `src` (0x20..0x7e, except ';') into `dst`, dropping every
 * other byte. Stops at a NUL, after `src_max` bytes scanned, or when `dst` is full. Always
 * NUL-terminates; a NULL `src` gives "". Never called under s_lock. */
static void keep_reason(char *dst, size_t dst_len, const char *src, size_t src_max)
{
    size_t n = 0;
    if (src != NULL) {
        for (size_t i = 0; i < src_max && src[i] != '\0' && n + 1 < dst_len; i++) {
            const unsigned char c = (unsigned char)src[i];
            if (c >= 0x20 && c <= 0x7e && c != ';') {
                dst[n++] = (char)c;
            }
        }
    }
    dst[n] = '\0';
}

esp_err_t ff_set_selftest(ff_selftest_fn fn, void *ctx)
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
        ESP_LOGE(TAG, "self-test refused: the fleetforge session has started; register right "
                      "after fleetforge_start()/begin()");
        return ESP_ERR_INVALID_STATE;
    }
    if (taken) {
        ESP_LOGE(TAG, "self-test refused: already registered; one self-test per image");
        return ESP_ERR_INVALID_STATE;
    }
    return ESP_OK;
}

bool ff_selftest_freeze(void)
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
            ESP_LOGI(TAG, "a custom self-test is registered for this image");
        } else {
            ESP_LOGI(TAG, "no custom self-test registered: an OTA'd image confirms once its "
                          "announce is acknowledged");
        }
    }
    return registered;
}

static void selftest_task(void *arg)
{
    (void)arg;

    portENTER_CRITICAL(&s_lock);
    s_state = ST_RUNNING;
    const ff_selftest_fn fn = s_fn;
    void *const ctx = s_ctx;
    const ff_selftest_done_fn done = s_done;
    portEXIT_CRITICAL(&s_lock);

    /* The one place the registered function is called (property 1). */
    char reason[FF_SELFTEST_REASON_BUF] = {0};
    const ff_selftest_result_t r = fn(ctx, reason, sizeof reason);
    reason[sizeof reason - 1] = '\0';
    char kept[FF_SELFTEST_REASON_BUF];
    keep_reason(kept, sizeof kept, reason, sizeof reason);

    portENTER_CRITICAL(&s_lock);
    if (s_state == ST_RUNNING) {
        if (r == FF_SELFTEST_PENDING) {
            s_state = ST_PENDING;
        } else {
            /* Anything but PASS fails, an out-of-range value included (property 2). */
            s_passed = r == FF_SELFTEST_PASS;
            if (s_passed) {
                s_reason[0] = '\0';
            } else {
                memcpy(s_reason, kept, sizeof s_reason);
            }
            s_state = ST_DONE;
        }
    }
    /* else ST_DONE: a report made while the function ran won; its return is ignored. */
    portEXIT_CRITICAL(&s_lock);

    /* No deadline here: the confirm deadline owns time, and a rollback reboot ends this
     * task. */
    for (;;) {
        portENTER_CRITICAL(&s_lock);
        const bool decided = s_state == ST_DONE;
        portEXIT_CRITICAL(&s_lock);
        if (decided) {
            break;
        }
        xSemaphoreTake(s_sem, portMAX_DELAY);
    }

    portENTER_CRITICAL(&s_lock);
    const bool passed = s_passed;
    memcpy(kept, s_reason, sizeof kept);
    portEXIT_CRITICAL(&s_lock);

    if (passed) {
        ESP_LOGI(TAG, "custom self-test passed");
    } else {
        /* Sanitized printable text (keep_reason): safe to print. */
        ESP_LOGW(TAG, "custom self-test failed: %s", kept);
    }
    if (done != NULL) {
        done(passed, kept);
    }
    vTaskDelete(NULL);
}

esp_err_t ff_selftest_run(ff_selftest_done_fn done)
{
    portENTER_CRITICAL(&s_lock);
    const bool startable = s_frozen && s_fn != NULL && s_state == ST_IDLE;
    if (startable) {
        s_state = ST_STARTING;
        s_done = done;
    }
    portEXIT_CRITICAL(&s_lock);
    if (!startable) {
        return ESP_ERR_INVALID_STATE;
    }

    /* Only the caller that moved IDLE -> STARTING gets here, so this needs no lock. */
    if (s_sem == NULL) {
        s_sem = xSemaphoreCreateBinaryStatic(&s_sem_buf);
    }
    /* Before the create, so the log never shows the result ahead of the start. */
    ESP_LOGI(TAG, "running the custom self-test");
    if (xTaskCreate(selftest_task, "ff_selftest", FF_SELFTEST_STACK, NULL, FF_SELFTEST_PRIO,
                    NULL) != pdPASS) {
        portENTER_CRITICAL(&s_lock);
        s_state = ST_IDLE;
        portEXIT_CRITICAL(&s_lock);
        ESP_LOGE(TAG, "cannot create the self-test task");
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

esp_err_t ff_selftest_report(bool passed, const char *reason)
{
    char kept[FF_SELFTEST_REASON_BUF];
    keep_reason(kept, sizeof kept, reason, FF_SELFTEST_REPORT_SCAN);

    portENTER_CRITICAL(&s_lock);
    if (s_state != ST_RUNNING && s_state != ST_PENDING) {
        portEXIT_CRITICAL(&s_lock);
        return ESP_ERR_INVALID_STATE;
    }
    const bool was_pending = s_state == ST_PENDING;
    s_state = ST_DONE;
    s_passed = passed;
    if (passed) {
        s_reason[0] = '\0';
    } else {
        memcpy(s_reason, kept, sizeof s_reason);
    }
    portEXIT_CRITICAL(&s_lock);

    if (was_pending) {
        xSemaphoreGive(s_sem);
    }
    return ESP_OK;
}
