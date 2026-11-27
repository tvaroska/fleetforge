/*
 * ff_ota — download, write the inactive slot, verify, apply. See ff_ota.h.
 *
 * Core invariants (`CRITICAL.md`, `DECISIONS.md` R2-fw-1..6, R5-fw-4..5, R6-SEC-1..2):
 * 1. `downloading` is published once per transfer; chunk progress goes to serial console.
 * 2. Read-back SHA-256 and ECDSA P-256 verification run BEFORE `esp_https_ota_finish()`
 *    switches `otadata`. Flash encryption is refused at compile time (`#error`) and
 *    `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE` is required (`#error`).
 * 3. Download URLs are bearer credentials and are never logged or echoed in `detail`.
 * 4. `choose_target_slot()` selects the inactive slot once, refuses `PENDING_VERIFY` or an
 *    already-staged slot, and `restore_boot_partition()` rewrites `otadata` only if moved.
 * 5. `OTA_STALL_MS` bounds silent connections; `ff_ota_is_handling()` deduplicates redelivered
 *    `stage` commands; `s_cancel_lock` serializes cancel checkpoints & safe-window wait
 *    against `applying`.
 */

#define FF_OTA_INTERNAL_IMPL
#include "ff_ota_internal.h"

#include <inttypes.h>
#include <stdlib.h>
#include <string.h>

#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_https_ota.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "ff_mqtt_internal.h"
#include "ff_safe_window_internal.h"
#include "ff_txn.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#if CONFIG_SECURE_FLASH_ENC_ENABLED
#error "ff_ota verifies the slot BEFORE esp_https_ota_finish(); with flash encryption esp_ota_write() holds back a trailing partial block until esp_ota_end(), so that read-back would hash an incomplete image. See R2-fw-1."
#endif

/* R3-fw-5: the OTA path does not build without rollback. Every consumer's bootloader is
 * built from the same sdkconfig as its app (Arduino's core or hybrid rebuild, one IDF
 * sdkconfig), and this file is always compiled, so this is the one guard that covers every
 * consumer, including an IDF main that skips the example's fleetforge_start.c. It emits no
 * code; the runtime posture after an OTA is unchanged (ff_mqtt.c::classify_txn). */
#if !CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
#error "fleetforge's OTA path needs CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y: without it an image written by OTA that cannot reach the fleet never rolls back. Turn it back on in sdkconfig.defaults (ESP-IDF) or remove the custom_sdkconfig line that turns it off (Arduino/PlatformIO)."
#endif

static const char *TAG = "ff-ota";

/* TLS session + mbedtls sha256 + the OTA component's own buffers. Measured headroom is
 * comfortable at 8 KB; below ~6 KB the handshake overflows it, and a stack overflow on a
 * board mid-deploy is a reboot loop with nobody there to read the backtrace. */
#define OTA_TASK_STACK 8192
/* Above the heartbeat (4) so a download cannot starve it, below esp-mqtt's own task. */
#define OTA_TASK_PRIO 5

/* How long the image may stop growing before the download is abandoned (property 5).
 * Wall clock, not a count of empty reads, so it keeps its meaning if OTA_HTTP_TIMEOUT_MS
 * changes, and it also covers the wait for the first body byte. It is checked each time
 * perform() returns, which while stalled is every OTA_HTTP_TIMEOUT_MS, so the abort lands
 * 60-80 s after the last byte. 60 s, not less: on metal TCP keepalive (IDF 5 s idle, 5 s
 * interval, 3 probes) ends a socket whose radio is really gone in ~20 s, and it should stay
 * the first responder; this guard is for a far end that is alive and silent (R2-fw-5). */
_Static_assert(OTA_STALL_MS >= 2 * OTA_HTTP_TIMEOUT_MS,
               "one slow read must never trip the stall guard");

/* Long enough for two QoS-1 PUBLISHes (`applying`, `rebooting`) to leave the wire before
 * the reboot kills the socket. Without it the last thing the fleet sees is `staged`, and
 * the deploy looks stuck forever. */
#define OTA_PUBLISH_DRAIN_MS 1500

/* One update at a time, for the whole life of the process. Not a mutex: a second `stage`
 * must be REFUSED and reported, not queued behind the first — see ff_ota_start(). */
volatile bool s_running;

/* The cmd_id of the update `s_running` is about (R2-fw-6), for ff_ota_is_handling().
 * Single writer: ff_ota_start(), immediately BEFORE `s_running = true`. Never cleared and
 * never touched by ota_task, so it is meaningful only while `s_running` is true. */
static char s_running_cmd_id[sizeof(((ff_ota_cmd_t *)0)->cmd_id)];
_Static_assert(sizeof(s_running_cmd_id) == FF_TXN_MAX_CMD_ID,
               "s_running_cmd_id, ff_ota_cmd_t.cmd_id and the ff_txn record hold the same ids");

/* R5-fw-4. The cancel handshake (property 5). `s_phase` says where the update `s_running` is
 * about stands; `s_cancel` is a cancel accepted for it and not yet acted on. Both, and the
 * true -> false move of `s_running` at `done:`, change only under this lock. */
portMUX_TYPE s_cancel_lock = portMUX_INITIALIZER_UNLOCKED;
static volatile ff_ota_txn_t s_phase = FF_OTA_TXN_NONE;
static bool s_cancel;

bool cancel_requested(void)
{
    bool requested;
    portENTER_CRITICAL(&s_cancel_lock);
    requested = s_cancel;
    portEXIT_CRITICAL(&s_cancel_lock);
    return requested;
}

void report_cancelled(const ff_ota_cmd_t *cmd)
{
    ESP_LOGW(TAG, "update %s: cancelled by command", cmd->cmd_id);
    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_CANCELLED, FF_STATUS_PCT_NONE, NULL);
}

void fail(const ff_ota_cmd_t *cmd, const char *detail)
{
    ESP_LOGE(TAG, "update %s failed: %s", cmd->cmd_id, detail);
    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE, detail);
}

/* Point the boot partition at the running slot again, in case a FAILED
 * esp_https_ota_finish() left it anywhere else, or because a `cancel` arrived after finish()
 * moved it (R5-fw-4). It writes otadata ONLY if the pointer actually moved (R2-fw-2). */
bool restore_boot_partition(const char *why)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    if (running == NULL) {
        ESP_LOGE(TAG, "cannot read the running partition to undo the boot switch — this "
                      "board may reboot into an image that failed verification");
        return false;
    }
    const esp_partition_t *boot = esp_ota_get_boot_partition();
    if (boot == running) {
        ESP_LOGW(TAG, "boot partition still names %s; nothing to undo — otadata was not "
                      "touched",
                 running->label);
        return true;
    }
    if (boot == NULL) {
        ESP_LOGE(TAG, "cannot read the boot partition; putting it back to %s anyway",
                 running->label);
    }
    esp_err_t err = esp_ota_set_boot_partition(running);
    if (err == ESP_OK) {
        ESP_LOGE(TAG, "boot partition put back to %s: %s", running->label, why);
        return true;
    }
    ESP_LOGE(TAG, "CANNOT undo the boot switch (%s) — this board will reboot into the staged "
                  "image",
             esp_err_to_name(err));
    return false;
}

/* R5-fw-4. The staged-cancel sequence (R5-spec-1 D4), the order load-bearing: the boot pointer
 * back on the running image FIRST, the record cleared only if that worked. */
static bool cancel_staged(const char *cmd_id)
{
    if (!restore_boot_partition(WHY_CANCELLED)) {
        return false;
    }
    (void)ff_txn_clear_if(cmd_id);
    return true;
}

static const char *ota_state_name(esp_ota_img_states_t state)
{
    switch (state) {
    case ESP_OTA_IMG_NEW:            return "new";
    case ESP_OTA_IMG_PENDING_VERIFY: return "pending_verify";
    case ESP_OTA_IMG_VALID:          return "valid";
    case ESP_OTA_IMG_INVALID:        return "invalid";
    case ESP_OTA_IMG_ABORTED:        return "aborted";
    case ESP_OTA_IMG_UNDEFINED:      return "undefined";
    default:                         return "?";
    }
}

/* The ONE place the slot is chosen (property 4). Returns the slot to write, or NULL after
 * it has published `failed` itself. Everything here is a read: no otadata write, no erase,
 * no fetch, and no ff_txn call. */
const esp_partition_t *choose_target_slot(const ff_ota_cmd_t *cmd)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    if (running == NULL) {
        fail(cmd, "cannot read the running partition");
        return NULL;
    }

    esp_ota_img_states_t running_state;
    esp_err_t state_err = esp_ota_get_state_partition(running, &running_state);
    if (state_err != ESP_OK) {
        ESP_LOGI(TAG, "update %s: %s has no ota state (%s); a serially flashed image counts "
                      "as confirmed",
                 cmd->cmd_id, running->label, esp_err_to_name(state_err));
    } else if (running_state == ESP_OTA_IMG_PENDING_VERIFY) {
        ESP_LOGE(TAG, "update %s: refused — %s is still pending_verify (it confirms at its "
                      "announce ack or rolls back); nothing was fetched or erased",
                 cmd->cmd_id, running->label);
        fail(cmd, "the running image is not confirmed yet");
        return NULL;
    }

    const esp_partition_t *boot = esp_ota_get_boot_partition();
    if (boot == NULL) {
        fail(cmd, "cannot read the boot partition");
        return NULL;
    }
    if (boot != running) {
        esp_ota_img_states_t boot_state;
        const char *boot_state_name = esp_ota_get_state_partition(boot, &boot_state) == ESP_OK
                                          ? ota_state_name(boot_state)
                                          : "unknown";
        ESP_LOGE(TAG, "update %s: refused — the boot partition names %s (ota state %s) while "
                      "%s is running; nothing was fetched or erased; the staged image boots "
                      "at the next reset",
                 cmd->cmd_id, boot->label, boot_state_name, running->label);
        fail(cmd, "an update is already staged and waits for a reboot");
        return NULL;
    }

    const esp_partition_t *target = esp_ota_get_next_update_partition(NULL);
    if (target == NULL) {
        fail(cmd, "no spare ota slot");
        return NULL;
    }
    if (target == running || target->type != ESP_PARTITION_TYPE_APP ||
        target->subtype < ESP_PARTITION_SUBTYPE_APP_OTA_MIN ||
        target->subtype >= ESP_PARTITION_SUBTYPE_APP_OTA_MAX) {
        ESP_LOGE(TAG, "update %s: next update slot %s is the running slot or not an ota slot "
                      "(type %d, subtype 0x%02x)",
                 cmd->cmd_id, target->label, (int)target->type, (unsigned)target->subtype);
        fail(cmd, "no spare ota slot");
        return NULL;
    }
    return target;
}

static void ota_task(void *arg)
{
    ff_ota_cmd_t *cmd = (ff_ota_cmd_t *)arg;
    char *resolved = NULL;

    ESP_LOGI(TAG, "update %s: staging version %s (%u bytes, sha256 %s)", cmd->cmd_id,
             cmd->version[0] != '\0' ? cmd->version : "?", (unsigned)cmd->size, cmd->sha256);
    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_STAGING, 0, NULL);

    if (cmd->type[0] != '\0' && strcmp(cmd->type, "app") != 0) {
        run_cargo_ota(cmd);
        goto done;
    }

    const esp_partition_t *target = choose_target_slot(cmd);
    if (target == NULL) {
        goto done;
    }
    ESP_LOGI(TAG, "update %s: writing slot %s at 0x%08" PRIx32 " (%" PRIu32 " bytes)",
             cmd->cmd_id, target->label, target->address, target->size);
    if (cmd->size > target->size) {
        ESP_LOGE(TAG, "update %s: the artifact is %u bytes, slot %s holds %" PRIu32,
                 cmd->cmd_id, (unsigned)cmd->size, target->label, target->size);
        fail(cmd, "artifact larger than the ota slot");
        goto done;
    }

    resolved = malloc(OTA_RESOLVED_URL_MAX);
    if (resolved == NULL) {
        fail(cmd, "out of memory");
        goto done;
    }
    if (resolve_artifact_url(cmd->url, resolved) != ESP_OK) {
        fail(cmd, "cannot open the artifact");
        goto done;
    }
    if (cancel_requested()) {
        report_cancelled(cmd);
        goto done;
    }

    esp_http_client_config_t http = {
        .url = resolved,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = OTA_HTTP_TIMEOUT_MS,
        .keep_alive_enable = true,
        .buffer_size = OTA_HTTP_RX_BUFFER,
        .buffer_size_tx = OTA_HTTP_TX_BUFFER,
    };
    s_ota_http_client = NULL;
    s_resume_range_hdr[0] = '\0';
    esp_https_ota_config_t ota_config = {
        .http_config = &http,
        .http_client_init_cb = ota_http_client_init_cb,
        .partition = {.staging = target},
    };

    esp_https_ota_handle_t handle = NULL;
    esp_err_t err = esp_https_ota_begin(&ota_config, &handle);
    if (err != ESP_OK || handle == NULL) {
        fail(cmd, "cannot open the artifact");
        ESP_LOGE(TAG, "esp_https_ota_begin: %s", esp_err_to_name(err));
        goto done;
    }

    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_DOWNLOADING, 0, NULL);

    int last_logged_pct = -10;
    int last_len = 0;
    int64_t last_progress_us = esp_timer_get_time();
    bool stalled = false;
    bool cancel_seen = false;
    while ((err = esp_https_ota_perform(handle)) == ESP_ERR_HTTPS_OTA_IN_PROGRESS) {
        if (cancel_requested()) {
            cancel_seen = true;
            break;
        }
        int so_far = esp_https_ota_get_image_len_read(handle);
        int64_t now_us = esp_timer_get_time();
        if (so_far > last_len) {
            last_len = so_far;
            last_progress_us = now_us;
        } else if (now_us - last_progress_us >= (int64_t)OTA_STALL_MS * 1000) {
            stalled = true;
            break;
        }
        if (cmd->size == 0 || so_far < 0) {
            continue;
        }
        int pct = (int)(((uint64_t)so_far * 100) / (uint64_t)cmd->size);
        if (pct >= last_logged_pct + 10) {
            last_logged_pct = pct;
            ESP_LOGI(TAG, "update %s: %d%% (%d bytes)", cmd->cmd_id, pct, so_far);
        }
    }

    if (err != ESP_OK && !cancel_seen && !stalled) {
        err = resume_range_perform(handle, cmd, &last_len, &last_progress_us, &stalled,
                                   &cancel_seen);
    }

    if (cancel_seen) {
        esp_https_ota_abort(handle);
        report_cancelled(cmd);
        goto done;
    }
    if (stalled) {
        int stalled_s = (int)((esp_timer_get_time() - last_progress_us) / 1000000);
        esp_https_ota_abort(handle);
        ESP_LOGE(TAG, "update %s: no bytes for %d s at %d bytes — abandoning the download; "
                      "the boot partition was never moved",
                 cmd->cmd_id, stalled_s, last_len);
        fail(cmd, "download stalled");
        goto done;
    }
    if (err != ESP_OK) {
        esp_https_ota_abort(handle);
        fail(cmd, "download failed");
        ESP_LOGE(TAG, "esp_https_ota_perform: %s", esp_err_to_name(err));
        goto done;
    }
    if (!esp_https_ota_is_complete_data_received(handle)) {
        esp_https_ota_abort(handle);
        fail(cmd, "truncated download");
        goto done;
    }

    int read_total = esp_https_ota_get_image_len_read(handle);
    if (read_total < 0) {
        esp_https_ota_abort(handle);
        fail(cmd, "cannot size the downloaded image");
        goto done;
    }
    size_t got = (size_t)read_total;
    if (cmd->size != 0 && got != cmd->size) {
        esp_https_ota_abort(handle);
        ESP_LOGE(TAG, "update %s: got %u bytes, the command says %u", cmd->cmd_id, (unsigned)got,
                 (unsigned)cmd->size);
        fail(cmd, "size mismatch");
        goto done;
    }

    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_VERIFYING, FF_STATUS_PCT_NONE, NULL);

    char digest[65];
    uint8_t digest_raw[32];
    err = partition_digest(target, got, digest, digest_raw);
    if (err != ESP_OK) {
        esp_https_ota_abort(handle);
        ESP_LOGE(TAG, "cannot read %s back: %s", target->label, esp_err_to_name(err));
        fail(cmd, "cannot verify the staged image");
        goto done;
    }
    if (strcmp(digest, cmd->sha256) != 0) {
        esp_https_ota_abort(handle);
        ESP_LOGE(TAG, "update %s: sha256 MISMATCH, flash holds %s, the command says %s — "
                      "the boot partition was never moved",
                 cmd->cmd_id, digest, cmd->sha256);
        fail(cmd, "sha256 mismatch");
        goto done;
    }
    const char *sig_detail = "signature invalid";
    if (!verify_artifact_signature(digest_raw, cmd->sig, &sig_detail)) {
        esp_https_ota_abort(handle);
        ESP_LOGE(TAG, "update %s: %s — the boot partition was never moved", cmd->cmd_id,
                 sig_detail);
        fail(cmd, sig_detail);
        goto done;
    }
    if (cancel_requested()) {
        esp_https_ota_abort(handle);
        report_cancelled(cmd);
        goto done;
    }
    ESP_LOGI(TAG, "update %s: sha256 %s matches what is on flash in %s; switching the boot "
                  "partition",
             cmd->cmd_id, digest, target->label);

    err = esp_https_ota_finish(handle);
    if (err != ESP_OK) {
        if (err == ESP_ERR_OTA_VALIDATE_FAILED) {
            fail(cmd, "image validation failed");
        } else {
            fail(cmd, "cannot finish the update");
            ESP_LOGE(TAG, "esp_https_ota_finish: %s", esp_err_to_name(err));
        }
        (void)restore_boot_partition(WHY_REJECTED);
        goto done;
    }

    bool cancel_now;
    portENTER_CRITICAL(&s_cancel_lock);
    cancel_now = s_cancel;
    if (!cancel_now) {
        s_phase = cmd->apply_now ? FF_OTA_TXN_AWAITING : FF_OTA_TXN_STAGED;
    }
    portEXIT_CRITICAL(&s_cancel_lock);
    if (cancel_now) {
        if (restore_boot_partition(WHY_CANCELLED)) {
            report_cancelled(cmd);
            goto done;
        }
        ESP_LOGE(TAG, "update %s: the cancel cannot be carried out — the boot partition stays "
                      "on %s, so the update goes on and its outcome is reported",
                 cmd->cmd_id, target->label);
        portENTER_CRITICAL(&s_cancel_lock);
        s_cancel = false;
        s_phase = cmd->apply_now ? FF_OTA_TXN_AWAITING : FF_OTA_TXN_STAGED;
        portEXIT_CRITICAL(&s_cancel_lock);
    }

    esp_err_t txn_err = ff_txn_save(cmd->cmd_id, target->address, cmd->confirm_timeout_s);
    if (txn_err != ESP_OK) {
        ESP_LOGE(TAG, "update %s: cannot record the transaction (%s) — the outcome will not "
                      "be reported after the reboot",
                 cmd->cmd_id, esp_err_to_name(txn_err));
    }

    ESP_LOGI(TAG, "update %s: %s is staged and bootable", cmd->cmd_id, target->label);

    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_STAGED, 100, NULL);

    if (!cmd->apply_now) {
        ESP_LOGW(TAG, "update %s: apply=on_command — staged and waiting for the apply "
                      "command (a further stage is refused until this board reboots or the "
                      "update is cancelled)",
                 cmd->cmd_id);
        goto done;
    }

    if (!cancel_requested() && !ff_safe_window_is_safe()) {
        ESP_LOGW(TAG, "update %s: %s is staged; the reboot into it waits for a safe window",
                 cmd->cmd_id, target->label);
        ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_AWAITING_SAFE_WINDOW, 100,
                               FF_DETAIL_APPLY_WAITS);
        while (!cancel_requested() && !ff_safe_window_is_safe()) {
            vTaskDelay(pdMS_TO_TICKS(FF_SAFE_WINDOW_POLL_MS));
        }
    }
    bool cancel_apply;
    portENTER_CRITICAL(&s_cancel_lock);
    cancel_apply = s_cancel;
    if (!cancel_apply) {
        s_phase = FF_OTA_TXN_APPLYING;
    }
    portEXIT_CRITICAL(&s_cancel_lock);
    if (cancel_apply) {
        goto done;
    }

    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_APPLYING, 100, NULL);
    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_REBOOTING, 100, NULL);
    ESP_LOGW(TAG, "update %s: rebooting into %s. The bootloader will run it as "
                  "PENDING_VERIFY; ff_mqtt confirms it only once the retained announce is "
                  "acknowledged, and rolls back otherwise.",
             cmd->cmd_id, target->label);
    (void)ff_txn_note_reboot_cmd(cmd->cmd_id);
    vTaskDelay(pdMS_TO_TICKS(OTA_PUBLISH_DRAIN_MS));
    while (!ff_safe_window_is_safe()) {
        vTaskDelay(pdMS_TO_TICKS(FF_SAFE_WINDOW_POLL_MS));
    }
    esp_restart(); /* does not return */

done:
    for (;;) {
        bool cancel_staged_now;
        portENTER_CRITICAL(&s_cancel_lock);
        cancel_staged_now =
            s_cancel && (s_phase == FF_OTA_TXN_STAGED || s_phase == FF_OTA_TXN_AWAITING);
        s_cancel = false;
        if (!cancel_staged_now) {
            s_phase = FF_OTA_TXN_NONE;
            s_running = false;
        }
        portEXIT_CRITICAL(&s_cancel_lock);
        if (!cancel_staged_now) {
            break;
        }
        if (cancel_staged(cmd->cmd_id)) {
            report_cancelled(cmd);
        } else {
            ESP_LOGE(TAG, "update %s: the cancel cannot be carried out — the staged image "
                          "still boots at the next reset",
                     cmd->cmd_id);
        }
        portENTER_CRITICAL(&s_cancel_lock);
        s_phase = FF_OTA_TXN_NONE;
        portEXIT_CRITICAL(&s_cancel_lock);
    }
    s_ota_http_client = NULL;
    s_resume_range_hdr[0] = '\0';
    free(resolved);
    free(cmd);
    vTaskDelete(NULL);
}

esp_err_t ff_ota_start(const ff_ota_cmd_t *cmd)
{
    if (cmd == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_running) {
        return ESP_ERR_INVALID_STATE;
    }

    ff_ota_cmd_t *copy = malloc(sizeof(*copy));
    if (copy == NULL) {
        return ESP_ERR_NO_MEM;
    }
    memcpy(copy, cmd, sizeof(*copy));

    strlcpy(s_running_cmd_id, cmd->cmd_id, sizeof(s_running_cmd_id));
    portENTER_CRITICAL(&s_cancel_lock);
    s_cancel = false;
    s_phase = FF_OTA_TXN_TRANSFER;
    s_running = true;
    portEXIT_CRITICAL(&s_cancel_lock);
    if (xTaskCreate(ota_task, "ff_ota", OTA_TASK_STACK, copy, OTA_TASK_PRIO, NULL) != pdPASS) {
        portENTER_CRITICAL(&s_cancel_lock);
        s_phase = FF_OTA_TXN_NONE;
        s_running = false;
        portEXIT_CRITICAL(&s_cancel_lock);
        free(copy);
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

bool ff_ota_is_handling(const char *cmd_id)
{
    if (cmd_id == NULL || cmd_id[0] == '\0') {
        return false;
    }
    if (s_running) {
        return strcmp(cmd_id, s_running_cmd_id) == 0;
    }
    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *boot = esp_ota_get_boot_partition();
    if (running == NULL || boot == NULL || boot == running) {
        return false;
    }
    ff_txn_t rec;
    if (ff_txn_load(&rec) != ESP_OK) {
        return false;
    }
    return strcmp(rec.cmd_id, cmd_id) == 0;
}

static ff_ota_txn_t staged_phase(const char *cmd_id, char *held, size_t held_len)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *boot = esp_ota_get_boot_partition();
    if (running == NULL || boot == NULL || boot == running) {
        return FF_OTA_TXN_NONE;
    }
    ff_txn_t rec;
    const bool recorded = ff_txn_load(&rec) == ESP_OK && rec.target_addr == boot->address;
    if (cmd_id == NULL) {
        if (recorded && held != NULL) {
            strlcpy(held, rec.cmd_id, held_len);
        }
        return FF_OTA_TXN_STAGED;
    }
    if (!recorded || strcmp(rec.cmd_id, cmd_id) != 0) {
        return FF_OTA_TXN_NONE;
    }
    if (held != NULL) {
        strlcpy(held, rec.cmd_id, held_len);
    }
    return FF_OTA_TXN_STAGED;
}

ff_ota_txn_t ff_ota_txn_phase(const char *cmd_id, char *held, size_t held_len)
{
    if (held != NULL && held_len > 0) {
        held[0] = '\0';
    } else {
        held = NULL;
    }
    bool running;
    bool match;
    bool cancel;
    ff_ota_txn_t phase;
    portENTER_CRITICAL(&s_cancel_lock);
    running = s_running;
    phase = s_phase;
    cancel = s_cancel;
    match = running && cmd_id != NULL && strcmp(cmd_id, s_running_cmd_id) == 0;
    if (running && held != NULL && (cmd_id == NULL || match)) {
        strlcpy(held, s_running_cmd_id, held_len);
    }
    portEXIT_CRITICAL(&s_cancel_lock);

    if (running && cmd_id == NULL) {
        return phase != FF_OTA_TXN_NONE ? phase : FF_OTA_TXN_TRANSFER;
    }
    if (match) {
        if (cancel || phase == FF_OTA_TXN_NONE) {
            if (held != NULL) {
                held[0] = '\0';
            }
            return FF_OTA_TXN_NONE;
        }
        return phase;
    }
    return staged_phase(cmd_id, held, held != NULL ? held_len : 0);
}

esp_err_t ff_ota_cancel(const char *cmd_id, bool *reported)
{
    if (cmd_id == NULL || cmd_id[0] == '\0' || reported == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *reported = false;

    bool in_flight = false;
    esp_err_t rc = ESP_ERR_NOT_FOUND;
    portENTER_CRITICAL(&s_cancel_lock);
    if (s_running && strcmp(cmd_id, s_running_cmd_id) == 0) {
        in_flight = true;
        if (s_phase == FF_OTA_TXN_APPLYING) {
            rc = ESP_ERR_INVALID_STATE;
        } else if (s_phase == FF_OTA_TXN_NONE) {
            rc = ESP_ERR_NOT_FOUND;
        } else {
            s_cancel = true;
            *reported = true;
            rc = ESP_OK;
        }
    }
    portEXIT_CRITICAL(&s_cancel_lock);
    if (in_flight) {
        return rc;
    }

    if (staged_phase(cmd_id, NULL, 0) != FF_OTA_TXN_STAGED) {
        return ESP_ERR_NOT_FOUND;
    }
    if (!cancel_staged(cmd_id)) {
        return ESP_FAIL;
    }
    ESP_LOGW(TAG, "update %s: cancelled by command (the staged image will not boot)", cmd_id);
    return ESP_OK;
}

bool ff_ota_in_flight(void)
{
    portENTER_CRITICAL(&s_cancel_lock);
    const bool in_flight = s_running;
    portEXIT_CRITICAL(&s_cancel_lock);
    return in_flight;
}
