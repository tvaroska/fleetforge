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
#include <strings.h>

#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "ff_identity.h"
#include "ff_time.h"

static const char *TAG = "ff-progress";

#define PROGRESS_PATH "/v1/device-progress"

/* A fifth of the enroll timeout. A stage report is stale as soon as the next stage
 * happens, and the boot sequence must not wait on one. */
#define PROGRESS_TIMEOUT_MS 5000

/* api/schemas.py::MAX_PROGRESS_DETAIL. Retyped rather than shared — there is no way to
 * share a constant across this seam — so it is truncated here and never 422'd there. */
#define PROGRESS_MAX_DETAIL 200

/* api/schemas.py's stage pattern is `^[a-z][a-z0-9_]{0,31}$`, so 32 characters is the
 * widest stage a future caller could legally pass. Retyped across the same seam as
 * PROGRESS_MAX_DETAIL: a silent strlcpy truncation here would turn a valid stage into a
 * different one, which is worse than dropping it. */
#define FF_PROGRESS_MAX_STAGE 32

/* Stages produced before the transport can carry them (S0-fw-2) wait here. Depth 3:
 * only `link_up` can realistically queue today, with room for a pre-clock `halted`.
 * Static, because a report must never be able to fail on a malloc. */
#define FF_PROGRESS_QUEUE_DEPTH 3

static struct {
    bool armed;
    /* Decided once, from the scheme of the built URL: ff_cfg.h documents `api_base` as
     * "the scheme selects TLS", and a plaintext base needs no clock at all. */
    bool tls;
    char url[FF_CFG_MAX_URI + sizeof(PROGRESS_PATH)];
    char token[FF_CFG_MAX_TOKEN];
    struct {
        char stage[FF_PROGRESS_MAX_STAGE + 1];
        char detail[PROGRESS_MAX_DETAIL + 1];
    } queue[FF_PROGRESS_QUEUE_DEPTH];
    size_t queued; /* number of live entries, oldest first */
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
    s_state.tls = strncasecmp(s_state.url, "https://", 8) == 0;
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

/* Send one report. Returns false when it did not reach the server at all (client init,
 * open, or a short write) — a status that came back, even a bad one, counts as sent. */
static bool post_one(const char *stage, const char *detail)
{
    bool sent = false;

    char *body = build_body(stage, detail);
    if (body == NULL) {
        return false;
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
    sent = true;

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
        /* Nothing a held stage can ever do now but sit in RAM. */
        memset(s_state.queue, 0, sizeof(s_state.queue));
        s_state.queued = 0;
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
    return sent;
}

/* Can a report leave this board right now?
 *
 * NOT `ff_time_is_sane()` alone. A plaintext lab (`http://` + `--no-ntp`) never sets its
 * clock, so a clock-only gate would hold `link_up` forever and break the one setup where
 * the stage has always worked. Only a TLS handshake needs the date. */
static bool transport_ready(void)
{
    return !s_state.tls || ff_time_is_sane();
}

/* Hold a stage until the transport can carry it. Full queue drops the OLDEST entry: the
 * dashboard shows the newest stage per device, so the newest is the one worth keeping. */
static void enqueue(const char *stage, const char *detail)
{
    if (s_state.queued == FF_PROGRESS_QUEUE_DEPTH) {
        memmove(&s_state.queue[0], &s_state.queue[1],
                sizeof(s_state.queue[0]) * (FF_PROGRESS_QUEUE_DEPTH - 1));
        s_state.queued--;
    }
    /* NULL and "" are the same held entry — build_body() omits an empty detail. */
    strlcpy(s_state.queue[s_state.queued].stage, stage,
            sizeof(s_state.queue[s_state.queued].stage));
    strlcpy(s_state.queue[s_state.queued].detail, detail == NULL ? "" : detail,
            sizeof(s_state.queue[s_state.queued].detail));
    s_state.queued++;
    ESP_LOGD(TAG, "holding '%s' until the clock is set", stage);
}

/* Send everything held, oldest first, and drop it either way — a held stage gets exactly
 * one attempt, like every other report (property 2).
 *
 * Abandons the rest on the first send that does not reach the server: no route means the
 * remaining stale stages are not worth another PROGRESS_TIMEOUT_MS each on the boot path. */
static void drain(void)
{
    size_t held = s_state.queued;
    s_state.queued = 0;

    for (size_t i = 0; i < held; i++) {
        if (!s_state.armed) {
            break; /* a held entry took the 401 branch */
        }
        if (!post_one(s_state.queue[i].stage, s_state.queue[i].detail)) {
            ESP_LOGD(TAG, "dropping %u held stage(s): '%s' did not reach the server",
                     (unsigned)(held - i), s_state.queue[i].stage);
            break;
        }
    }
    memset(s_state.queue, 0, sizeof(s_state.queue));
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

    if (!transport_ready()) {
        enqueue(stage, detail);
        return;
    }

    /* Drained BEFORE this call's own POST, so the server — which timestamps at receipt
     * and breaks `at` ties by `id` — sees the held stages ahead of this one. */
    if (s_state.queued > 0) {
        drain();
        /* A held entry can have disarmed the reporter on a 401 mid-drain; carrying on
         * would be exactly the "keep talking with a dead credential" property 3 forbids. */
        if (!s_state.armed) {
            return;
        }
    }

    (void)post_one(stage, detail);
}
