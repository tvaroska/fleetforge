/*
 * ff_ota_internal.h — private state and helpers shared across the ff_ota translation units
 * (`ff_ota.c`, `ff_ota_verify.c`, `ff_ota_cargo.c`).
 *
 * Included ONLY by `ff_ota*.c` with `FF_OTA_INTERNAL_IMPL` defined before inclusion so short
 * local identifiers inside function bodies map cleanly onto `ff_ota__*` component symbols.
 */

#pragma once

#include "ff_ota.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "esp_http_client.h"
#include "esp_https_ota.h"
#include "esp_partition.h"
#include "freertos/FreeRTOS.h"

#define OTA_HTTP_TIMEOUT_MS 20000
#define OTA_STALL_MS 60000
#define OTA_HTTP_RX_BUFFER 4096
#define OTA_HTTP_TX_BUFFER 2048
#define OTA_READBACK_CHUNK 4096
#define OTA_MAX_RETRIES 3
#define OTA_RETRY_BASE_MS 1000
#define OTA_RESOLVED_URL_MAX 1024

#define WHY_REJECTED "the staged image was rejected and will NOT be booted"
#define WHY_CANCELLED "the update was cancelled by command and will NOT be booted"

#ifdef FF_OTA_INTERNAL_IMPL
#define s_running ff_ota__running
#define s_cancel_lock ff_ota__cancel_lock
#define s_ota_http_client ff_ota__http_client
#define s_resume_range_hdr ff_ota__resume_range_hdr

#define cancel_requested ff_ota__cancel_requested
#define report_cancelled ff_ota__report_cancelled
#define fail ff_ota__fail
#define restore_boot_partition ff_ota__restore_boot_partition
#define choose_target_slot ff_ota__choose_target_slot
#define hex_encode ff_ota__hex_encode
#define partition_digest ff_ota__partition_digest
#define verify_artifact_signature ff_ota__verify_artifact_signature
#define ota_http_client_init_cb ff_ota__http_client_init_cb
#define resume_range_perform ff_ota__resume_range_perform
#define resolve_artifact_url ff_ota__resolve_artifact_url
#define run_cargo_ota ff_ota__run_cargo_ota

extern volatile bool s_running;
extern portMUX_TYPE s_cancel_lock;
extern esp_http_client_handle_t s_ota_http_client;
extern char s_resume_range_hdr[48];

bool cancel_requested(void);
void report_cancelled(const ff_ota_cmd_t *cmd);
void fail(const ff_ota_cmd_t *cmd, const char *detail);
bool restore_boot_partition(const char *why);
const esp_partition_t *choose_target_slot(const ff_ota_cmd_t *cmd);

void hex_encode(const uint8_t *digest, size_t len, char *out);
esp_err_t partition_digest(const esp_partition_t *part, size_t length, char out[65],
                           uint8_t raw_out[32]);
bool verify_artifact_signature(const uint8_t digest_raw[32], const char *sig_str,
                               const char **detail_out);
esp_err_t ota_http_client_init_cb(esp_http_client_handle_t client);
esp_err_t resume_range_perform(esp_https_ota_handle_t handle, const ff_ota_cmd_t *cmd,
                               int *last_len, int64_t *last_progress_us,
                               bool *stalled, bool *cancel_seen);
esp_err_t resolve_artifact_url(const char *url, char *out);
void run_cargo_ota(const ff_ota_cmd_t *cmd);
#endif
