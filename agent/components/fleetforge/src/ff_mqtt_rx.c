/*
 * ff_mqtt_rx — inbound `dn/cmd` and `dn/cfg` parsing, `stage` validation, and four-verb handlers.
 *
 * `apply`, `cancel`, `rollback` and `reboot` answer once on `up/ack` (QoS 1, never retained)
 * and never reboot inside delivery: `command_reboot_task` waits `COMMAND_REBOOT_DRAIN_MS` plus
 * any registered safe-window rule before calling `esp_restart()`.
 */

#define FF_MQTT_INTERNAL_IMPL
#include "ff_mqtt_internal.h"

#include <string.h>

#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "ff_ota.h"
#include "ff_safe_window_internal.h"
#include "ff_sec_internal.h"

static const char *TAG = "ff-mqtt";

#define ACK_ACCEPTED "accepted"
#define ACK_DEFERRED "deferred"
#define ACK_REFUSED "refused"

#define ACK_NOTHING_STAGED "nothing staged for this cmd_id"
#define ACK_NO_IMAGE "no confirmed image to return to"
#define ACK_CANCEL_FIRST "cancel first"
#define ACK_NOT_IMPLEMENTED "not implemented"
#define ACK_NOTHING_TO_CANCEL "nothing to cancel for this cmd_id"
#define ACK_ALREADY_APPLYING "already applying; use rollback"
#define ACK_ROLLBACK_DECIDED "a rollback is already decided"
#define ACK_UPDATE_IN_PROGRESS "an update is in progress"
#define ACK_NOT_CONFIRMED "the running image is not confirmed yet"
#define ACK_REBOOT_PENDING "a reboot is already pending"
#define ACK_CMD_ID_MISSING "cmd_id missing"
#define ACK_ID_TOO_LONG "command id too long"
#define ACK_CONFIRMING_NOW "the image is confirming right now"
#define ACK_CANNOT_MARK "cannot mark this image invalid"
#define ACK_CANNOT_PUT_BACK "cannot put the boot slot back"
#define ACK_FITS(text) _Static_assert(sizeof(text) <= DETAIL_MAX + 1, text " is over 63 bytes")
ACK_FITS(DETAIL_ROLLED_BACK_BY_COMMAND DETAIL_RETURNED_TO "ota_0");
ACK_FITS(ACK_NOTHING_STAGED);
ACK_FITS(ACK_NO_IMAGE);
ACK_FITS(ACK_CANCEL_FIRST);
ACK_FITS(ACK_NOT_IMPLEMENTED);
ACK_FITS(ACK_NOTHING_TO_CANCEL);
ACK_FITS(ACK_ALREADY_APPLYING);
ACK_FITS(ACK_ROLLBACK_DECIDED);
ACK_FITS(ACK_UPDATE_IN_PROGRESS);
ACK_FITS(ACK_NOT_CONFIRMED);
ACK_FITS(ACK_REBOOT_PENDING);
ACK_FITS(ACK_CMD_ID_MISSING);
ACK_FITS(ACK_ID_TOO_LONG);
ACK_FITS(ACK_CONFIRMING_NOW);
ACK_FITS(ACK_CANNOT_MARK);
ACK_FITS(ACK_CANNOT_PUT_BACK);

#define COMMAND_REBOOT_DRAIN_MS 1500
#define COMMAND_REBOOT_STACK 4096
#define COMMAND_REBOOT_PRIO 3

typedef enum {
    FIELD_OK,
    FIELD_MISSING,
    FIELD_TOO_LONG,
} field_result_t;

static field_result_t take_string(const cJSON *obj, const char *key, char *out, size_t len)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (!cJSON_IsString(item) || item->valuestring[0] == '\0') {
        return FIELD_MISSING;
    }
    if (strlcpy(out, item->valuestring, len) >= len) {
        out[0] = '\0';
        return FIELD_TOO_LONG;
    }
    return FIELD_OK;
}

static bool is_lowercase_sha256(const char *s)
{
    for (size_t i = 0; i < 64; i++) {
        char c = s[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) {
            return false;
        }
    }
    return s[64] == '\0';
}

/* R6-SEC-1: `ecdsa-p256-sha256:<base64_der>` (80..120 base64 characters), validated at the
 * command contract before any slot is erased or downloaded. */
static bool is_valid_artifact_sig(const char *s)
{
    static const char PREFIX[] = "ecdsa-p256-sha256:";
    const size_t prefix_len = sizeof(PREFIX) - 1;
    if (strncmp(s, PREFIX, prefix_len) != 0) {
        return false;
    }
    const char *b64 = s + prefix_len;
    size_t len = 0;
    while (b64[len] != '\0') {
        char c = b64[len];
        bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                  (c >= '0' && c <= '9') || c == '+' || c == '/' || c == '=';
        if (!ok || len >= 120) {
            return false;
        }
        len++;
    }
    return len >= 80 && len <= 120;
}

/* Parse a `stage` command and pass a plain C struct across the `ff_ota` contract. */
static void on_stage(const cJSON *root, const char *id)
{
    ff_ota_cmd_t cmd;
    memset(&cmd, 0, sizeof(cmd));

    if (strlcpy(cmd.cmd_id, id, sizeof(cmd.cmd_id)) >= sizeof(cmd.cmd_id)) {
        ESP_LOGE(TAG, "stage command id is longer than this firmware's %u-byte buffer — "
                      "ignored; no status can be reported against an id we cannot echo",
                 (unsigned)sizeof(cmd.cmd_id));
        return;
    }

    if (ff_ota_is_handling(cmd.cmd_id)) {
        ESP_LOGW(TAG, "stage id=%s is the update this board is already carrying out — "
                      "ignored (re-delivery); its outcome is reported when it ends",
                 cmd.cmd_id);
        return;
    }
    if (held_txn_id() != NULL && strcmp(held_txn_id(), cmd.cmd_id) == 0) {
        ESP_LOGW(TAG, "stage id=%s is the transaction this boot already holds — ignored",
                 cmd.cmd_id);
        return;
    }
    const bool decided = s_rollback_decided;
    if (s_cmd_reboot_pending || decided) {
        ESP_LOGW(TAG, "stage id=%s refused: %s", cmd.cmd_id,
                 decided ? ACK_ROLLBACK_DECIDED : ACK_REBOOT_PENDING);
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE,
                               decided ? ACK_ROLLBACK_DECIDED : ACK_REBOOT_PENDING);
        return;
    }

    const cJSON *artifact = cJSON_GetObjectItemCaseSensitive(root, "artifact");
    if (!cJSON_IsObject(artifact)) {
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE,
                               "no artifact in command");
        return;
    }

    switch (take_string(artifact, "url", cmd.url, sizeof(cmd.url))) {
    case FIELD_OK: break;
    case FIELD_MISSING:
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE, "artifact url missing");
        return;
    case FIELD_TOO_LONG:
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE, "artifact url too long");
        return;
    }

    switch (take_string(artifact, "sha256", cmd.sha256, sizeof(cmd.sha256))) {
    case FIELD_OK: break;
    case FIELD_MISSING:
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE, "artifact sha256 missing");
        return;
    case FIELD_TOO_LONG:
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE, "artifact sha256 malformed");
        return;
    }
    if (!is_lowercase_sha256(cmd.sha256)) {
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE, "artifact sha256 malformed");
        return;
    }
    (void)take_string(artifact, "version", cmd.version, sizeof(cmd.version));

    switch (take_string(artifact, "type", cmd.type, sizeof(cmd.type))) {
    case FIELD_OK: break;
    case FIELD_MISSING: strlcpy(cmd.type, "app", sizeof(cmd.type)); break;
    case FIELD_TOO_LONG:
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE, "artifact type malformed");
        return;
    }

    const cJSON *size = cJSON_GetObjectItemCaseSensitive(artifact, "size");
    if (size != NULL && !cJSON_IsNull(size)) {
        const double raw_size = cJSON_IsNumber(size) ? size->valuedouble : -1.0;
        if (!(raw_size > 0.0 && raw_size <= (double)(16U * 1024U * 1024U)) ||
            (double)(size_t)raw_size != raw_size) {
            ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE,
                                   "artifact size invalid");
            return;
        }
        cmd.size = (size_t)raw_size;
    }

    switch (take_string(artifact, "sig", cmd.sig, sizeof(cmd.sig))) {
    case FIELD_OK:
        if (!is_valid_artifact_sig(cmd.sig)) {
            ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE,
                                   "artifact sig malformed");
            return;
        }
        break;
    case FIELD_MISSING:
        break;
    case FIELD_TOO_LONG:
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE,
                               "artifact sig malformed");
        return;
    }

    cmd.apply_now = true;
    const cJSON *apply = cJSON_GetObjectItemCaseSensitive(root, "apply");
    if (cJSON_IsString(apply) && strcmp(apply->valuestring, "on_command") == 0) {
        cmd.apply_now = false;
    }

    cmd.confirm_timeout_s = CONFIRM_TIMEOUT_S;
    const cJSON *confirm = cJSON_GetObjectItemCaseSensitive(root, "confirm_timeout_s");
    if (cJSON_IsNumber(confirm)) {
        cmd.confirm_timeout_s = confirm_timeout_clamp(confirm->valuedouble);
        if (!(confirm->valuedouble >= CONFIRM_TIMEOUT_MIN_S &&
              confirm->valuedouble <= CONFIRM_TIMEOUT_MAX_S)) {
            ESP_LOGW(TAG, "stage id=%s: confirm_timeout_s is outside %d-%d s — using %u s",
                     cmd.cmd_id, CONFIRM_TIMEOUT_MIN_S, CONFIRM_TIMEOUT_MAX_S,
                     (unsigned)cmd.confirm_timeout_s);
        }
    } else if (confirm != NULL) {
        ESP_LOGW(TAG, "stage id=%s: confirm_timeout_s is not a number — using %u s", cmd.cmd_id,
                 (unsigned)cmd.confirm_timeout_s);
    }

    esp_err_t err = ff_ota_start(&cmd);
    if (err == ESP_ERR_INVALID_STATE) {
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE,
                               "another update is already in progress");
    } else if (err != ESP_OK) {
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE,
                               "cannot start the update");
        ESP_LOGE(TAG, "ff_ota_start: %s", esp_err_to_name(err));
    }
}

static char s_reboot_txn_id[FF_TXN_MAX_CMD_ID];
static char s_rollback_txn_id[FF_TXN_MAX_CMD_ID];

static StaticTask_t s_reboot_tcb;
static StackType_t s_reboot_stack[COMMAND_REBOOT_STACK];
static TaskHandle_t s_reboot_task;

typedef enum {
    CMD_REBOOT_PLAIN,
    CMD_REBOOT_APPLY,
    CMD_REBOOT_ROLLBACK,
} cmd_reboot_kind_t;
static cmd_reboot_kind_t s_reboot_kind;

static portMUX_TYPE s_reboot_lock = portMUX_INITIALIZER_UNLOCKED;
static bool s_reboot_deferred;
static bool s_reboot_committed;
static bool s_reboot_hold;
static bool s_reboot_abort;

static bool wait_for_the_window(void)
{
    bool logged = false;
    for (;;) {
        bool abort_now;
        bool hold;
        portENTER_CRITICAL(&s_reboot_lock);
        abort_now = s_reboot_abort;
        hold = s_reboot_hold;
        portEXIT_CRITICAL(&s_reboot_lock);
        if (abort_now) {
            return false;
        }
        if (!hold && ff_safe_window_is_safe()) {
            bool committed;
            portENTER_CRITICAL(&s_reboot_lock);
            committed = !s_reboot_hold && !s_reboot_abort;
            if (committed) {
                s_reboot_committed = true;
            }
            portEXIT_CRITICAL(&s_reboot_lock);
            if (committed) {
                return true;
            }
            continue;
        }
        if (!hold && !logged) {
            ESP_LOGW(TAG, "command %s: the reboot waits for a safe window", s_reboot_cmd_id);
            logged = true;
        }
        vTaskDelay(pdMS_TO_TICKS(FF_SAFE_WINDOW_POLL_MS));
    }
}

static void command_reboot_task(void *arg)
{
    (void)arg;
    for (;;) {
        (void)ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        vTaskDelay(pdMS_TO_TICKS(COMMAND_REBOOT_DRAIN_MS));
        if (!wait_for_the_window()) {
            portENTER_CRITICAL(&s_reboot_lock);
            s_reboot_committed = false;
            s_reboot_hold = false;
            s_reboot_abort = false;
            s_reboot_deferred = false;
            s_reboot_txn_id[0] = '\0';
            s_cmd_reboot_pending = false;
            portEXIT_CRITICAL(&s_reboot_lock);
            ESP_LOGW(TAG, "command %s: the deferred apply was cancelled — no reboot",
                     s_reboot_cmd_id);
            continue;
        }
        if (s_reboot_deferred) {
            if (s_reboot_kind == CMD_REBOOT_APPLY) {
                (void)enqueue_status(s_reboot_txn_id, FF_STATUS_APPLYING, 100, NULL);
                (void)enqueue_status(s_reboot_txn_id, FF_STATUS_REBOOTING, 100, NULL);
            } else if (s_reboot_kind == CMD_REBOOT_ROLLBACK) {
                (void)enqueue_status(s_rollback_txn_id, FF_STATUS_ROLLING_BACK,
                                     FF_STATUS_PCT_NONE, NULL);
            }
            vTaskDelay(pdMS_TO_TICKS(COMMAND_REBOOT_DRAIN_MS));
            while (!ff_safe_window_is_safe()) {
                vTaskDelay(pdMS_TO_TICKS(FF_SAFE_WINDOW_POLL_MS));
            }
        }
        ESP_LOGW(TAG, "rebooting for command %s", s_reboot_cmd_id);
        esp_restart();
    }
}

static void schedule_command_reboot(const char *id)
{
    if (s_cmd_reboot_pending) {
        ESP_LOGE(TAG, "command %s: a command reboot is already scheduled — not scheduling "
                      "another",
                 id);
        return;
    }
    esp_err_t err = ff_txn_note_reboot_cmd(id);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "command %s: cannot keep its id across the reboot (%s)", id,
                 esp_err_to_name(err));
    }
    strlcpy(s_reboot_cmd_id, id, sizeof(s_reboot_cmd_id));
    s_cmd_reboot_pending = true;
    if (s_reboot_task == NULL) {
        s_reboot_task = xTaskCreateStatic(command_reboot_task, "ff_reboot", COMMAND_REBOOT_STACK,
                                          NULL, COMMAND_REBOOT_PRIO, s_reboot_stack,
                                          &s_reboot_tcb);
    }
    if (s_reboot_task == NULL) {
        ESP_LOGE(TAG, "command %s: cannot start the reboot task — reset the board to finish",
                 id);
        return;
    }
    xTaskNotifyGive(s_reboot_task);
}

static bool other_slot_is_returnable(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *other = esp_ota_get_next_update_partition(NULL);
    if (running == NULL || other == NULL || other == running) {
        return false;
    }
    esp_ota_img_states_t state = ESP_OTA_IMG_INVALID;
    if (esp_ota_get_state_partition(other, &state) != ESP_OK) {
        return false;
    }
    if (state != ESP_OTA_IMG_VALID && state != ESP_OTA_IMG_UNDEFINED) {
        return false;
    }
    return esp_ota_check_rollback_is_possible();
}

static void store_rollback_head(const char *txn_id)
{
    (void)ff_txn_set_detail(txn_id, DETAIL_ROLLED_BACK_BY_COMMAND);
}

static bool open_rollback_txn(const char *id)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_err_t err = running != NULL ? ff_txn_save(id, running->address, 0) : ESP_ERR_NOT_FOUND;
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "rollback %s: cannot record the transaction (%s) — rolled_back will not "
                      "be reported after the reboot",
                 id, esp_err_to_name(err));
        return false;
    }
    store_rollback_head(id);
    return true;
}

static void on_apply(const cJSON *root, const char *id)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, "cmd_id");
    if (!cJSON_IsString(item) || item->valuestring[0] == '\0') {
        enqueue_ack(id, "apply", ACK_REFUSED, NULL, ACK_CMD_ID_MISSING);
        return;
    }
    const char *cmd_id = item->valuestring;
    if (strlen(cmd_id) >= FF_TXN_MAX_CMD_ID) {
        enqueue_ack(id, "apply", ACK_REFUSED, NULL, ACK_NOTHING_STAGED);
        return;
    }
    const bool released = s_cmd_reboot_pending && strcmp(s_reboot_txn_id, cmd_id) == 0;
    const bool booted = held_txn_id() != NULL && strcmp(s_txn.cmd_id, cmd_id) == 0;
    char held[FF_TXN_MAX_CMD_ID];
    const ff_ota_txn_t phase = ff_ota_txn_phase(cmd_id, held, sizeof(held));
    if (released || booted || phase == FF_OTA_TXN_APPLYING || phase == FF_OTA_TXN_AWAITING) {
        enqueue_ack(id, "apply", ACK_ACCEPTED, cmd_id, NULL);
        return;
    }
    if (phase != FF_OTA_TXN_STAGED) {
        enqueue_ack(id, "apply", ACK_REFUSED, phase == FF_OTA_TXN_TRANSFER ? cmd_id : NULL,
                    ACK_NOTHING_STAGED);
        return;
    }
    if (s_cmd_reboot_pending) {
        enqueue_ack(id, "apply", ACK_REFUSED, cmd_id, ACK_REBOOT_PENDING);
        return;
    }

    const bool safe = ff_safe_window_is_safe();
    if (safe) {
        enqueue_ack(id, "apply", ACK_ACCEPTED, cmd_id, NULL);
        (void)enqueue_status(cmd_id, FF_STATUS_APPLYING, 100, NULL);
        (void)enqueue_status(cmd_id, FF_STATUS_REBOOTING, 100, NULL);
        ESP_LOGW(TAG, "apply %s: releasing the staged update %s — rebooting into it", id, cmd_id);
    } else {
        enqueue_ack(id, "apply", ACK_DEFERRED, cmd_id, NULL);
        (void)enqueue_status(cmd_id, FF_STATUS_AWAITING_SAFE_WINDOW, 100, FF_DETAIL_APPLY_WAITS);
        ESP_LOGW(TAG, "apply %s: the staged update %s waits for a safe window", id, cmd_id);
    }
    strlcpy(s_reboot_txn_id, cmd_id, sizeof(s_reboot_txn_id));
    s_reboot_kind = CMD_REBOOT_APPLY;
    s_reboot_deferred = !safe;
    schedule_command_reboot(id);
}

static void on_cancel(const cJSON *root, const char *id)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, "cmd_id");
    if (!cJSON_IsString(item) || item->valuestring[0] == '\0') {
        enqueue_ack(id, "cancel", ACK_REFUSED, NULL, ACK_CMD_ID_MISSING);
        return;
    }
    const char *cmd_id = item->valuestring;
    if (strlen(cmd_id) >= FF_TXN_MAX_CMD_ID) {
        enqueue_ack(id, "cancel", ACK_REFUSED, NULL, ACK_NOTHING_TO_CANCEL);
        return;
    }
    if (held_txn_id() != NULL && strcmp(s_txn.cmd_id, cmd_id) == 0) {
        const bool decided = s_rollback_decided;
        enqueue_ack(id, "cancel", ACK_REFUSED, cmd_id,
                    decided ? ACK_ROLLBACK_DECIDED : ACK_ALREADY_APPLYING);
        return;
    }
    bool took_back = false;
    if (s_cmd_reboot_pending && strcmp(s_reboot_txn_id, cmd_id) == 0) {
        portENTER_CRITICAL(&s_reboot_lock);
        if (s_reboot_kind == CMD_REBOOT_APPLY && s_reboot_deferred && !s_reboot_committed) {
            s_reboot_hold = true;
            took_back = true;
        }
        portEXIT_CRITICAL(&s_reboot_lock);
        if (!took_back) {
            enqueue_ack(id, "cancel", ACK_REFUSED, cmd_id, ACK_ALREADY_APPLYING);
            return;
        }
    } else if (ff_ota_txn_phase(cmd_id, NULL, 0) == FF_OTA_TXN_APPLYING) {
        enqueue_ack(id, "cancel", ACK_REFUSED, cmd_id, ACK_ALREADY_APPLYING);
        return;
    }

    bool reported = false;
    esp_err_t err = ff_ota_cancel(cmd_id, &reported);
    if (took_back) {
        portENTER_CRITICAL(&s_reboot_lock);
        s_reboot_hold = false;
        if (err == ESP_OK) {
            s_reboot_abort = true;
        }
        portEXIT_CRITICAL(&s_reboot_lock);
    }
    if (err == ESP_ERR_NOT_FOUND) {
        enqueue_ack(id, "cancel", ACK_REFUSED, NULL, ACK_NOTHING_TO_CANCEL);
        return;
    }
    if (err == ESP_ERR_INVALID_STATE) {
        enqueue_ack(id, "cancel", ACK_REFUSED, cmd_id, ACK_ALREADY_APPLYING);
        return;
    }
    if (err != ESP_OK) {
        enqueue_ack(id, "cancel", ACK_REFUSED, cmd_id, ACK_CANNOT_PUT_BACK);
        return;
    }

    enqueue_ack(id, "cancel", ACK_ACCEPTED, cmd_id, NULL);
    if (!reported) {
        (void)enqueue_status(cmd_id, FF_STATUS_CANCELLED, FF_STATUS_PCT_NONE, NULL);
    }
}

static void on_rollback(const char *id)
{
    if (s_rollback_txn_id[0] != '\0') {
        enqueue_ack(id, "rollback", ACK_ACCEPTED, s_rollback_txn_id, NULL);
        return;
    }
    const bool decided = s_rollback_decided;
    if (decided) {
        enqueue_ack(id, "rollback", ACK_ACCEPTED, held_txn_id(), NULL);
        return;
    }
    if (s_cmd_reboot_pending) {
        enqueue_ack(id, "rollback", ACK_REFUSED, NULL, ACK_REBOOT_PENDING);
        return;
    }
    char held[FF_TXN_MAX_CMD_ID];
    if (ff_ota_txn_phase(NULL, held, sizeof(held)) != FF_OTA_TXN_NONE) {
        enqueue_ack(id, "rollback", ACK_REFUSED, held[0] != '\0' ? held : NULL, ACK_CANCEL_FIRST);
        return;
    }
    if (!other_slot_is_returnable()) {
        enqueue_ack(id, "rollback", ACK_REFUSED, NULL, ACK_NO_IMAGE);
        return;
    }

    const char *txn = id;
    if (pending_verify() || s_txn.kind == TXN_BOOTED_NEW) {
        bool confirm_first;
        bool beaten;
        portENTER_CRITICAL(&s_decide_lock);
        confirm_first = s_confirm_claimed;
        beaten = s_rollback_decided;
        if (!confirm_first && !beaten) {
            s_rollback_decided = true;
            s_rollback_commanded = true;
        }
        portEXIT_CRITICAL(&s_decide_lock);
        if (confirm_first) {
            enqueue_ack(id, "rollback", ACK_REFUSED, held_txn_id(), ACK_CONFIRMING_NOW);
            return;
        }
        if (beaten) {
            enqueue_ack(id, "rollback", ACK_ACCEPTED, held_txn_id(), NULL);
            return;
        }
        const txn_kind_t kind = s_txn.kind;
        if (kind == TXN_CONFIRMING || kind == TXN_BOOTED_NEW) {
            store_rollback_head(s_txn.cmd_id);
            txn = s_txn.cmd_id;
        } else {
            (void)open_rollback_txn(id);
        }
        esp_err_t err = esp_ota_mark_app_invalid_rollback();
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "rollback %s: cannot mark this image invalid (%s) — rebooting anyway",
                     id, esp_err_to_name(err));
        }
    } else {
        const bool recorded = open_rollback_txn(id);
        esp_err_t err = esp_ota_mark_app_invalid_rollback();
        if (err != ESP_OK) {
            if (recorded) {
                (void)ff_txn_clear_if(id);
            }
            ESP_LOGE(TAG, "rollback %s: cannot mark this image invalid (%s) — not rolling back",
                     id, esp_err_to_name(err));
            enqueue_ack(id, "rollback", ACK_REFUSED, NULL, ACK_CANNOT_MARK);
            return;
        }
    }

    const bool safe = ff_safe_window_is_safe();
    if (safe) {
        ESP_LOGW(TAG, "rollback %s: this image is marked invalid — returning to the other slot",
                 id);
        enqueue_ack(id, "rollback", ACK_ACCEPTED, txn, NULL);
        (void)enqueue_status(txn, FF_STATUS_ROLLING_BACK, FF_STATUS_PCT_NONE, NULL);
    } else {
        ESP_LOGW(TAG, "rollback %s: this image is marked invalid — the return waits for a safe "
                      "window",
                 id);
        enqueue_ack(id, "rollback", ACK_DEFERRED, txn, NULL);
        (void)enqueue_status(txn, FF_STATUS_AWAITING_SAFE_WINDOW, FF_STATUS_PCT_NONE,
                             FF_DETAIL_ROLLBACK_WAITS);
    }
    strlcpy(s_rollback_txn_id, txn, sizeof(s_rollback_txn_id));
    s_reboot_kind = CMD_REBOOT_ROLLBACK;
    s_reboot_deferred = !safe;
    schedule_command_reboot(id);
}

static void on_reboot(const char *id)
{
    if (s_cmd_reboot_pending) {
        enqueue_ack(id, "reboot", ACK_ACCEPTED, NULL, NULL);
        return;
    }
    const bool decided = s_rollback_decided;
    if (decided) {
        enqueue_ack(id, "reboot", ACK_REFUSED, held_txn_id(), ACK_ROLLBACK_DECIDED);
        return;
    }
    char held[FF_TXN_MAX_CMD_ID];
    if (ff_ota_txn_phase(NULL, held, sizeof(held)) != FF_OTA_TXN_NONE) {
        enqueue_ack(id, "reboot", ACK_REFUSED, held[0] != '\0' ? held : NULL,
                    ACK_UPDATE_IN_PROGRESS);
        return;
    }
    if (pending_verify() || s_txn.kind == TXN_CONFIRMING || s_txn.kind == TXN_BOOTED_NEW) {
        enqueue_ack(id, "reboot", ACK_REFUSED, held_txn_id(), ACK_NOT_CONFIRMED);
        return;
    }

    if (ff_safe_window_is_safe()) {
        enqueue_ack(id, "reboot", ACK_ACCEPTED, NULL, NULL);
    } else {
        enqueue_ack(id, "reboot", ACK_DEFERRED, NULL, NULL);
        ESP_LOGW(TAG, "reboot %s: waits for a safe window", id);
    }
    s_reboot_kind = CMD_REBOOT_PLAIN;
    s_reboot_deferred = false;
    schedule_command_reboot(id);
}

void on_command(ff_mqtt_ctx_t *ctx, const char *topic, int topic_len, const char *data,
                int data_len)
{
    cJSON *root = cJSON_ParseWithLength(data, (size_t)data_len);
    const char *type = "?";
    const char *type_echo = NULL;
    const char *id = NULL;
    if (root != NULL) {
        const cJSON *type_item = cJSON_GetObjectItemCaseSensitive(root, "type");
        const cJSON *id_item = cJSON_GetObjectItemCaseSensitive(root, "id");
        if (cJSON_IsString(type_item)) {
            type = type_item->valuestring;
            type_echo = type;
        }
        if (cJSON_IsString(id_item)) {
            id = id_item->valuestring;
        }
    }

    if (id != NULL && strcmp(id, ctx->last_command_id) == 0) {
        ESP_LOGW(TAG, "duplicate command id=%s — ignored (QoS 1 redelivery)", id);
        cJSON_Delete(root);
        return;
    }
    if (id != NULL && s_reboot_cmd_id[0] != '\0' && strcmp(id, s_reboot_cmd_id) == 0) {
        ESP_LOGW(TAG, "duplicate command id=%s — ignored (this board already rebooted for it)",
                 id);
        cJSON_Delete(root);
        return;
    }
    if (id != NULL) {
        strlcpy(ctx->last_command_id, id, sizeof(ctx->last_command_id));
    }

    ESP_LOGI(TAG, "%.*s id=%s type=%s", topic_len, topic, id != NULL ? id : "?", type);

    if (strcmp(type, "stage") == 0) {
        if (id == NULL) {
            ESP_LOGE(TAG, "a stage command with no id cannot be deduplicated or reported "
                          "against — ignored");
        } else {
            on_stage(root, id);
        }
    } else if (id == NULL) {
        ESP_LOGW(TAG, "command type=%s has no id — ignored; there is nothing to answer against",
                 type);
    } else if (strlen(id) >= FF_TXN_MAX_CMD_ID) {
        enqueue_ack(id, type_echo, ACK_REFUSED, NULL, ACK_ID_TOO_LONG);
    } else if (strcmp(type, "apply") == 0) {
        on_apply(root, id);
    } else if (strcmp(type, "cancel") == 0) {
        on_cancel(root, id);
    } else if (strcmp(type, "rollback") == 0) {
        on_rollback(id);
    } else if (strcmp(type, "reboot") == 0) {
        on_reboot(id);
    } else {
        enqueue_ack(id, type_echo, ACK_REFUSED, NULL, ACK_NOT_IMPLEMENTED);
    }
    cJSON_Delete(root);
}

void on_cfg(ff_mqtt_ctx_t *ctx, const char *data, int data_len)
{
    if (data == NULL || data_len <= 0) {
        return;
    }
    cJSON *root = cJSON_ParseWithLength(data, (size_t)data_len);
    if (root == NULL) {
        ESP_LOGW(TAG, "dn/cfg: invalid JSON (%d B); ignored", data_len);
        return;
    }
    const cJSON *stay_awake = cJSON_GetObjectItemCaseSensitive(root, "stay_awake");
    if (cJSON_IsBool(stay_awake)) {
        s_stay_awake = cJSON_IsTrue(stay_awake);
        ESP_LOGI(TAG, "dn/cfg: stay_awake=%s", s_stay_awake ? "true" : "false");
    }
    const cJSON *secrets_rev = cJSON_GetObjectItemCaseSensitive(root, "secrets_rev");
    if (cJSON_IsNumber(secrets_rev) && secrets_rev->valuedouble >= 0.0) {
        uint32_t clamped = secrets_rev->valuedouble >= (double)UINT32_MAX
                               ? UINT32_MAX
                               : (uint32_t)secrets_rev->valuedouble;
        ff_sec_on_cfg(clamped, ctx->client, ctx->topic_announce);
    }
    cJSON_Delete(root);
}
