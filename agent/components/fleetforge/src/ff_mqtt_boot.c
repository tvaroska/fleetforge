/*
 * ff_mqtt_boot — OTA confirm/rollback timers, self-test hold, rollback gate, and boot classification.
 *
 * CRITICAL.md: "Device-side confirm timer / rollback path". Armed first in app_main before
 * NVS/network bring-up; never calls esp_mqtt_client_* from timer context.
 */

#define FF_MQTT_INTERNAL_IMPL
#include "ff_mqtt_internal.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "ff_identity_internal.h"
#include "ff_safe_window_internal.h"

static const char *TAG = "ff-mqtt";

#define ROLLBACK_REPORT_GRACE_MS 2000
#define ROLLBACK_REPORT_STACK 4096
#define ROLLBACK_GATE_STACK 4096
#define ROLLBACK_GATE_PRIO 6

static esp_timer_handle_t s_rollback_timer;
static esp_timer_handle_t s_confirm_timer;
static int64_t s_confirm_origin_us;
volatile uint32_t s_confirm_deadline_s = CONFIRM_TIMEOUT_S;
static bool s_gate_started;
static bool s_confirm_armed;

bool confirm_this_image(void)
{
    if (!pending_verify()) {
        return false;
    }
    esp_err_t err = esp_ota_mark_app_valid_cancel_rollback();
    if (err == ESP_OK) {
        ESP_LOGW(TAG, "this image was written by OTA and is now CONFIRMED: the broker "
                      "accepted us and the retained announce was acknowledged");
        return true;
    }
    ESP_LOGE(TAG, "cannot confirm this OTA image (%s) — the bootloader will roll back "
                  "on the next reset",
             esp_err_to_name(err));
    return false;
}

bool accept_unverified_image(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
    if (running == NULL || esp_ota_get_state_partition(running, &state) != ESP_OK ||
        state != ESP_OTA_IMG_NEW) {
        return false;
    }
    esp_err_t err = esp_ota_mark_app_valid_cancel_rollback();
    if (err == ESP_OK) {
        ESP_LOGW(TAG, "this image was written by OTA and booted without rollback armed; the "
                      "broker accepted us, so it is now marked valid and CONFIRMED");
        return true;
    }
    ESP_LOGE(TAG, "cannot mark this unverified OTA image valid (%s) — not reporting confirmed",
             esp_err_to_name(err));
    return false;
}

static void selftest_failed_head(char *out, size_t len, const char *reason)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    const size_t label_len =
        running != NULL ? strnlen(running->label, sizeof(running->label)) : strlen("ota_0");
    const size_t fixed = strlen(DETAIL_SELFTEST_FAILED) + strlen(DETAIL_RETURNED_TO) + label_len;
    const int budget = fixed < DETAIL_MAX ? (int)(DETAIL_MAX - fixed) : 0;
    if (reason == NULL || reason[0] == '\0') {
        reason = DETAIL_NO_REASON;
    }
    snprintf(out, len, DETAIL_SELFTEST_FAILED "%.*s", budget, reason);
}

static void compose_returned_detail(char *out, size_t len, const char *head, const char *label)
{
    const size_t tail_len = strlen(DETAIL_RETURNED_TO) + strlen(label);
    size_t head_len = strlen(head);
    if (head_len + tail_len > DETAIL_MAX) {
        const size_t prefix_len =
            strncmp(head, DETAIL_SELFTEST_FAILED, strlen(DETAIL_SELFTEST_FAILED)) == 0
                ? strlen(DETAIL_SELFTEST_FAILED)
                : head_len;
        const size_t room = tail_len < DETAIL_MAX ? DETAIL_MAX - tail_len : 0;
        head_len = room > prefix_len ? room : prefix_len;
    }
    snprintf(out, len, "%.*s" DETAIL_RETURNED_TO "%s", (int)head_len, head, label);
}

static void rollback_now_cb(void *arg)
{
    (void)arg;
    esp_ota_mark_app_invalid_rollback_and_reboot();
    ESP_LOGE(TAG, "esp_ota_mark_app_invalid_rollback_and_reboot returned — there is no "
                  "other bootable slot to go back to");
}

static void rollback_report_task(void *arg)
{
    (void)arg;
    if (s_ctx.connected) {
        (void)enqueue_status(s_txn.cmd_id, FF_STATUS_ROLLING_BACK, FF_STATUS_PCT_NONE, NULL);
    } else {
        ESP_LOGW(TAG, "no broker session: %s for %s is not reported", FF_STATUS_ROLLING_BACK,
                 s_txn.cmd_id);
    }
    vTaskDelete(NULL);
}

static StaticTask_t s_gate_tcb;
static StackType_t s_gate_stack[ROLLBACK_GATE_STACK];

static void rollback_gate_task(void *arg)
{
    (void)arg;
    const txn_kind_t kind = s_txn.kind;
    const bool owed = kind == TXN_CONFIRMING || kind == TXN_BOOTED_NEW;
    if (!ff_safe_window_is_safe()) {
        s_rollback_waiting = true;
        ESP_LOGW(TAG, "the rollback is decided and waits for a safe window — this image keeps "
                      "running until then; a reset rolls it back too");
        if (owed && s_ctx.connected) {
            (void)enqueue_status(s_txn.cmd_id, FF_STATUS_AWAITING_SAFE_WINDOW,
                                 FF_STATUS_PCT_NONE, FF_DETAIL_ROLLBACK_WAITS);
        }
        while (!ff_safe_window_is_safe()) {
            vTaskDelay(pdMS_TO_TICKS(FF_SAFE_WINDOW_POLL_MS));
        }
        ESP_LOGW(TAG, "a safe window: rolling back now");
    }
    s_rollback_waiting = false;
    if (owed && s_ctx.connected) {
        (void)enqueue_status(s_txn.cmd_id, FF_STATUS_ROLLING_BACK, FF_STATUS_PCT_NONE, NULL);
        vTaskDelay(pdMS_TO_TICKS(ROLLBACK_REPORT_GRACE_MS));
    } else if (owed) {
        ESP_LOGW(TAG, "no broker session: %s for %s is not reported", FF_STATUS_ROLLING_BACK,
                 s_txn.cmd_id);
    }
    while (!ff_safe_window_is_safe()) {
        vTaskDelay(pdMS_TO_TICKS(FF_SAFE_WINDOW_POLL_MS));
    }
    esp_ota_mark_app_invalid_rollback_and_reboot();
    ESP_LOGE(TAG, "esp_ota_mark_app_invalid_rollback_and_reboot returned — there is no other "
                  "bootable slot to go back to");
    vTaskDelete(NULL);
}

static void start_rollback_gate(void)
{
    bool first;
    portENTER_CRITICAL(&s_decide_lock);
    first = !s_gate_started;
    s_gate_started = true;
    portEXIT_CRITICAL(&s_decide_lock);
    if (!first) {
        return;
    }
    if (xTaskCreateStatic(rollback_gate_task, "ff_rbgate", ROLLBACK_GATE_STACK, NULL,
                          ROLLBACK_GATE_PRIO, s_gate_stack, &s_gate_tcb) == NULL) {
        ESP_LOGE(TAG, "cannot start the rollback gate — rolling back without waiting for a "
                      "safe window");
        esp_ota_mark_app_invalid_rollback_and_reboot();
    }
}

static void confirm_timeout_cb(void *arg)
{
    (void)arg;
    if (!pending_verify()) {
        return;
    }
    bool confirm_claimed;
    bool already_decided;
    bool commanded;
    bool session_confirmed;
    portENTER_CRITICAL(&s_decide_lock);
    session_confirmed = s_ctx.session_confirmed;
    confirm_claimed = s_confirm_claimed;
    already_decided = s_rollback_decided;
    commanded = s_rollback_commanded;
    if (!(session_confirmed && !s_selftest_registered) && !confirm_claimed && !already_decided) {
        s_rollback_decided = true;
    }
    portEXIT_CRITICAL(&s_decide_lock);
    if (session_confirmed && !s_selftest_registered) {
        return;
    }
    if (confirm_claimed) {
        ESP_LOGW(TAG, "the confirm deadline passed as a passing self-test confirms this image "
                      "— not rolling back");
        return;
    }
    if (already_decided) {
        if (ff_safe_window_registered()) {
            if (!commanded) {
                start_rollback_gate();
            }
            return;
        }
        ESP_LOGE(TAG, "the failed self-test's rollback has not happened by the confirm "
                      "deadline — rolling back now");
        esp_ota_mark_app_invalid_rollback_and_reboot();
        return;
    }
    if (s_ctx.session_confirmed) {
        ESP_LOGE(TAG, "no self-test result %d s after an OTA boot — marking this image invalid "
                      "and rolling back to the previous slot",
                 (int)s_confirm_deadline_s);
    } else {
        ESP_LOGE(TAG, "no working session %d s after an OTA boot — marking this image invalid "
                      "and rolling back to the previous slot",
                 (int)s_confirm_deadline_s);
    }

    if (ff_safe_window_registered()) {
        start_rollback_gate();
        return;
    }

    if (s_txn.kind == TXN_CONFIRMING && s_rollback_timer != NULL &&
        esp_timer_start_once(s_rollback_timer, (uint64_t)ROLLBACK_REPORT_GRACE_MS * 1000ULL) ==
            ESP_OK) {
        if (xTaskCreate(rollback_report_task, "ff_rbrep", ROLLBACK_REPORT_STACK, NULL, 4, NULL) !=
            pdPASS) {
            ESP_LOGE(TAG, "cannot start the %s report — rolling back unreported in %d ms",
                     FF_STATUS_ROLLING_BACK, ROLLBACK_REPORT_GRACE_MS);
        }
        return;
    }
    esp_ota_mark_app_invalid_rollback_and_reboot();
}

void selftest_done(bool passed, const char *reason)
{
    bool too_late;
    portENTER_CRITICAL(&s_decide_lock);
    too_late = s_rollback_decided;
    if (!too_late) {
        if (passed) {
            s_confirm_claimed = true;
        } else {
            s_rollback_decided = true;
        }
    }
    portEXIT_CRITICAL(&s_decide_lock);
    if (too_late) {
        ESP_LOGW(TAG, "the self-test answered after the confirm deadline — the rollback already "
                      "decided stands; NOT confirming");
        return;
    }

    const txn_kind_t kind = s_txn.kind;
    if (passed) {
        ESP_LOGW(TAG, "the custom self-test passed inside the confirm deadline — confirming "
                      "this OTA image");
        if (kind == TXN_BOOTED_NEW) {
            if (accept_unverified_image()) {
                s_txn.kind = TXN_ALREADY_CONFIRMED;
                s_txn.terminal_msg_id =
                    enqueue_status(s_txn.cmd_id, FF_STATUS_CONFIRMED, 100, s_txn.detail);
            }
            return;
        }
        const bool marked = confirm_this_image();
        if (marked && kind == TXN_CONFIRMING) {
            strlcpy(s_txn.detail, DETAIL_SELFTEST_PASSED, sizeof(s_txn.detail));
            s_txn.kind = TXN_ALREADY_CONFIRMED;
            s_txn.terminal_msg_id =
                enqueue_status(s_txn.cmd_id, FF_STATUS_CONFIRMED, 100, s_txn.detail);
        }
        return;
    }

    const bool owed = kind == TXN_CONFIRMING || kind == TXN_BOOTED_NEW;
    if (owed) {
        char head[FF_TXN_MAX_DETAIL];
        selftest_failed_head(head, sizeof(head), reason);
        (void)ff_txn_set_detail(s_txn.cmd_id, head);
    }
    ESP_LOGE(TAG, "the custom self-test failed — marking this image invalid and rolling back to "
                  "the previous slot");
    if (ff_safe_window_registered()) {
        start_rollback_gate();
        return;
    }

    if (owed && s_rollback_timer == NULL) {
        const esp_timer_create_args_t rollback_args = {
            .callback = rollback_now_cb,
            .name = "ff_rollback",
        };
        if (esp_timer_create(&rollback_args, &s_rollback_timer) != ESP_OK) {
            s_rollback_timer = NULL;
        }
    }
    if (owed && s_rollback_timer != NULL &&
        esp_timer_start_once(s_rollback_timer, (uint64_t)ROLLBACK_REPORT_GRACE_MS * 1000ULL) ==
            ESP_OK) {
        if (s_ctx.connected) {
            (void)enqueue_status(s_txn.cmd_id, FF_STATUS_ROLLING_BACK, FF_STATUS_PCT_NONE, NULL);
        } else {
            ESP_LOGW(TAG, "no broker session: %s for %s is not reported", FF_STATUS_ROLLING_BACK,
                     s_txn.cmd_id);
        }
        return;
    }
    esp_ota_mark_app_invalid_rollback_and_reboot();
    ESP_LOGE(TAG, "esp_ota_mark_app_invalid_rollback_and_reboot returned — there is no other "
                  "bootable slot to go back to");
}

void ff_mqtt_arm_confirm_timer(void)
{
    if (s_confirm_armed) {
        ESP_LOGW(TAG, "the confirm timer is already armed — ignoring the second call");
        return;
    }
    s_confirm_armed = true;

    if (!pending_verify()) {
        ESP_LOGD(TAG, "not an OTA boot: no confirm timer armed");
        return;
    }
    const esp_timer_create_args_t rollback_args = {
        .callback = rollback_now_cb,
        .name = "ff_rollback",
    };
    if (esp_timer_create(&rollback_args, &s_rollback_timer) != ESP_OK) {
        s_rollback_timer = NULL;
        ESP_LOGW(TAG, "no rollback grace timer: a timed-out image will roll back without "
                      "reporting %s",
                 FF_STATUS_ROLLING_BACK);
    }

    const esp_timer_create_args_t args = {
        .callback = confirm_timeout_cb,
        .name = "ff_confirm",
    };
    esp_timer_handle_t timer = NULL;
    if (esp_timer_create(&args, &timer) == ESP_OK) {
        s_confirm_timer = timer;
        s_confirm_deadline_s = CONFIRM_TIMEOUT_S;
        s_confirm_origin_us = esp_timer_get_time();
        ESP_ERROR_CHECK(esp_timer_start_once(timer, (uint64_t)CONFIRM_TIMEOUT_S * 1000000ULL));
        ESP_LOGW(TAG, "OTA boot: %d s from now to reach the fleet or roll back",
                 CONFIRM_TIMEOUT_S);
    } else {
        ESP_LOGE(TAG, "cannot create the confirm timer: this OTA image cannot roll itself "
                      "back — only a reset returns this board to its previous slot");
    }
}

uint32_t confirm_timeout_clamp(double requested_s)
{
    if (!(requested_s >= CONFIRM_TIMEOUT_MIN_S)) {
        return CONFIRM_TIMEOUT_MIN_S;
    }
    if (requested_s > CONFIRM_TIMEOUT_MAX_S) {
        return CONFIRM_TIMEOUT_MAX_S;
    }
    return (uint32_t)requested_s;
}

static void rearm_confirm_deadline(uint32_t deadline_s)
{
    if (!s_confirm_armed || s_confirm_timer == NULL) {
        ESP_LOGE(TAG, "transaction %s: no confirm timer to re-arm — the boot arm's outcome "
                      "stands",
                 s_txn.cmd_id);
        return;
    }
    if (deadline_s == s_confirm_deadline_s) {
        ESP_LOGI(TAG, "transaction %s: confirm deadline stays %u s", s_txn.cmd_id,
                 (unsigned)deadline_s);
        return;
    }

    int64_t remaining_us =
        (int64_t)deadline_s * 1000000 - (esp_timer_get_time() - s_confirm_origin_us);
    if (remaining_us < 0) {
        remaining_us = 0;
    }
    if (esp_timer_stop(s_confirm_timer) != ESP_OK) {
        ESP_LOGW(TAG, "transaction %s: the confirm timer has already fired — its decision "
                      "stands",
                 s_txn.cmd_id);
        return;
    }
    s_confirm_deadline_s = deadline_s;
    ESP_ERROR_CHECK(esp_timer_start_once(s_confirm_timer, (uint64_t)remaining_us));
    ESP_LOGW(TAG, "transaction %s: confirm deadline %u s from image start (%d s left)",
             s_txn.cmd_id, (unsigned)deadline_s, (int)(remaining_us / 1000000));
}

void classify_txn(void)
{
    s_txn.kind = TXN_NONE;
    s_txn.cmd_id[0] = '\0';
    s_txn.detail[0] = '\0';
    s_txn.confirming_sent = false;
    s_txn.terminal_msg_id = -1;

    if (pending_verify()) {
        ff_identity_note_rollback_capable();
    }

    ff_txn_t rec;
    esp_err_t err = ff_txn_load(&rec);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        return;
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot read the transaction record (%s) — no outcome will be reported "
                      "for it",
                 esp_err_to_name(err));
        return;
    }
    strlcpy(s_txn.cmd_id, rec.cmd_id, sizeof(s_txn.cmd_id));

    const esp_partition_t *running = esp_ota_get_running_partition();
    if (running != NULL && running->address == rec.target_addr) {
        esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
        if (esp_ota_get_state_partition(running, &state) == ESP_OK) {
            if (state == ESP_OTA_IMG_PENDING_VERIFY) {
                s_txn.kind = TXN_CONFIRMING;
                ESP_LOGW(TAG, "transaction %s: confirming on %s", s_txn.cmd_id, running->label);
                rearm_confirm_deadline(rec.confirm_timeout_s == 0
                                           ? CONFIRM_TIMEOUT_S
                                           : confirm_timeout_clamp((double)rec.confirm_timeout_s));
                return;
            }
            if (state == ESP_OTA_IMG_VALID) {
                s_txn.kind = TXN_ALREADY_CONFIRMED;
                if (strncmp(rec.detail, DETAIL_SELFTEST_PREFIX, strlen(DETAIL_SELFTEST_PREFIX)) ==
                    0) {
                    strlcpy(s_txn.detail, DETAIL_SELFTEST_PASSED, sizeof(s_txn.detail));
                }
                ESP_LOGW(TAG, "transaction %s: already confirmed on %s", s_txn.cmd_id,
                         running->label);
                return;
            }
            if (state == ESP_OTA_IMG_NEW) {
                s_txn.kind = TXN_BOOTED_NEW;
                strlcpy(s_txn.detail, DETAIL_BOOTED_NEW, sizeof(s_txn.detail));
                ESP_LOGW(TAG, "transaction %s: running %s in state new — the bootloader never "
                              "put it in pending_verify; rollback_capable would be false, "
                              "which this agent does not report until it is benched "
                              "(R2b-test-5)",
                         s_txn.cmd_id, running->label);
                return;
            }
        }
    } else if (running != NULL) {
        const esp_partition_t *invalid = esp_ota_get_last_invalid_partition();
        if (invalid != NULL && invalid->address == rec.target_addr) {
            s_txn.kind = TXN_ROLLED_BACK;
            if (strncmp(rec.detail, DETAIL_SELFTEST_PREFIX, strlen(DETAIL_SELFTEST_PREFIX)) ==
                    0 ||
                strcmp(rec.detail, DETAIL_ROLLED_BACK_BY_COMMAND) == 0) {
                compose_returned_detail(s_txn.detail, sizeof(s_txn.detail), rec.detail,
                                        running->label);
            } else {
                snprintf(s_txn.detail, sizeof(s_txn.detail), "returned to %s; %s did not confirm",
                         running->label, invalid->label);
            }
            ESP_LOGW(TAG, "transaction %s: rolled_back (%s)", s_txn.cmd_id, s_txn.detail);
            return;
        }
    }

    ESP_LOGW(TAG, "stale transaction record for %s — discarded", s_txn.cmd_id);
    (void)ff_txn_clear_if(s_txn.cmd_id);
    s_txn.cmd_id[0] = '\0';
}

void report_txn_on_connect(void)
{
    switch (s_txn.kind) {
    case TXN_CONFIRMING:
        if (!s_txn.confirming_sent) {
            s_txn.confirming_sent =
                enqueue_status(s_txn.cmd_id, FF_STATUS_CONFIRMING, FF_STATUS_PCT_NONE, NULL) > 0;
        }
        break;
    case TXN_ALREADY_CONFIRMED:
        if (s_txn.terminal_msg_id <= 0) {
            s_txn.terminal_msg_id =
                enqueue_status(s_txn.cmd_id, FF_STATUS_CONFIRMED, 100,
                               s_txn.detail[0] != '\0' ? s_txn.detail : NULL);
        }
        break;
    case TXN_BOOTED_NEW:
        break;
    case TXN_ROLLED_BACK:
        if (s_txn.terminal_msg_id <= 0) {
            s_txn.terminal_msg_id = enqueue_status(s_txn.cmd_id, FF_STATUS_ROLLED_BACK,
                                                   FF_STATUS_PCT_NONE, s_txn.detail);
        }
        break;
    case TXN_NONE:
    default:
        break;
    }
    if (s_rollback_waiting && (s_txn.kind == TXN_CONFIRMING || s_txn.kind == TXN_BOOTED_NEW)) {
        (void)enqueue_status(s_txn.cmd_id, FF_STATUS_AWAITING_SAFE_WINDOW, FF_STATUS_PCT_NONE,
                             FF_DETAIL_ROLLBACK_WAITS);
    }
}
