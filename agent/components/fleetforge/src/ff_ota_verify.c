/*
 * ff_ota_verify — read-back SHA-256, ECDSA P-256 key-ring verification, HTTP redirect
 * resolution, and HTTP Range mid-stream resume (`R2-fw-1`, `R6-FW-1`, `R6-SEC-1..2`).
 */

#define FF_OTA_INTERNAL_IMPL
#include "ff_ota_internal.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_https_ota.h"
#include "esp_log.h"
#include "esp_partition.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/base64.h"
#include "mbedtls/md.h"
#include "mbedtls/pk.h"
#include "mbedtls/sha256.h"
#include "sdkconfig.h"

static const char *TAG = "ff-ota";

#define FF_OTA_DEV_PRIMARY_PUBKEY_PEM \
    "-----BEGIN PUBLIC KEY-----\n" \
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEbHg1tVC28a9y8SbZY0jis7OKKWxr\n" \
    "X2+S5g0No7NMyV8nI+vUHHQ8MNQnNSDSPESUAT9m2iLyNC/nM26RvxYV1Q==\n" \
    "-----END PUBLIC KEY-----\n"

#define FF_OTA_DEV_SECONDARY_PUBKEY_PEM \
    "-----BEGIN PUBLIC KEY-----\n" \
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEwJct9NbOiOIbnB/ToYTvq02EBrzs\n" \
    "UAETxsFAAWHFWhxfpL3x0OcIH6yW9q6EhYA3q6vNGIEwC7KPJQXd1ONEqQ==\n" \
    "-----END PUBLIC KEY-----\n"

#if defined(CONFIG_FF_OTA_PRODUCTION_BUILD) && CONFIG_FF_OTA_PRODUCTION_BUILD && !defined(FF_OTA_PRODUCTION_BUILD)
#define FF_OTA_PRODUCTION_BUILD 1
#endif

#if defined(FF_OTA_PRODUCTION_BUILD) && !defined(FF_OTA_EMBEDDED_PUBKEYS) && \
    !defined(FF_OTA_KCONFIG_PUBKEYS_VALID) && \
    (!defined(FF_OTA_SIGNING_PUBKEY_PRIMARY_PEM) || \
     !defined(FF_OTA_SIGNING_PUBKEY_SECONDARY_PEM))
#error "FF_OTA_PRODUCTION_BUILD requires overriding both primary and secondary signing public keys (via EMBED_TXTFILES, CMake-validated Kconfig PEMs, or FF_OTA_SIGNING_PUBKEY_{PRIMARY,SECONDARY}_PEM)"
#endif

#if defined(FF_OTA_PRODUCTION_BUILD) && !defined(FF_OTA_EMBEDDED_PUBKEYS) && \
    !defined(FF_OTA_KCONFIG_PUBKEYS_VALID)
_Static_assert(sizeof(FF_OTA_SIGNING_PUBKEY_PRIMARY_PEM) > 1 &&
                   sizeof(FF_OTA_SIGNING_PUBKEY_SECONDARY_PEM) > 1,
               "FF_OTA_PRODUCTION_BUILD requires non-empty primary and secondary PEM strings");
#endif

#ifndef FF_OTA_REQUIRE_SIGNATURE
#if defined(FF_OTA_PRODUCTION_BUILD)
#define FF_OTA_REQUIRE_SIGNATURE 1
#elif defined(CONFIG_FF_OTA_REQUIRE_SIGNATURE)
#define FF_OTA_REQUIRE_SIGNATURE CONFIG_FF_OTA_REQUIRE_SIGNATURE
#else
#define FF_OTA_REQUIRE_SIGNATURE 1
#endif
#endif

#if defined(FF_OTA_EMBEDDED_PUBKEYS)
extern const char _binary_ff_ota_pubkey_primary_pem_start[];
extern const char _binary_ff_ota_pubkey_secondary_pem_start[];
#endif

#ifndef FF_OTA_SIGNING_PUBKEY_PRIMARY_PEM
#define FF_OTA_SIGNING_PUBKEY_PRIMARY_PEM FF_OTA_DEV_PRIMARY_PUBKEY_PEM
#endif

#ifndef FF_OTA_SIGNING_PUBKEY_SECONDARY_PEM
#define FF_OTA_SIGNING_PUBKEY_SECONDARY_PEM FF_OTA_DEV_SECONDARY_PUBKEY_PEM
#endif

static const char *const s_signing_pubkey_ring[] = {
    FF_OTA_SIGNING_PUBKEY_PRIMARY_PEM,
    FF_OTA_SIGNING_PUBKEY_SECONDARY_PEM,
};

static const char *signing_pubkey_slot(size_t idx)
{
#if defined(FF_OTA_EMBEDDED_PUBKEYS)
    if (idx < 2) {
        return idx == 0 ? _binary_ff_ota_pubkey_primary_pem_start
                        : _binary_ff_ota_pubkey_secondary_pem_start;
    }
#endif
#if defined(CONFIG_FF_OTA_SIGNING_PUBKEY_PRIMARY_PEM)
    if (idx == 0 && CONFIG_FF_OTA_SIGNING_PUBKEY_PRIMARY_PEM[0] != '\0') {
        return CONFIG_FF_OTA_SIGNING_PUBKEY_PRIMARY_PEM;
    }
#endif
#if defined(CONFIG_FF_OTA_SIGNING_PUBKEY_SECONDARY_PEM)
    if (idx == 1 && CONFIG_FF_OTA_SIGNING_PUBKEY_SECONDARY_PEM[0] != '\0') {
        return CONFIG_FF_OTA_SIGNING_PUBKEY_SECONDARY_PEM;
    }
#endif
    return s_signing_pubkey_ring[idx];
}

bool ff_ota_requires_signature(void) { return FF_OTA_REQUIRE_SIGNATURE != 0; }

bool ff_ota_uses_dev_signing_key(void)
{
    for (size_t i = 0; i < sizeof(s_signing_pubkey_ring) / sizeof(s_signing_pubkey_ring[0]); i++) {
        const char *pem = signing_pubkey_slot(i);
        if (pem == NULL || pem[0] == '\0' || strstr(pem, "-----BEGIN PUBLIC KEY-----") == NULL ||
            strstr(pem, "bHg1tVC28a9y8SbZY0jis7OKKWxr") != NULL ||
            strstr(pem, "wJct9NbOiOIbnB/ToYTvq02EBrzs") != NULL) {
            return true;
        }
    }
    return false;
}

/* The URL the artifact link redirects to. Twice `ff_ota_cmd_t.url` because a presigned S3
 * link is the long one: host + key + ~7 X-Amz-* query parameters. Heap, not stack — the OTA
 * task's 8 KB is already sized for the TLS handshake. */

void hex_encode(const uint8_t *digest, size_t len, char *out)
{
    static const char HEX[] = "0123456789abcdef";
    for (size_t i = 0; i < len; i++) {
        out[i * 2] = HEX[digest[i] >> 4];
        out[i * 2 + 1] = HEX[digest[i] & 0x0f];
    }
    out[len * 2] = '\0';
}

esp_err_t partition_digest(const esp_partition_t *part, size_t length, char out[65],
                           uint8_t raw_out[32])
{
    uint8_t *buf = malloc(OTA_READBACK_CHUNK);
    if (buf == NULL) {
        return ESP_ERR_NO_MEM;
    }

    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    esp_err_t err = ESP_OK;
    if (mbedtls_sha256_starts(&sha, 0) != 0) {
        err = ESP_FAIL;
    }

    for (size_t offset = 0; err == ESP_OK && offset < length;) {
        size_t chunk = length - offset;
        if (chunk > OTA_READBACK_CHUNK) {
            chunk = OTA_READBACK_CHUNK;
        }
        err = esp_partition_read(part, offset, buf, chunk);
        if (err == ESP_OK && mbedtls_sha256_update(&sha, buf, chunk) != 0) {
            err = ESP_FAIL;
        }
        offset += chunk;
    }

    uint8_t digest[32];
    if (err == ESP_OK && mbedtls_sha256_finish(&sha, digest) != 0) {
        err = ESP_FAIL;
    }
    if (err == ESP_OK) {
        memcpy(raw_out, digest, sizeof(digest));
        hex_encode(digest, sizeof(digest), out);
    }

    mbedtls_sha256_free(&sha);
    free(buf);
    return err;
}

/* R6-SEC-1 / R6-SEC-2: Verify `sig_str` (`ecdsa-p256-sha256:<base64_der>`) against the 32-byte
 * raw SHA-256 digest of the staged slot across all public keys in `s_signing_pubkey_ring`. */
bool verify_artifact_signature(const uint8_t digest_raw[32], const char *sig_str,
                               const char **detail_out)
{
#if defined(FF_OTA_PRODUCTION_BUILD)
    if (ff_ota_uses_dev_signing_key()) {
        *detail_out = "production ring carries dev key";
        return false;
    }
#endif
    if (sig_str == NULL || sig_str[0] == '\0') {
#if FF_OTA_REQUIRE_SIGNATURE
        *detail_out = "signature missing";
        return false;
#else
        return true;
#endif
    }

    static const char PREFIX[] = "ecdsa-p256-sha256:";
    const size_t prefix_len = sizeof(PREFIX) - 1;
    if (strncmp(sig_str, PREFIX, prefix_len) != 0) {
        *detail_out = "artifact sig malformed";
        return false;
    }

    const char *b64 = sig_str + prefix_len;
    uint8_t der[96];
    size_t der_len = 0;
    if (mbedtls_base64_decode(der, sizeof(der), &der_len, (const unsigned char *)b64,
                             strlen(b64)) != 0 ||
        der_len < 8 || der_len > 80 || der[0] != 0x30) {
        *detail_out = "artifact sig malformed";
        return false;
    }

    for (size_t i = 0; i < sizeof(s_signing_pubkey_ring) / sizeof(s_signing_pubkey_ring[0]); i++) {
        const char *pem = signing_pubkey_slot(i);
        mbedtls_pk_context pk;
        mbedtls_pk_init(&pk);
        int rc = mbedtls_pk_parse_public_key(&pk, (const unsigned char *)pem, strlen(pem) + 1);
        if (rc == 0) {
            rc = mbedtls_pk_verify(&pk, MBEDTLS_MD_SHA256, digest_raw, 32, der, der_len);
        }
        mbedtls_pk_free(&pk);
        if (rc == 0) {
            return true;
        }
    }

    *detail_out = "signature invalid";
    return false;
}

esp_http_client_handle_t s_ota_http_client;
char s_resume_range_hdr[48];

esp_err_t ota_http_client_init_cb(esp_http_client_handle_t client)
{
    s_ota_http_client = client;
    if (s_resume_range_hdr[0] != '\0') {
        esp_err_t err = esp_http_client_set_header(client, "Range", s_resume_range_hdr);
        s_resume_range_hdr[0] = '\0';
        return err;
    }
    return ESP_OK;
}

esp_err_t resume_range_perform(esp_https_ota_handle_t handle, const ff_ota_cmd_t *cmd,
                               int *last_len, int64_t *last_progress_us,
                               bool *stalled, bool *cancel_seen)
{
    esp_err_t err = ESP_FAIL;
    for (int attempt = 0; attempt < OTA_MAX_RETRIES; attempt++) {
        if (cancel_requested()) {
            *cancel_seen = true;
            return ESP_ERR_HTTPS_OTA_IN_PROGRESS;
        }
        uint32_t delay_ms = (uint32_t)OTA_RETRY_BASE_MS << attempt;
        int offset = esp_https_ota_get_image_len_read(handle);
        int start = offset > 0 ? offset : 0;
        char range_hdr[48];
        snprintf(range_hdr, sizeof(range_hdr), "bytes=%d-", start);
        memcpy(s_resume_range_hdr, range_hdr, sizeof(s_resume_range_hdr));
        ESP_LOGW(TAG, "update %s: transient transfer fault at %d bytes; retry %d/%d in %" PRIu32
                      " ms (%s)",
                 cmd->cmd_id, offset, attempt + 1, OTA_MAX_RETRIES, delay_ms, range_hdr);
        vTaskDelay(pdMS_TO_TICKS(delay_ms));
        if (cancel_requested()) {
            *cancel_seen = true;
            return ESP_ERR_HTTPS_OTA_IN_PROGRESS;
        }
        if (s_ota_http_client != NULL) {
            (void)esp_http_client_close(s_ota_http_client);
            (void)esp_http_client_set_header(s_ota_http_client, "Range", s_resume_range_hdr);
            s_resume_range_hdr[0] = '\0';
            esp_err_t open_err = esp_http_client_open(s_ota_http_client, 0);
            if (open_err != ESP_OK) {
                err = open_err;
                continue;
            }
            if (esp_http_client_fetch_headers(s_ota_http_client) < 0) {
                (void)esp_http_client_close(s_ota_http_client);
                err = ESP_ERR_INVALID_RESPONSE;
                continue;
            }
            int status = esp_http_client_get_status_code(s_ota_http_client);
            if (status >= 400 && status < 500 && status != 408 && status != 429) {
                (void)esp_http_client_close(s_ota_http_client);
                return ESP_ERR_INVALID_RESPONSE;
            }
            if (status != 206 && !(offset <= 0 && status == 200)) {
                (void)esp_http_client_close(s_ota_http_client);
                err = ESP_ERR_INVALID_RESPONSE;
                continue;
            }
        }
        for (;;) {
            err = esp_https_ota_perform(handle);
            if (err != ESP_ERR_HTTPS_OTA_IN_PROGRESS) {
                break;
            }
            if (cancel_requested()) {
                *cancel_seen = true;
                return err;
            }
            int so_far = esp_https_ota_get_image_len_read(handle);
            int64_t now_us = esp_timer_get_time();
            if (so_far > offset) {
                attempt = 0;
                offset = so_far;
            }
            if (so_far > *last_len) {
                *last_len = so_far;
                *last_progress_us = now_us;
            } else if (now_us - *last_progress_us >= (int64_t)OTA_STALL_MS * 1000) {
                *stalled = true;
                return err;
            }
        }
        if (err == ESP_OK) {
            return ESP_OK;
        }
    }
    return err;
}

typedef struct {
    char *url;
    bool overflow;
} redirect_capture_t;

static esp_err_t capture_location(esp_http_client_event_t *evt)
{
    if (evt->event_id != HTTP_EVENT_ON_HEADER || evt->user_data == NULL) {
        return ESP_OK;
    }
    if (evt->header_key == NULL || strcasecmp(evt->header_key, "Location") != 0) {
        return ESP_OK;
    }
    redirect_capture_t *capture = (redirect_capture_t *)evt->user_data;
    if (strlcpy(capture->url, evt->header_value, OTA_RESOLVED_URL_MAX) >= OTA_RESOLVED_URL_MAX) {
        capture->url[0] = '\0';
        capture->overflow = true;
    }
    return ESP_OK;
}

/* Resolve the artifact link's ONE redirect by hand so S3/MinIO presigned Host headers stay intact. */
esp_err_t resolve_artifact_url(const char *url, char *out)
{
    out[0] = '\0';
    redirect_capture_t capture = {.url = out, .overflow = false};

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_GET,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = OTA_HTTP_TIMEOUT_MS,
        .buffer_size = OTA_HTTP_RX_BUFFER,
        .buffer_size_tx = OTA_HTTP_TX_BUFFER,
        .disable_auto_redirect = true,
        .event_handler = capture_location,
        .user_data = &capture,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) {
        return ESP_ERR_NO_MEM;
    }

    esp_err_t err = esp_http_client_open(client, 0);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot reach the artifact origin: %s", esp_err_to_name(err));
        esp_http_client_cleanup(client);
        return err;
    }
    if (esp_http_client_fetch_headers(client) < 0) {
        ESP_LOGE(TAG, "the artifact origin sent no usable response headers");
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        return ESP_ERR_INVALID_RESPONSE;
    }

    int status = esp_http_client_get_status_code(client);
    esp_http_client_close(client);
    esp_http_client_cleanup(client);

    if (status == 200) {
        if (strlcpy(out, url, OTA_RESOLVED_URL_MAX) >= OTA_RESOLVED_URL_MAX) {
            return ESP_ERR_INVALID_SIZE;
        }
        return ESP_OK;
    }
    if (status < 300 || status > 399) {
        ESP_LOGE(TAG, "the artifact link answered %d", status);
        return ESP_ERR_INVALID_RESPONSE;
    }
    if (capture.overflow) {
        ESP_LOGE(TAG, "the store's download URL is longer than this firmware's %d-byte "
                      "buffer",
                 OTA_RESOLVED_URL_MAX);
        return ESP_ERR_INVALID_SIZE;
    }
    if (out[0] == '\0') {
        ESP_LOGE(TAG, "the artifact link answered %d with no Location", status);
        return ESP_ERR_INVALID_RESPONSE;
    }
    if (strncasecmp(out, "http://", 7) != 0 && strncasecmp(out, "https://", 8) != 0) {
        out[0] = '\0';
        ESP_LOGE(TAG, "the artifact link redirected to a relative Location");
        return ESP_ERR_INVALID_RESPONSE;
    }
    ESP_LOGI(TAG, "artifact link redirected (%d) to the object store", status);
    return ESP_OK;
}
