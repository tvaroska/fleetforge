/*
 * ff_ota_cargo — pluggable non-app companion cargo handlers (`R7-cargo-1`) and in-memory
 * USB-C dock staging (`CUJ-3`).
 */

#define FF_OTA_INTERNAL_IMPL
#include "ff_ota_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "ff_mqtt_internal.h"
#include "ff_txn.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/sha256.h"

#define OTA_MAX_CARGO_HANDLERS 8
typedef struct {
    char type[32];
    ff_cargo_write_fn_t write_cb;
    ff_cargo_finish_fn_t finish_cb;
    void *ctx;
} ff_cargo_handler_t;

static ff_cargo_handler_t s_cargo_handlers[OTA_MAX_CARGO_HANDLERS];
static size_t s_cargo_handler_count;

esp_err_t ff_ota_register_cargo_handler(const char *type, ff_cargo_write_fn_t write_cb,
                                        ff_cargo_finish_fn_t finish_cb, void *ctx)
{
    if (type == NULL || type[0] == '\0' || strcmp(type, "app") == 0 || write_cb == NULL ||
        finish_cb == NULL || strlen(type) >= sizeof(s_cargo_handlers[0].type)) {
        return ESP_ERR_INVALID_ARG;
    }
    for (size_t i = 0; i < s_cargo_handler_count; i++) {
        if (strcmp(s_cargo_handlers[i].type, type) == 0) {
            s_cargo_handlers[i].write_cb = write_cb;
            s_cargo_handlers[i].finish_cb = finish_cb;
            s_cargo_handlers[i].ctx = ctx;
            return ff_mqtt_register_cargo_type(type);
        }
    }
    if (s_cargo_handler_count >= OTA_MAX_CARGO_HANDLERS) {
        return ESP_ERR_NO_MEM;
    }
    strlcpy(s_cargo_handlers[s_cargo_handler_count].type, type,
            sizeof(s_cargo_handlers[0].type));
    s_cargo_handlers[s_cargo_handler_count].write_cb = write_cb;
    s_cargo_handlers[s_cargo_handler_count].finish_cb = finish_cb;
    s_cargo_handlers[s_cargo_handler_count].ctx = ctx;
    s_cargo_handler_count++;
    return ff_mqtt_register_cargo_type(type);
}

static const ff_cargo_handler_t *find_cargo_handler(const char *type)
{
    for (size_t i = 0; i < s_cargo_handler_count; i++) {
        if (strcmp(s_cargo_handlers[i].type, type) == 0) {
            return &s_cargo_handlers[i];
        }
    }
    return NULL;
}

void run_cargo_ota(const ff_ota_cmd_t *cmd)
{
    const ff_cargo_handler_t *handler = find_cargo_handler(cmd->type);
    if (handler == NULL) {
        fail(cmd, "unsupported artifact type");
        return;
    }
    char *resolved = malloc(OTA_RESOLVED_URL_MAX);
    uint8_t *buf = malloc(OTA_READBACK_CHUNK);
    if (resolved == NULL || buf == NULL) {
        free(resolved);
        free(buf);
        fail(cmd, "out of memory");
        return;
    }
    if (resolve_artifact_url(cmd->url, resolved) != ESP_OK) {
        free(resolved);
        free(buf);
        fail(cmd, "cannot open the artifact");
        return;
    }
    if (cancel_requested()) {
        free(resolved);
        free(buf);
        report_cancelled(cmd);
        return;
    }

    esp_http_client_config_t http = {
        .url = resolved,
        .method = HTTP_METHOD_GET,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = OTA_HTTP_TIMEOUT_MS,
        .keep_alive_enable = true,
        .buffer_size = OTA_HTTP_RX_BUFFER,
        .buffer_size_tx = OTA_HTTP_TX_BUFFER,
    };
    esp_http_client_handle_t client = esp_http_client_init(&http);
    if (client == NULL || esp_http_client_open(client, 0) != ESP_OK ||
        esp_http_client_fetch_headers(client) < 0 ||
        esp_http_client_get_status_code(client) != 200) {
        if (client != NULL) {
            esp_http_client_close(client);
            esp_http_client_cleanup(client);
        }
        free(resolved);
        free(buf);
        fail(cmd, "cannot open the artifact");
        return;
    }

    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_DOWNLOADING, 0, NULL);

    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    (void)mbedtls_sha256_starts(&sha, 0);

    size_t total = 0;
    bool download_failed = false;
    bool cancelled = false;
    int retry_attempt = 0;
    for (;;) {
        if (cancel_requested()) {
            cancelled = true;
            break;
        }
        int r = esp_http_client_read(client, (char *)buf, OTA_READBACK_CHUNK);
        if (r == 0 && esp_http_client_is_complete_data_received(client)) {
            break;
        }
        if (r <= 0) {
            if (retry_attempt >= OTA_MAX_RETRIES) {
                download_failed = true;
                break;
            }
            uint32_t delay_ms = (uint32_t)OTA_RETRY_BASE_MS << retry_attempt;
            retry_attempt++;
            char range_hdr[48];
            snprintf(range_hdr, sizeof(range_hdr), "bytes=%u-", (unsigned)total);
            esp_http_client_close(client);
            vTaskDelay(pdMS_TO_TICKS(delay_ms));
            if (cancel_requested()) {
                cancelled = true;
                break;
            }
            (void)esp_http_client_set_header(client, "Range", range_hdr);
            if (esp_http_client_open(client, 0) != ESP_OK ||
                esp_http_client_fetch_headers(client) < 0) {
                esp_http_client_close(client);
                continue;
            }
            int st = esp_http_client_get_status_code(client);
            if (st != 206 && !(total == 0 && st == 200)) {
                esp_http_client_close(client);
                if (st >= 400 && st < 500 && st != 408 && st != 429) {
                    download_failed = true;
                    break;
                }
                continue;
            }
            continue;
        }
        retry_attempt = 0;
        if (mbedtls_sha256_update(&sha, buf, (size_t)r) != 0 ||
            handler->write_cb(total, buf, (size_t)r, handler->ctx) != ESP_OK) {
            download_failed = true;
            break;
        }
        total += (size_t)r;
    }

    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    free(resolved);
    free(buf);

    uint8_t digest_raw[32];
    (void)mbedtls_sha256_finish(&sha, digest_raw);
    mbedtls_sha256_free(&sha);

    if (cancelled) {
        report_cancelled(cmd);
        return;
    }
    if (download_failed) {
        fail(cmd, "download failed");
        return;
    }
    if (cmd->size != 0 && total != cmd->size) {
        fail(cmd, "size mismatch");
        return;
    }

    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_VERIFYING, FF_STATUS_PCT_NONE, NULL);
    char digest_hex[65];
    hex_encode(digest_raw, sizeof(digest_raw), digest_hex);
    if (strcmp(digest_hex, cmd->sha256) != 0) {
        fail(cmd, "sha256 mismatch");
        return;
    }
    const char *sig_detail = "signature invalid";
    if (!verify_artifact_signature(digest_raw, cmd->sig, &sig_detail)) {
        fail(cmd, sig_detail);
        return;
    }
    if (cancel_requested()) {
        report_cancelled(cmd);
        return;
    }

    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_STAGED, 100, NULL);
    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_APPLYING, 100, NULL);
    if (handler->finish_cb(total, digest_hex, cmd->version, handler->ctx) != ESP_OK) {
        fail(cmd, "cargo apply failed");
        return;
    }
    ff_mqtt_complete_cargo(cmd->cmd_id, cmd->type, cmd->version);
}

esp_err_t ff_ota_dock_stage(const ff_ota_cmd_t *cmd, const uint8_t *data, size_t len)
{
    if (cmd == NULL || data == NULL || len == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    bool busy;
    portENTER_CRITICAL(&s_cancel_lock);
    busy = s_running;
    if (!busy) {
        s_running = true;
    }
    portEXIT_CRITICAL(&s_cancel_lock);
    if (busy) {
        return ESP_ERR_INVALID_STATE;
    }

    esp_err_t rc = ESP_OK;
    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_STAGING, 0, NULL);
    if (cmd->size != 0 && len != cmd->size) {
        fail(cmd, "size mismatch"); rc = ESP_ERR_INVALID_SIZE; goto done;
    }
    if (cmd->type[0] != '\0' && strcmp(cmd->type, "app") != 0) {
        const ff_cargo_handler_t *handler = find_cargo_handler(cmd->type);
        if (handler == NULL) {
            fail(cmd, "unsupported artifact type"); rc = ESP_ERR_NOT_SUPPORTED; goto done;
        }
        ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_DOWNLOADING, 0, NULL);
        uint8_t digest_raw[32];
        mbedtls_sha256_context sha;
        mbedtls_sha256_init(&sha);
        (void)mbedtls_sha256_starts(&sha, 0);
        (void)mbedtls_sha256_update(&sha, data, len);
        (void)mbedtls_sha256_finish(&sha, digest_raw);
        mbedtls_sha256_free(&sha);
        ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_VERIFYING, FF_STATUS_PCT_NONE, NULL);
        char digest_hex[65];
        hex_encode(digest_raw, sizeof(digest_raw), digest_hex);
        if (strcmp(digest_hex, cmd->sha256) != 0) {
            fail(cmd, "sha256 mismatch"); rc = ESP_ERR_INVALID_CRC; goto done;
        }
        const char *sig_detail = "signature invalid";
        if (!verify_artifact_signature(digest_raw, cmd->sig, &sig_detail)) {
            fail(cmd, sig_detail); rc = ESP_ERR_INVALID_STATE; goto done;
        }
        if (handler->write_cb(0, data, len, handler->ctx) != ESP_OK) {
            fail(cmd, "download failed"); rc = ESP_FAIL; goto done;
        }
        ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_STAGED, 100, NULL);
        ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_APPLYING, 100, NULL);
        if (handler->finish_cb(len, digest_hex, cmd->version, handler->ctx) != ESP_OK) {
            fail(cmd, "cargo apply failed"); rc = ESP_FAIL; goto done;
        }
        ff_mqtt_complete_cargo(cmd->cmd_id, cmd->type, cmd->version);
        goto done;
    }

    const esp_partition_t *target = choose_target_slot(cmd);
    if (target == NULL) {
        rc = ESP_ERR_INVALID_STATE; goto done;
    }
    if (len > target->size) {
        fail(cmd, "artifact larger than the ota slot"); rc = ESP_ERR_INVALID_SIZE; goto done;
    }
    esp_ota_handle_t ota_handle = 0;
    if (esp_ota_begin(target, len, &ota_handle) != ESP_OK) {
        fail(cmd, "cannot open the artifact"); rc = ESP_FAIL; goto done;
    }
    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_DOWNLOADING, 0, NULL);
    if (esp_ota_write(ota_handle, data, len) != ESP_OK) {
        (void)esp_ota_abort(ota_handle);
        fail(cmd, "download failed"); rc = ESP_FAIL; goto done;
    }
    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_VERIFYING, FF_STATUS_PCT_NONE, NULL);
    char digest[65];
    uint8_t digest_raw[32];
    if (partition_digest(target, len, digest, digest_raw) != ESP_OK) {
        (void)esp_ota_abort(ota_handle);
        fail(cmd, "cannot verify the staged image"); rc = ESP_FAIL; goto done;
    }
    if (strcmp(digest, cmd->sha256) != 0) {
        (void)esp_ota_abort(ota_handle);
        fail(cmd, "sha256 mismatch"); rc = ESP_ERR_INVALID_CRC; goto done;
    }
    const char *sig_detail = "signature invalid";
    if (!verify_artifact_signature(digest_raw, cmd->sig, &sig_detail)) {
        (void)esp_ota_abort(ota_handle);
        fail(cmd, sig_detail); rc = ESP_ERR_INVALID_STATE; goto done;
    }
    if (esp_ota_end(ota_handle) != ESP_OK ||
        ff_txn_save(cmd->cmd_id, target->address, cmd->confirm_timeout_s) != ESP_OK) {
        fail(cmd, "cannot finish the update");
        rc = ESP_FAIL; goto done;
    }
    if (esp_ota_set_boot_partition(target) != ESP_OK) {
        (void)ff_txn_clear_if(cmd->cmd_id);
        fail(cmd, "cannot finish the update");
        (void)restore_boot_partition(WHY_REJECTED);
        rc = ESP_FAIL; goto done;
    }
    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_STAGED, 100, NULL);
done:
    portENTER_CRITICAL(&s_cancel_lock);
    s_running = false;
    portEXIT_CRITICAL(&s_cancel_lock);
    return rc;
}
