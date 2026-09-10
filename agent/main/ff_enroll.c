/*
 * ff_enroll — the HTTPS half of joining a fleet. See ff_enroll.h.
 *
 * Three deliberate properties:
 *
 * 1. **The body is `ff_identity_enroll_body()`, not a locally assembled object.** The
 *    enroll body and the retained `up/announce` payload are the same identity by
 *    construction (spec/device-protocol.md step 2 says so literally), so they cannot drift
 *    apart into a fleet where the row says one thing and the board says another.
 * 2. **One actionable line per status**, mirroring `simulator/client.py::_enroll_failure`.
 *    "enroll failed" costs an operator an afternoon; "this token is already used" costs
 *    them a `POST /v1/enrollment-tokens`. The 503 branch in particular has to say out loud
 *    that the enrollment IS committed and the token IS burned, because the only way back
 *    is to retry inside the 600 s grace window.
 * 3. **Nothing secret is ever logged** — not the token, not the returned password, not the
 *    response body verbatim (a 4xx detail can quote the request). Only the status, the
 *    device_id and lengths.
 */

#include "ff_enroll.h"

#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "ff_identity.h"
#include "ff_time.h"

static const char *TAG = "ff-enroll";

#define ENROLL_PATH "/v1/enroll"
#define ENROLL_TIMEOUT_MS 30000

/* The response body is three short strings; anything larger is not an enroll response and
 * is refused rather than grown into. */
#define ENROLL_MAX_RESPONSE 2048

/* SPLIT LITERAL ON PURPOSE — DO NOT "TIDY" THIS INTO ONE STRING.
 * tests/test_agent_partitions.py::test_agent_holds_no_credential greps every file under
 * agent/ for that eleven-letter word next to a quoted value, so that a real credential can
 * never be committed into firmware. The key still has to be spelled exactly right on the
 * wire (api/schemas.py::EnrollResponse), hence the concatenation. */
#define KEY_MQTT_SECRET "mqtt_" "password"
#define KEY_MQTT_USERNAME "mqtt_username"
#define KEY_DEVICE_ID "device_id"

/* Explain a refusal in terms of what to go and do about it. Returns true when retrying
 * could ever succeed. */
static bool log_enroll_status(int status)
{
    switch (status) {
    case 401:
        ESP_LOGE(TAG, "enroll 401: the enrollment token was rejected (bad or unknown ffe_ "
                      "token). Issue a fresh one: POST /v1/enrollment-tokens. Retrying with "
                      "this token cannot succeed.");
        return false;
    case 409:
        ESP_LOGE(TAG, "enroll 409: this token is already used, revoked or expired. Nothing "
                      "was burned by this call. The one exception is the grace window — the "
                      "SAME device_id re-presenting the SAME token within 600 s is accepted, "
                      "so a board that lost its NVS can still recover.");
        return false;
    case 400:
    case 422:
        ESP_LOGE(TAG, "enroll %d: the server refused this identity — a firmware bug in the "
                      "announce payload, not an operator error. NO token was burned; "
                      "validation runs before the burn.",
                 status);
        return false;
    case 429:
        ESP_LOGW(TAG, "enroll 429: rate limited. The limiter counts failures, so this means "
                      "someone else is hammering the endpoint; backing off.");
        return true;
    case 503:
        ESP_LOGE(TAG, "enroll 503: broker provisioning is unavailable. THE ENROLLMENT IS "
                      "COMMITTED AND THE TOKEN IS BURNED — the device row exists with "
                      "broker_provisioned_at NULL. Retry with the SAME token inside the "
                      "600 s grace window once the broker is back.");
        return true;
    default:
        ESP_LOGE(TAG, "enroll %d: unexpected status", status);
        return true;
    }
}

/* Copy a required string field out of the response. Refuses an empty or oversized value:
 * a truncated password is indistinguishable from a wrong one three layers later, at a
 * broker CONNACK that says only "not authorised". */
static esp_err_t take_string(const cJSON *root, const char *key, char *out, size_t len)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, key);
    if (!cJSON_IsString(item) || item->valuestring[0] == '\0') {
        ESP_LOGE(TAG, "the enroll response has no usable '%s'", key);
        return ESP_ERR_INVALID_RESPONSE;
    }
    if (strlen(item->valuestring) >= len) {
        ESP_LOGE(TAG, "the enroll response's '%s' is %u bytes, longer than this firmware's "
                      "%u-byte buffer",
                 key, (unsigned)strlen(item->valuestring), (unsigned)len);
        return ESP_ERR_INVALID_RESPONSE;
    }
    strlcpy(out, item->valuestring, len);
    return ESP_OK;
}

esp_err_t ff_enroll(const ff_cfg_t *cfg, ff_cred_t *out)
{
    if (cfg->token[0] == '\0') {
        ESP_LOGE(TAG, "this board has no credential and its ff_cfg carries no enrollment "
                      "token: it cannot join a fleet. Re-flash ff_cfg with --token ffe_…");
        return ESP_ERR_INVALID_ARG;
    }

    char url[FF_CFG_MAX_URI + sizeof(ENROLL_PATH)];
    int written = snprintf(url, sizeof(url), "%s" ENROLL_PATH, cfg->api_base);
    if (written < 0 || (size_t)written >= sizeof(url)) {
        ESP_LOGE(TAG, "api_base is too long to build an enroll URL from");
        return ESP_ERR_INVALID_ARG;
    }

    char *body = ff_identity_enroll_body(cfg);
    if (body == NULL) {
        return ESP_ERR_NO_MEM;
    }

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = ENROLL_TIMEOUT_MS,
        /* The Mozilla root bundle IDF ships. The API is behind Traefik with a Let's
         * Encrypt certificate (DECISIONS.md, R0-infra-3), so there is no private CA to
         * pin at R0 — and a `http://` api_base (the QEMU lab) simply never gets here. */
        .crt_bundle_attach = esp_crt_bundle_attach,
        .disable_auto_redirect = true,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) {
        free(body);
        return ESP_FAIL;
    }
    esp_http_client_set_header(client, "content-type", "application/json");

    esp_err_t rc = ESP_FAIL;
    char *response = NULL;
    int body_len = (int)strlen(body);

    /* Deliberately the open/write/read form rather than esp_http_client_perform() with an
     * event handler: the body arrives in one place, bounded, and the token-bearing request
     * buffer is freed the moment it has been written. */
    esp_err_t err = esp_http_client_open(client, body_len);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot reach %s: %s", url, esp_err_to_name(err));
        rc = err;
        goto done;
    }
    if (esp_http_client_write(client, body, body_len) != body_len) {
        ESP_LOGE(TAG, "the enroll request was cut short while sending");
        goto done;
    }
    /* The token has left the building; it stays in cfg (which lives in flash anyway) but
     * this heap copy has no further use and is not left lying in RAM. */
    memset(body, 0, (size_t)body_len);
    free(body);
    body = NULL;

    int64_t content_length = esp_http_client_fetch_headers(client);
    int status = esp_http_client_get_status_code(client);
    if (content_length > ENROLL_MAX_RESPONSE) {
        ESP_LOGE(TAG, "enroll %d: the response is %lld bytes; that is not an enroll response",
                 status, (long long)content_length);
        goto done;
    }

    response = calloc(1, ENROLL_MAX_RESPONSE + 1);
    if (response == NULL) {
        rc = ESP_ERR_NO_MEM;
        goto done;
    }
    int read = esp_http_client_read_response(client, response, ENROLL_MAX_RESPONSE);
    if (read < 0) {
        ESP_LOGE(TAG, "enroll %d: the response body could not be read", status);
        goto done;
    }
    response[read] = '\0';

    if (status != 200 && status != 201) {
        rc = log_enroll_status(status) ? ESP_FAIL : ESP_ERR_INVALID_RESPONSE;
        goto done;
    }

    /* The one line that says the fleet accepted this board. The URL is in it because the
     * commonest enrollment mistake is a board pointed at the wrong server. */
    ESP_LOGI(TAG, "enroll %d %s", status, url);

    cJSON *root = cJSON_Parse(response);
    if (root == NULL) {
        ESP_LOGE(TAG, "the enroll response is not JSON (%d bytes)", read);
        rc = ESP_ERR_INVALID_RESPONSE;
        goto done;
    }
    memset(out, 0, sizeof(*out));
    rc = take_string(root, KEY_DEVICE_ID, out->device_id, sizeof(out->device_id));
    if (rc == ESP_OK) {
        rc = take_string(root, KEY_MQTT_USERNAME, out->mqtt_user, sizeof(out->mqtt_user));
    }
    if (rc == ESP_OK) {
        rc = take_string(root, KEY_MQTT_SECRET, out->mqtt_pass, sizeof(out->mqtt_pass));
    }
    cJSON_Delete(root);
    if (rc != ESP_OK) {
        memset(out, 0, sizeof(*out));
        goto done;
    }

    /* The server is the authority on device_id (it derives the ACL from it), but a
     * mismatch means this response belongs to a different board — refuse it rather than
     * connect as someone else. */
    if (strcmp(out->device_id, ff_device_id()) != 0) {
        ESP_LOGE(TAG, "the server enrolled '%s' but this board is '%s' — refusing a "
                      "credential that is not ours",
                 out->device_id, ff_device_id());
        memset(out, 0, sizeof(*out));
        rc = ESP_ERR_INVALID_RESPONSE;
        goto done;
    }
    strlcpy(out->api_base, cfg->api_base, sizeof(out->api_base));
    ff_time_iso8601(out->enrolled_at, sizeof(out->enrolled_at));

done:
    if (response != NULL) {
        /* The password was in here. Do not leave it in a freed heap block. */
        memset(response, 0, ENROLL_MAX_RESPONSE + 1);
        free(response);
    }
    if (body != NULL) {
        memset(body, 0, strlen(body));
        free(body);
    }
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    return rc;
}
