/*
 * ff_sec — rotating project secrets in NVS (`ff_sec`), synced over HTTPS when `dn/cfg`
 * advances `secrets_rev` (SEC-fw-1).
 *
 * Security rules:
 *   1. Secret values are NEVER logged — only key names, counts, and revision numbers.
 *   2. `GET /v1/device-secrets` is authenticated with the board's per-device broker
 *      credential (`Authorization: Basic base64(device_id:mqtt_pass)`) over TLS.
 *   3. `ff_sec_synced()` is read by `ff_mqtt_sleep_blocker()`, so a sleepy e-ink wake stays
 *      awake while a secret rotation fetch is in flight and goes back to sleep once stored.
 */

#include "ff_sec.h"
#include "ff_sec_internal.h"

#include <inttypes.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "ff_identity_internal.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/base64.h"
#include "nvs.h"

static const char *TAG = "ff-sec";

#define SECRETS_PATH "/v1/device-secrets"
#define SECRETS_TIMEOUT_MS 20000
#define SECRETS_MAX_RESPONSE 4096
#define SECRETS_TASK_STACK 6144
#define SECRETS_TASK_PRIO 4
#define SECRETS_MAX_RETRIES 3
#define SECRETS_RETRY_BASE_MS 1000
#define SECRETS_MAX_STALE_KEYS 32

static const ff_cfg_t *s_cfg;
static const ff_cred_t *s_cred;
static esp_mqtt_client_handle_t s_client;
static char s_topic_announce[96];

static volatile uint32_t s_local_rev;
static volatile uint32_t s_target_rev;
static volatile uint32_t s_failed_rev;
static volatile bool s_sync_in_flight;

static bool valid_secret_key(const char *key)
{
    if (key == NULL || key[0] == '\0' || strcmp(key, FF_SEC_KEY_REV) == 0) {
        return false;
    }
    size_t len = 0;
    for (; key[len] != '\0'; len++) {
        if (len >= 15) {
            return false;
        }
        char c = key[len];
        const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                        (c >= '0' && c <= '9') || c == '_' || c == '-';
        if (!ok) {
            return false;
        }
    }
    return len > 0;
}

esp_err_t ff_get_secret(const char *key, char *out, size_t cap)
{
    if (!valid_secret_key(key) || out == NULL || cap < 2) {
        return ESP_ERR_INVALID_ARG;
    }
    out[0] = '\0';

    nvs_handle_t handle;
    esp_err_t err = nvs_open(FF_SEC_NAMESPACE, NVS_READONLY, &handle);
    if (err != ESP_OK) {
        return err;
    }
    size_t actual = cap;
    err = nvs_get_str(handle, key, out, &actual);
    nvs_close(handle);
    if (err == ESP_ERR_NVS_INVALID_LENGTH) {
        out[0] = '\0';
        return ESP_ERR_INVALID_SIZE;
    }
    if (err != ESP_OK) {
        out[0] = '\0';
    }
    return err;
}

void ff_sec_init(const ff_cfg_t *cfg, const ff_cred_t *cred)
{
    s_cfg = cfg;
    s_cred = cred;
    s_local_rev = 0;

    nvs_handle_t handle;
    if (nvs_open(FF_SEC_NAMESPACE, NVS_READONLY, &handle) == ESP_OK) {
        uint32_t rev = 0;
        if (nvs_get_u32(handle, FF_SEC_KEY_REV, &rev) == ESP_OK) {
            s_local_rev = rev;
        }
        nvs_close(handle);
    }
    if (s_target_rev < s_local_rev) {
        s_target_rev = s_local_rev;
    }
    ESP_LOGI(TAG, "secrets_rev %" PRIu32 " in NVS", s_local_rev);
}

uint32_t ff_sec_rev(void)
{
    return s_local_rev;
}

bool ff_sec_synced(void)
{
    return !s_sync_in_flight && (s_local_rev >= s_target_rev || s_failed_rev >= s_target_rev);
}

static esp_err_t store_secrets_json(const cJSON *secrets_obj, uint32_t rev)
{
    if (!cJSON_IsObject(secrets_obj)) {
        return ESP_ERR_INVALID_ARG;
    }

    nvs_handle_t handle;
    esp_err_t err = nvs_open(FF_SEC_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot open NVS '%s' for writing: %s", FF_SEC_NAMESPACE,
                 esp_err_to_name(err));
        return err;
    }

    /* Validate and write all incoming keys in-place BEFORE removing stale keys,
     * so a power loss or write error never wipes existing secrets. */
    const cJSON *item = NULL;
    cJSON_ArrayForEach(item, secrets_obj)
    {
        if (item->string != NULL && valid_secret_key(item->string) && cJSON_IsString(item) &&
            item->valuestring != NULL) {
            err = nvs_set_str(handle, item->string, item->valuestring);
            if (err != ESP_OK) {
                break;
            }
        }
    }

    /* Prune only keys that were removed from `secrets_obj` in this revision. */
    if (err == ESP_OK) {
        char stale_keys[SECRETS_MAX_STALE_KEYS][16];
        size_t stale_count = 0;
        nvs_iterator_t it = NULL;
        if (nvs_entry_find_in_handle(handle, NVS_TYPE_STR, &it) == ESP_OK) {
            while (it != NULL && stale_count < SECRETS_MAX_STALE_KEYS) {
                nvs_entry_info_t info;
                nvs_entry_info(it, &info);
                if (valid_secret_key(info.key) &&
                    !cJSON_HasObjectItem(secrets_obj, info.key)) {
                    strlcpy(stale_keys[stale_count], info.key, sizeof(stale_keys[0]));
                    stale_count++;
                }
                if (nvs_entry_next(&it) != ESP_OK) {
                    break;
                }
            }
            nvs_release_iterator(it);
        }
        for (size_t i = 0; i < stale_count; i++) {
            (void)nvs_erase_key(handle, stale_keys[i]);
        }
    }

    if (err == ESP_OK) {
        err = nvs_set_u32(handle, FF_SEC_KEY_REV, rev);
    }
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    return err;
}

static void secrets_sync_task(void *arg)
{
    (void)arg;
    if (s_cfg == NULL || s_cred == NULL || s_cfg->api_base[0] == '\0' ||
        s_cred->device_id[0] == '\0' || s_cred->mqtt_pass[0] == '\0') {
        s_sync_in_flight = false;
        vTaskDelete(NULL);
        return;
    }

    char url[FF_CFG_MAX_URI + sizeof(SECRETS_PATH)];
    int written = snprintf(url, sizeof(url), "%s" SECRETS_PATH, s_cfg->api_base);
    if (written < 0 || (size_t)written >= sizeof(url)) {
        ESP_LOGE(TAG, "api_base too long for device-secrets URL");
        s_sync_in_flight = false;
        vTaskDelete(NULL);
        return;
    }

    char raw_cred[FF_CRED_MAX_USER + 1 + FF_CRED_MAX_PASS];
    int cred_len = snprintf(raw_cred, sizeof(raw_cred), "%s:%s", s_cred->device_id,
                            s_cred->mqtt_pass);
    unsigned char b64[256];
    size_t b64_len = 0;
    if (cred_len <= 0 || (size_t)cred_len >= sizeof(raw_cred) ||
        mbedtls_base64_encode(b64, sizeof(b64) - 1, &b64_len, (const unsigned char *)raw_cred,
                              (size_t)cred_len) != 0) {
        memset(raw_cred, 0, sizeof(raw_cred));
        s_sync_in_flight = false;
        vTaskDelete(NULL);
        return;
    }
    memset(raw_cred, 0, sizeof(raw_cred));
    b64[b64_len] = '\0';

    char auth_hdr[272];
    snprintf(auth_hdr, sizeof(auth_hdr), "Basic %s", (const char *)b64);
    memset(b64, 0, sizeof(b64));

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_GET,
        .timeout_ms = SECRETS_TIMEOUT_MS,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .disable_auto_redirect = true,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) {
        memset(auth_hdr, 0, sizeof(auth_hdr));
        s_sync_in_flight = false;
        vTaskDelete(NULL);
        return;
    }
    esp_http_client_set_header(client, "authorization", auth_hdr);
    memset(auth_hdr, 0, sizeof(auth_hdr));

    char *response = calloc(1, SECRETS_MAX_RESPONSE + 1);
    if (response == NULL) {
        s_failed_rev = s_target_rev;
        goto done;
    }

    bool fetched = false;
    for (int attempt = 0; attempt < SECRETS_MAX_RETRIES; attempt++) {
        if (attempt > 0) {
            uint32_t delay_ms = (uint32_t)SECRETS_RETRY_BASE_MS << (attempt - 1);
            vTaskDelay(pdMS_TO_TICKS(delay_ms));
            (void)esp_http_client_close(client);
        }
        esp_err_t err = esp_http_client_open(client, 0);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "cannot reach %s (attempt %d/%d): %s", url, attempt + 1,
                     SECRETS_MAX_RETRIES, esp_err_to_name(err));
            continue;
        }
        int64_t content_length = esp_http_client_fetch_headers(client);
        int status = esp_http_client_get_status_code(client);
        if (content_length > SECRETS_MAX_RESPONSE) {
            ESP_LOGW(TAG, "device-secrets %d: response too large", status);
            break;
        }
        int read_len = esp_http_client_read_response(client, response, SECRETS_MAX_RESPONSE);
        if (read_len >= 0 && status == 200) {
            response[read_len] = '\0';
            fetched = true;
            break;
        }
        ESP_LOGW(TAG, "device-secrets HTTP %d (attempt %d/%d)", status, attempt + 1,
                 SECRETS_MAX_RETRIES);
        if (status >= 400 && status < 500 && status != 429) {
            break;
        }
    }

    if (!fetched) {
        /* Release the sleep blocker for this wake cycle after bounded retries fail
         * so a transient server outage does not drain a sleepy board's battery. */
        s_failed_rev = s_target_rev;
        goto done;
    }

    cJSON *root = cJSON_Parse(response);
    if (root != NULL) {
        const cJSON *rev_item = cJSON_GetObjectItemCaseSensitive(root, "secrets_rev");
        const cJSON *secrets_obj = cJSON_GetObjectItemCaseSensitive(root, "secrets");
        if (cJSON_IsNumber(rev_item) && rev_item->valuedouble >= 0) {
            uint32_t rev = (uint32_t)rev_item->valuedouble;
            if (store_secrets_json(secrets_obj, rev) == ESP_OK) {
                s_local_rev = rev;
                s_failed_rev = 0;
                if (s_target_rev < rev) {
                    s_target_rev = rev;
                }
                ESP_LOGI(TAG, "synced secrets_rev %" PRIu32 " to NVS", rev);
                if (s_client != NULL && s_topic_announce[0] != '\0') {
                    char *announce = ff_identity_announce_json(s_cfg);
                    if (announce != NULL) {
                        (void)esp_mqtt_client_enqueue(s_client, s_topic_announce, announce, 0, 1,
                                                      1, true);
                        free(announce);
                    }
                }
            } else {
                s_failed_rev = s_target_rev;
            }
        } else {
            s_failed_rev = s_target_rev;
        }
        cJSON_Delete(root);
    } else {
        s_failed_rev = s_target_rev;
    }

done:
    if (response != NULL) {
        memset(response, 0, SECRETS_MAX_RESPONSE + 1);
        free(response);
    }
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    s_sync_in_flight = false;
    vTaskDelete(NULL);
}

void ff_sec_on_cfg(uint32_t target_rev, esp_mqtt_client_handle_t client,
                   const char *topic_announce)
{
    s_client = client;
    if (topic_announce != NULL) {
        strlcpy(s_topic_announce, topic_announce, sizeof(s_topic_announce));
    }
    if (target_rev <= s_local_rev) {
        return;
    }
    s_target_rev = target_rev;
    if (s_sync_in_flight) {
        return;
    }
    s_sync_in_flight = true;
    if (xTaskCreate(secrets_sync_task, "ff_sec", SECRETS_TASK_STACK, NULL, SECRETS_TASK_PRIO,
                    NULL) != pdPASS) {
        ESP_LOGE(TAG, "cannot start secrets sync task");
        s_sync_in_flight = false;
    }
}
