/*
 * ff_progress — best-effort boot/enrol stage reports. See ff_progress.h.
 *
 * Structurally ff_enroll.c's little sibling — same open/write/read form, same crt
 * bundle, same "nothing secret is ever logged" — with every knob turned the other way,
 * because the two have opposite jobs. Enrolment must succeed or the board is useless;
 * a progress report must never cost anything, so it has a short timeout, no retry, no
 * return value and no error that can propagate.
 */

#include "ff_progress.h"

#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "ff_identity.h"

static const char *TAG = "ff-progress";

#define PROGRESS_PATH "/v1/device-progress"

/* A fifth of the enroll timeout. A stage report is stale as soon as the next stage
 * happens, and the boot sequence must not wait on one. */
#define PROGRESS_TIMEOUT_MS 5000

/* api/schemas.py::MAX_PROGRESS_DETAIL. Retyped rather than shared — there is no way to
 * share a constant across this seam — so it is truncated here and never 422'd there. */
#define PROGRESS_MAX_DETAIL 200

static struct {
    bool armed;
    char url[FF_CFG_MAX_URI + sizeof(PROGRESS_PATH)];
    char token[FF_CFG_MAX_TOKEN];
} s_state;

void ff_progress_init(const ff_cfg_t *cfg)
{
    memset(&s_state, 0, sizeof(s_state));

    if (cfg == NULL || cfg->token[0] == '\0' || cfg->api_base[0] == '\0') {
        /* A board that has already enrolled has an empty token in ff_cfg, and that is
         * normal: it reports nothing and reaches the broker in seconds anyway. Not a
         * warning, because it would fire on every healthy re-boot of every board. */
        ESP_LOGD(TAG, "no enrollment token in ff_cfg; stage reporting is off this boot");
        return;
    }

    int written = snprintf(s_state.url, sizeof(s_state.url), "%s" PROGRESS_PATH, cfg->api_base);
    if (written < 0 || (size_t)written >= sizeof(s_state.url)) {
        ESP_LOGW(TAG, "api_base is too long to build a progress URL from; reporting is off");
        return;
    }

    strlcpy(s_state.token, cfg->token, sizeof(s_state.token));
    s_state.armed = true;
}

/* Copy `detail` into `out`, dropping control characters and truncating to the server's
 * limit. The server refuses control characters (they let a device forge a log line), so
 * stripping them here is what keeps a park() reason with a newline reportable. */
static void sanitize_detail(const char *detail, char *out, size_t len)
{
    size_t w = 0;
    for (size_t r = 0; detail[r] != '\0' && w + 1 < len; r++) {
        unsigned char c = (unsigned char)detail[r];
        out[w++] = (c < 0x20 || c == 0x7f) ? ' ' : (char)c;
    }
    out[w] = '\0';
}

/* Build `{"token":…,"device_id":…,"stage":…,"detail":…}` with cJSON rather than
 * snprintf: `detail` is a free-text reason and one quote in it would otherwise produce
 * a body the server rejects as malformed. Caller frees. */
static char *build_body(const char *stage, const char *detail)
{
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return NULL;
    }

    bool ok = cJSON_AddStringToObject(root, "token", s_state.token) != NULL &&
              cJSON_AddStringToObject(root, "device_id", ff_device_id()) != NULL &&
              cJSON_AddStringToObject(root, "stage", stage) != NULL;

    if (ok && detail != NULL && detail[0] != '\0') {
        char clean[PROGRESS_MAX_DETAIL + 1];
        sanitize_detail(detail, clean, sizeof(clean));
        ok = cJSON_AddStringToObject(root, "detail", clean) != NULL;
    }

    char *body = ok ? cJSON_PrintUnformatted(root) : NULL;
    cJSON_Delete(root);
    return body;
}

void ff_progress_report(const char *stage, const char *detail)
{
    if (!s_state.armed) {
        return;
    }
    /* ff_identity_init() may not have run — park() reports `halted` from paths that
     * precede it — and a body with an empty device_id is a guaranteed 422. */
    if (ff_device_id()[0] == '\0') {
        return;
    }

    char *body = build_body(stage, detail);
    if (body == NULL) {
        return;
    }
    int body_len = (int)strlen(body);

    esp_http_client_config_t config = {
        .url = s_state.url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = PROGRESS_TIMEOUT_MS,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .disable_auto_redirect = true,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) {
        goto done;
    }
    esp_http_client_set_header(client, "content-type", "application/json");

    esp_err_t err = esp_http_client_open(client, body_len);
    if (err != ESP_OK) {
        /* Expected, and at DEBUG for that reason: this is the very failure mode the
         * feature cannot see through. Saying it at WARN on every stage of a board with
         * no route would bury the link errors that actually explain the problem. */
        ESP_LOGD(TAG, "could not report '%s': %s", stage, esp_err_to_name(err));
        goto done;
    }
    if (esp_http_client_write(client, body, body_len) != body_len) {
        ESP_LOGD(TAG, "the '%s' report was cut short while sending", stage);
        goto done;
    }
    /* The token has left the building; do not leave this copy of it in the heap. */
    memset(body, 0, (size_t)body_len);

    (void)esp_http_client_fetch_headers(client);
    int status = esp_http_client_get_status_code(client);
    if (status == 401) {
        /* Not going to start working. Disable for this boot rather than keep talking to
         * the server with a credential it has told us is dead. Loud, because a board
         * whose token is refused HERE will very shortly be refused at /v1/enroll too,
         * and this line lands minutes earlier. */
        ESP_LOGW(TAG, "the server refused this board's enrollment token (progress 401); "
                      "stage reporting is off for this boot. Enrolment will fail too — "
                      "re-flash ff_cfg with a fresh ffe_ token.");
        s_state.armed = false;
    } else if (status != 202) {
        ESP_LOGD(TAG, "progress %d for '%s'", status, stage);
    }

done:
    if (client != NULL) {
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
    }
    /* `body` still holds the token unless the write path already wiped it. */
    memset(body, 0, (size_t)body_len);
    free(body);
}
