/*
 * ff_ota — download, write the inactive slot, verify, apply. See ff_ota.h.
 *
 * The wire behaviour is not invented here either: `src/fleetforge/simulator/device.py`
 * already walks `staging → downloading → verifying → staged → applying → rebooting`, the
 * ingestor parses what it publishes, and this file repeats that walk on real flash. Four
 * properties are worth stating out loud, because each one is a silent wrong answer rather
 * than an error:
 *
 * 1. **`downloading` is published exactly once**, not per chunk. `deploy_events` is a log
 *    of transitions, not a progress feed — `deploys.record_observed_status` dedups on
 *    `(device_id, cmd_id, state)`, so a per-chunk publish writes nothing and costs the
 *    broker a message per 4 KB. Progress goes to the serial console instead.
 * 2. **The digest is checked by reading the partition BACK, after esp_https_ota_finish().**
 *    Hashing the stream as it arrives looks equivalent and is not: `esp_ota_write` withholds
 *    the first 16 bytes of the image header until the write completes, so a hash of "what we
 *    think we wrote" is a hash of something that was never on flash. Reading the slot back
 *    is the only check that covers the flash write itself, which is the part that can fail.
 * 3. **A mismatch puts the boot partition back.** `esp_https_ota_finish()` has already
 *    called `esp_ota_set_boot_partition()` by the time we hash, so the undo —
 *    `esp_ota_set_boot_partition(esp_ota_get_running_partition())` — is not tidiness: without
 *    it a board with a bad image reboots into it at the next power cut, which for this
 *    product means a van and a screwdriver.
 * 4. **The URL is never logged.** It is the authorization (R1-be-3: "signed URL never
 *    persisted, never logged"), so it does not appear in a log line, in a `detail`, or
 *    truncated "just for debugging". Everything else about a failure is said plainly.
 */

#include "ff_ota.h"

#include <inttypes.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_https_ota.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_system.h"
#include "ff_mqtt.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/sha256.h"

static const char *TAG = "ff-ota";

/* TLS session + mbedtls sha256 + the OTA component's own buffers. Measured headroom is
 * comfortable at 8 KB; below ~6 KB the handshake overflows it, and a stack overflow on a
 * board mid-deploy is a reboot loop with nobody there to read the backtrace. */
#define OTA_TASK_STACK 8192
/* Above the heartbeat (4) so a download cannot starve it, below esp-mqtt's own task. */
#define OTA_TASK_PRIO 5

#define OTA_HTTP_TIMEOUT_MS 20000
/* Response headers. The 307 to the object store carries a long `Location`. */
#define OTA_HTTP_RX_BUFFER 4096
/* THE REQUEST buffer, and the gotcha: IDF defaults it to 512 bytes, while the request line
 * after the redirect contains the whole presigned query string. Too small and the client
 * fails while writing the request, which surfaces as a connection error and reads like a
 * network fault. */
#define OTA_HTTP_TX_BUFFER 2048

#define OTA_READBACK_CHUNK 4096

/* The URL the artifact link redirects to. Twice `ff_ota_cmd_t.url` because a presigned S3
 * link is the long one: host + key + ~7 X-Amz-* query parameters. Heap, not stack — the OTA
 * task's 8 KB is already sized for the TLS handshake. */
#define OTA_RESOLVED_URL_MAX 1024

/* Long enough for two QoS-1 PUBLISHes (`applying`, `rebooting`) to leave the wire before
 * the reboot kills the socket. Without it the last thing the fleet sees is `staged`, and
 * the deploy looks stuck forever. */
#define OTA_PUBLISH_DRAIN_MS 1500

/* One update at a time, for the whole life of the process. Not a mutex: a second `stage`
 * must be REFUSED and reported, not queued behind the first — see ff_ota_start(). */
static volatile bool s_running;

static void fail(const ff_ota_cmd_t *cmd, const char *detail)
{
    ESP_LOGE(TAG, "update %s failed: %s", cmd->cmd_id, detail);
    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE, detail);
}

/* Lowercase hex, by hand rather than through snprintf, for the same reason as
 * ff_store.c::token_fingerprint: no stdio, and nothing a -Werror format check can trip on. */
static void hex_encode(const uint8_t *digest, size_t len, char *out)
{
    static const char HEX[] = "0123456789abcdef";
    for (size_t i = 0; i < len; i++) {
        out[i * 2] = HEX[digest[i] >> 4];
        out[i * 2 + 1] = HEX[digest[i] & 0x0f];
    }
    out[len * 2] = '\0';
}

/* sha256 of the first `length` bytes of `part`, as it is now on flash. */
static esp_err_t partition_digest(const esp_partition_t *part, size_t length, char out[65])
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
        hex_encode(digest, sizeof(digest), out);
    }

    mbedtls_sha256_free(&sha);
    free(buf);
    return err;
}

/* Put the boot partition back where it was before esp_https_ota_finish() moved it. The
 * one branch in this file that must never be wrong: it runs when the image on flash is
 * NOT the image the server asked for, and skipping it arms a reboot into unverified
 * bytes. Logged at ERROR either way — an operator needs to know a board is carrying a
 * rejected image in its spare slot. */
static void restore_boot_partition(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    if (running == NULL) {
        ESP_LOGE(TAG, "cannot read the running partition to undo the boot switch — this "
                      "board may reboot into an image that failed verification");
        return;
    }
    esp_err_t err = esp_ota_set_boot_partition(running);
    if (err == ESP_OK) {
        ESP_LOGE(TAG, "boot partition put back to %s: the staged image was rejected and "
                      "will NOT be booted",
                 running->label);
    } else {
        ESP_LOGE(TAG, "CANNOT undo the boot switch (%s) — this board will reboot into an "
                      "image that failed verification",
                 esp_err_to_name(err));
    }
}

/* Where the `Location` of the one redirect we follow is collected, out of the HTTP event
 * callback (the only public way to read a RESPONSE header out of esp_http_client). */
typedef struct {
    char *url; /* OTA_RESOLVED_URL_MAX bytes, or NULL */
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
        /* Truncated is worse than absent: it would be fetched, fail, and look like the
         * store was down. */
        capture->url[0] = '\0';
        capture->overflow = true;
    }
    return ESP_OK;
}

/* Resolve the artifact link's ONE redirect by hand, and hand esp_https_ota the URL it
 * lands on. `out` is OTA_RESOLVED_URL_MAX bytes and is a CREDENTIAL, exactly like the link
 * it came from.
 *
 * Why not let esp_https_ota follow the 307 itself, since it handles 301/302/303/307/308?
 * Because IDF v5.5.5 rebuilds the `Host` header wrong on a redirect, in two different ways,
 * and an S3-compatible presigned URL SIGNS that header:
 *   - esp_http_client_init() builds it with `_get_host_header(host, port)`, i.e. with the
 *     `:port` suffix whenever the port is not 80/443;
 *   - esp_http_client_set_url() — the redirect path — sets it to
 *     `client->connection_info.host` alone, dropping the port, and only when the host STRING
 *     changed, so a redirect that keeps the host and changes only the port keeps the FIRST
 *     hop's Host verbatim.
 * Either way the object store receives a Host that was never signed and answers
 * `403 SignatureDoesNotMatch`, which esp_https_ota reports as "File not found(403)".
 * Measured against MinIO on :9000 in the QEMU lab (origin :8080 → store :9000, the
 * host-unchanged case) and reproduced exactly with `curl -H 'Host: 10.0.2.2'` against the
 * same presigned URL, which 403s while the correct Host 200s.
 *
 * Production is unaffected today (GCS on :443 signs a portless Host), which is precisely
 * why this had to be fixed rather than left: it is a fault that appears only on a
 * self-hosted store — V2's whole shape — and only on a device.
 *
 * The cost is one header-only round trip on hop one, whose body is empty anyway. */
static esp_err_t resolve_artifact_url(const char *url, char *out)
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
        .disable_auto_redirect = true, /* the point: we want to SEE the Location */
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
        /* No redirect at all — an origin that serves the bytes itself. Use the link as
         * given. */
        if (strlcpy(out, url, OTA_RESOLVED_URL_MAX) >= OTA_RESOLVED_URL_MAX) {
            return ESP_ERR_INVALID_SIZE;
        }
        return ESP_OK;
    }
    if (status < 300 || status > 399) {
        /* 401/403 here is an expired or forged signature, 404 an artifact the store does
         * not have. The status is the whole diagnosis; the URL is still not logged. */
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
        /* A relative Location would have to be resolved against the request URL, and
         * every store we support returns an absolute one. Refused rather than guessed. */
        out[0] = '\0';
        ESP_LOGE(TAG, "the artifact link redirected to a relative Location");
        return ESP_ERR_INVALID_RESPONSE;
    }
    ESP_LOGI(TAG, "artifact link redirected (%d) to the object store", status);
    return ESP_OK;
}

static void ota_task(void *arg)
{
    ff_ota_cmd_t *cmd = (ff_ota_cmd_t *)arg;
    char *resolved = NULL;

    ESP_LOGI(TAG, "update %s: staging version %s (%u bytes, sha256 %s)", cmd->cmd_id,
             cmd->version[0] != '\0' ? cmd->version : "?", (unsigned)cmd->size, cmd->sha256);
    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_STAGING, 0, NULL);

    /* The slot the download lands in, read BEFORE finish() moves the boot pointer: after
     * that call `next_update` is the slot we are running from, and hashing it would
     * verify the old image and pass. */
    const esp_partition_t *target = esp_ota_get_next_update_partition(NULL);
    if (target == NULL) {
        fail(cmd, "no spare ota slot");
        goto done;
    }
    ESP_LOGI(TAG, "update %s: writing slot %s (%" PRIu32 " bytes)", cmd->cmd_id, target->label,
             target->size);

    resolved = malloc(OTA_RESOLVED_URL_MAX);
    if (resolved == NULL) {
        fail(cmd, "out of memory");
        goto done;
    }
    if (resolve_artifact_url(cmd->url, resolved) != ESP_OK) {
        fail(cmd, "cannot open the artifact");
        goto done;
    }

    esp_http_client_config_t http = {
        .url = resolved,
        /* Same posture as ff_enroll.c / ff_progress.c: the Mozilla bundle IDF ships,
         * because the production origin is Traefik with a Let's Encrypt certificate and
         * there is no private CA to pin. Passing it also satisfies esp_https_ota's
         * server-verification check, which is what lets the plaintext QEMU lab origin
         * work without CONFIG_ESP_HTTPS_OTA_ALLOW_HTTP — a `http://` URL simply never
         * reaches the TLS layer. */
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = OTA_HTTP_TIMEOUT_MS,
        .keep_alive_enable = true,
        .buffer_size = OTA_HTTP_RX_BUFFER,
        .buffer_size_tx = OTA_HTTP_TX_BUFFER,
        /* No redirect setting here on purpose: esp_https_ota follows redirects through
         * esp_http_client_set_redirection() of its own accord, which `disable_auto_redirect`
         * does not govern. `resolved` is already past the one hop we expect, and a store
         * that bounces further would hit the Host-header defect described above — so the
         * fix is to arrive at the final URL, not to try to switch redirects off. */
    };
    esp_https_ota_config_t ota_config = {
        .http_config = &http,
    };

    esp_https_ota_handle_t handle = NULL;
    esp_err_t err = esp_https_ota_begin(&ota_config, &handle);
    if (err != ESP_OK || handle == NULL) {
        /* No URL in the detail: it is a credential, and the reason is never the URL's
         * spelling anyway — it is DNS, a refused connection or a 4xx. */
        fail(cmd, "cannot open the artifact");
        ESP_LOGE(TAG, "esp_https_ota_begin: %s", esp_err_to_name(err));
        goto done;
    }

    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_DOWNLOADING, 0, NULL);

    int last_logged_pct = -10;
    while ((err = esp_https_ota_perform(handle)) == ESP_ERR_HTTPS_OTA_IN_PROGRESS) {
        int so_far = esp_https_ota_get_image_len_read(handle);
        if (cmd->size == 0 || so_far < 0) {
            continue; /* the server did not say how big it is; no percentage to report */
        }
        int pct = (int)(((uint64_t)so_far * 100) / (uint64_t)cmd->size);
        if (pct >= last_logged_pct + 10) {
            last_logged_pct = pct;
            /* Serial only. See property 1 at the top of this file. */
            ESP_LOGI(TAG, "update %s: %d%% (%d bytes)", cmd->cmd_id, pct, so_far);
        }
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

    /* BEFORE finish(), which frees the handle: reading this afterwards is a
     * use-after-free that happens to return a plausible number. */
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

    /* IDF's own validation (magic byte, image length, secure-boot signature when it is
     * on) AND the boot-partition switch. From here on the board is pointed at the new
     * slot, and every failure path below has to put it back. */
    err = esp_https_ota_finish(handle);
    if (err != ESP_OK) {
        if (err == ESP_ERR_OTA_VALIDATE_FAILED) {
            fail(cmd, "image validation failed");
        } else {
            fail(cmd, "cannot finish the update");
            ESP_LOGE(TAG, "esp_https_ota_finish: %s", esp_err_to_name(err));
        }
        /* finish() only sets the boot partition on its own success path, but an image
         * that failed validation must not be left bootable under any reading of that. */
        restore_boot_partition();
        goto done;
    }

    char digest[65];
    err = partition_digest(target, got, digest);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot read %s back: %s", target->label, esp_err_to_name(err));
        restore_boot_partition();
        fail(cmd, "cannot verify the staged image");
        goto done;
    }
    if (strcasecmp(digest, cmd->sha256) != 0) {
        /* Both digests are public facts about the artifact — unlike the URL — so they are
         * printed in full, exactly as the simulator does. */
        ESP_LOGE(TAG, "update %s: sha256 MISMATCH, flash holds %s, the command says %s",
                 cmd->cmd_id, digest, cmd->sha256);
        restore_boot_partition();
        fail(cmd, "sha256 mismatch");
        goto done;
    }
    ESP_LOGI(TAG, "update %s: sha256 %s matches; %s is staged and bootable", cmd->cmd_id, digest,
             target->label);

    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_STAGED, 100, NULL);

    if (!cmd->apply_now) {
        /* `apply: "on_command"`. The device owns the reboot (design/architecture.md
         * principle 5) and R1 ships no `apply` command, so this board stays here — the
         * same place the simulator stops. Deliberately NOT `awaiting_safe_window`: this
         * agent is always-on and has no window to wait for, so reporting one would be a
         * state nothing will ever leave. */
        ESP_LOGW(TAG, "update %s: apply=on_command — staged and waiting (no apply command "
                      "exists before R2)",
                 cmd->cmd_id);
        goto done;
    }

    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_APPLYING, 100, NULL);
    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_REBOOTING, 100, NULL);
    ESP_LOGW(TAG, "update %s: rebooting into %s. The bootloader will run it as "
                  "PENDING_VERIFY; ff_mqtt confirms it only once the retained announce is "
                  "acknowledged, and rolls back otherwise.",
             cmd->cmd_id, target->label);
    /* Nothing is written to NVS about this transaction on purpose: the new image
     * announces itself and that is the whole report. A cmd_id persisted across the reboot
     * is R2's `confirming`/`confirmed` story, and half of it here would be a state
     * machine nobody drives. */
    vTaskDelay(pdMS_TO_TICKS(OTA_PUBLISH_DRAIN_MS));
    esp_restart(); /* does not return */

done:
    free(resolved);
    free(cmd);
    s_running = false;
    vTaskDelete(NULL);
}

esp_err_t ff_ota_start(const ff_ota_cmd_t *cmd)
{
    if (cmd == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_running) {
        /* Refused, not queued. Two concurrent writers to one slot corrupt it, and a
         * deploy that silently waits behind another is a deploy the server cannot
         * explain. The caller publishes `failed` for the NEW cmd_id. */
        return ESP_ERR_INVALID_STATE;
    }

    ff_ota_cmd_t *copy = malloc(sizeof(*copy));
    if (copy == NULL) {
        return ESP_ERR_NO_MEM;
    }
    memcpy(copy, cmd, sizeof(*copy));

    s_running = true;
    if (xTaskCreate(ota_task, "ff_ota", OTA_TASK_STACK, copy, OTA_TASK_PRIO, NULL) != pdPASS) {
        s_running = false;
        free(copy);
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}
