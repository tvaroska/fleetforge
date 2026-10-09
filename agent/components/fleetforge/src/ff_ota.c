/*
 * ff_ota — download, write the inactive slot, verify, apply. See ff_ota.h.
 *
 * The wire behaviour is not invented here either: `src/fleetforge/simulator/device.py`
 * already walks `staging → downloading → verifying → staged → applying → rebooting`, the
 * ingestor parses what it publishes, and this file repeats that walk on real flash. What
 * happens after the reboot (`confirming` → `confirmed` | `rolling_back` → `rolled_back`) is
 * reported by ff_mqtt.c from the record this file writes at `staged` (ff_txn.h). Seven
 * properties are worth stating out loud, because each one is a silent wrong answer rather
 * than an error:
 *
 * 1. **`downloading` is published exactly once**, not per chunk. `deploy_events` is a log
 *    of transitions, not a progress feed — `deploys.record_observed_status` dedups on
 *    `(device_id, cmd_id, state)`, so a per-chunk publish writes nothing and costs the
 *    broker a message per 4 KB. Progress goes to the serial console instead.
 * 2. **The digest is checked by reading the slot BACK after the last write, and BEFORE
 *    esp_https_ota_finish() moves the boot pointer.** Hashing the stream as it arrives looks
 *    equivalent and is not: it is a hash of what we meant to write, and reading the slot
 *    back is the only check that covers the flash write itself, which is the part that can
 *    fail. Reading back BEFORE finish() is possible because, without flash encryption,
 *    `esp_ota_write()` writes every byte it is handed straight to the partition
 *    (IDF v5.5.5 `app_update/esp_ota_ops.c`: the `partial_data[16]` buffer exists only inside
 *    `if (esp_flash_encryption_enabled())`, and what it holds back is the TRAILING partial
 *    16-byte block, not the header), and `esp_https_ota.c::_ota_write()` hands every chunk to
 *    it unbuffered. R1-fw-1 believed `esp_ota_write` withheld the first 16 bytes of the
 *    header until the end; that is not what v5.5.5 does (DECISIONS 2026-10-03, R2-fw-1).
 *    The #error below makes flash encryption a build failure, so the premise cannot rot.
 *    A second #error makes a build without CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE fail too
 *    (R3-fw-5): this OTA path is never built with a config whose bootloader cannot roll back.
 * 3. **A mismatch never moves the boot pointer.** It is caught before finish(), so the
 *    image is abandoned with esp_https_ota_abort() and otadata is never written. Before
 *    R2-fw-1 the switch came first and was undone afterwards, and in between — the whole
 *    read-back of up to 1.9 MB — a power cut or a watchdog booted an unverified image. The
 *    only path that can still need restore_boot_partition() is a failed finish().
 * 4. **The URL is never logged.** It is the authorization (R1-be-3: "signed URL never
 *    persisted, never logged"), so it does not appear in a log line, in a `detail`, or
 *    truncated "just for debugging". Everything else about a failure is said plainly.
 * 5. **One slot, chosen once, and never one the boot pointer names** (R2-fw-2).
 *    choose_target_slot() picks the slot, and the same pointer is handed to esp_https_ota
 *    (`.partition.staging`), hashed, switched to and recorded. Before any I/O it refuses a
 *    stage while the running image is still PENDING_VERIFY, and while an earlier staged
 *    image waits for a reboot (boot != running). The second refusal is the one that matters.
 *    IDF's esp_rewrite_ota_data() picks the new seq with `while (seq > id+1 + i*N) i++`,
 *    and equality stops that loop. So switching to the slot the active entry ALREADY names
 *    rewrites the same seq into the other otadata sector, which is the running image's
 *    entry. Both sectors then name the staged slot, and a rollback has nowhere to go
 *    (DECISIONS 2026-10-03, R2-fw-2).
 * 6. **A download that stops making progress ends** (R2-fw-5). IDF turns a read timeout
 *    with nothing read into ESP_ERR_HTTPS_OTA_IN_PROGRESS, so a peer that stays connected
 *    and silent would keep the perform loop, and `s_running`, alive for ever. If the image
 *    length read has not grown for OTA_STALL_MS (wall clock, from `downloading`), the
 *    download is abandoned with esp_https_ota_abort() and reported `failed` /
 *    `download stalled`; the slot is free for the next `stage`. On metal TCP keepalive
 *    usually ends a dead radio first (`download failed`). Known residual: IDF's
 *    read_header() loops inside the FIRST perform() until it has 1 KB of body, so a peer
 *    silent before that is not seen here (DECISIONS 2026-10-03, R2-fw-5).
 * 7. **A re-delivered stage is not a second update** (R2-fw-6). The broker and a re-POST
 *    (`reused: true`) can hand the board the same cmd_id again after other commands, past
 *    ff_mqtt's one-id dedupe. ff_ota_is_handling() answers whether that id is the download
 *    in flight or the image staged and waiting, and the command seam drops it without a
 *    status. Before, it was refused against itself (`failed`), which ended the server's row
 *    while the update carried on (DECISIONS 2026-10-03, R2-fw-6).
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
#include "esp_timer.h"
#include "ff_mqtt_internal.h"
#include "ff_txn.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/sha256.h"
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

#define OTA_HTTP_TIMEOUT_MS 20000
/* How long the image may stop growing before the download is abandoned (property 6).
 * Wall clock, not a count of empty reads, so it keeps its meaning if OTA_HTTP_TIMEOUT_MS
 * changes, and it also covers the wait for the first body byte. It is checked each time
 * perform() returns, which while stalled is every OTA_HTTP_TIMEOUT_MS, so the abort lands
 * 60-80 s after the last byte. 60 s, not less: on metal TCP keepalive (IDF 5 s idle, 5 s
 * interval, 3 probes) ends a socket whose radio is really gone in ~20 s, and it should stay
 * the first responder; this guard is for a far end that is alive and silent (R2-fw-5). */
#define OTA_STALL_MS 60000
_Static_assert(OTA_STALL_MS >= 2 * OTA_HTTP_TIMEOUT_MS,
               "one slow read must never trip the stall guard");
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

/* The cmd_id of the update `s_running` is about (R2-fw-6), for ff_ota_is_handling().
 * Single writer: ff_ota_start(), immediately BEFORE `s_running = true`. Never cleared and
 * never touched by ota_task, so it is meaningful only while `s_running` is true. Both
 * ff_ota_start() and ff_ota_is_handling() run on the esp-mqtt task (on_stage() in the event
 * handler), so the only variable shared with ota_task is `s_running` itself, which ota_task
 * only ever moves true -> false at `done:`. If that happens between the predicate and
 * ff_ota_start(), the stage simply runs again — the pre-existing behaviour for a finished
 * cmd. No mutex. Not the task's heap copy of the cmd either: that is freed at `done:`. */
static char s_running_cmd_id[sizeof(((ff_ota_cmd_t *)0)->cmd_id)];
_Static_assert(sizeof(s_running_cmd_id) == FF_TXN_MAX_CMD_ID,
               "s_running_cmd_id, ff_ota_cmd_t.cmd_id and the ff_txn record hold the same ids");

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

/* Point the boot partition at the running slot again, in case a FAILED
 * esp_https_ota_finish() left it anywhere else. Since R2-fw-1 a sha256 mismatch never gets
 * here — it is caught before finish() and nothing was moved (property 3) — so this is the
 * belt-and-braces branch for an image IDF itself refused. It is also the only call to
 * esp_ota_set_boot_partition() in this file. Logged at ERROR either way — an operator needs
 * to know a board is carrying a rejected image in its spare slot.
 *
 * It writes otadata ONLY if the pointer actually moved (R2-fw-2). finish() reaches
 * esp_ota_set_boot_partition() only after esp_ota_end() succeeded, and choose_target_slot()
 * guarantees boot == running when the stage began. So a failed finish() leaves
 * boot == running in every case short of a torn otadata write. "Putting it back" anyway is
 * not a no-op. It is IDF's equal-seq rewrite (property 5): a duplicate entry for the
 * running slot, in state NEW, written over the previous image's entry, and depending on
 * sector parity the known-good image then boots PENDING_VERIFY. */
static void restore_boot_partition(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    if (running == NULL) {
        ESP_LOGE(TAG, "cannot read the running partition to undo the boot switch — this "
                      "board may reboot into an image that failed verification");
        return;
    }
    const esp_partition_t *boot = esp_ota_get_boot_partition();
    if (boot == running) {
        ESP_LOGW(TAG, "boot partition still names %s; nothing to undo — otadata was not "
                      "touched",
                 running->label);
        return;
    }
    if (boot == NULL) {
        ESP_LOGE(TAG, "cannot read the boot partition; putting it back to %s anyway",
                 running->label);
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

/* Same spelling as agent_main.c's boot-facts line, so a refusal and the boot log read
 * alike. */
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

/* The ONE place the slot is chosen (property 5). Returns the slot to write, or NULL after
 * it has published `failed` itself. Everything here is a read: no otadata write, no erase,
 * no fetch, and no ff_txn call. A refused stage leaves an earlier stage's transaction
 * record exactly as it was, because that image still boots at the next reset and reports
 * against its own cmd_id.
 *
 * The order is load-bearing. A confirming image is refused first, then a waiting staged
 * image, and only then is the next slot computed. */
static const esp_partition_t *choose_target_slot(const ff_ota_cmd_t *cmd)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    if (running == NULL) {
        fail(cmd, "cannot read the running partition");
        return NULL;
    }

    /* 1. The running image must be confirmed. IDF's esp_ota_begin() refuses this too, but
     *    only inside the first esp_https_ota_perform(): after `downloading` was published
     *    and the signed URL was fetched, and reported as "download failed". Refuse ONLY on
     *    exactly PENDING_VERIFY. A failed read (ESP_ERR_NOT_FOUND on a serially flashed
     *    board, whose otadata is all 0xFF) is allowed, or no freshly USB-flashed board could
     *    ever be deployed to. IDF's own check stays the backstop. */
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

    /* 2. Never write a slot the boot pointer names. After an `apply: "on_command"` stage
     *    the boot pointer names the staged slot, and that slot is also the next update
     *    slot. Writing it would erase an image otadata already points at, and finish()
     *    would then rewrite the SAME seq over the running image's entry (property 5). So
     *    refuse until the board reboots into what it staged. */
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

    /* 3. The slot after the running one, sanity-checked. Belt and braces: IDF refuses the
     *    running slot and non-OTA slots too, but only after this function has returned. */
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

    /* The slot the download lands in, chosen ONCE and before any I/O (property 5). The
     * same pointer goes to esp_https_ota below, is hashed, switched to and recorded. It is
     * read BEFORE finish() moves the boot pointer: after that call `next_update` is the slot
     * we are running from, and hashing it would verify the old image and pass. */
    const esp_partition_t *target = choose_target_slot(cmd);
    if (target == NULL) {
        goto done;
    }
    ESP_LOGI(TAG, "update %s: writing slot %s at 0x%08" PRIx32 " (%" PRIu32 " bytes)",
             cmd->cmd_id, target->label, target->address, target->size);
    /* An artifact that cannot fit is refused BEFORE anything is fetched or erased.
     * Otherwise it is discovered when esp_ota_write() runs off the end of the slot — after
     * the slot, which holds the previous image, has been erased. `size == 0` means the
     * server did not say; the digest still covers whatever arrives. */
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
        /* THE slot, not esp_https_ota's own pick. Left NULL it calls
         * esp_ota_get_next_update_partition() again, independently, and nothing would
         * hold that equal to the `target` hashed and recorded here. `.final` stays NULL,
         * which IDF reads as "same as staging". */
        .partition = {.staging = target},
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
    /* The stall clock (property 6) starts at `downloading`, so the wait for the first body
     * byte counts too. */
    int last_len = 0;
    int64_t last_progress_us = esp_timer_get_time();
    bool stalled = false;
    while ((err = esp_https_ota_perform(handle)) == ESP_ERR_HTTPS_OTA_IN_PROGRESS) {
        int so_far = esp_https_ota_get_image_len_read(handle);
        /* BEFORE the size-less `continue` below, or a command without a size would never
         * be guarded. */
        int64_t now_us = esp_timer_get_time();
        if (so_far > last_len) {
            last_len = so_far;
            last_progress_us = now_us;
        } else if (now_us - last_progress_us >= (int64_t)OTA_STALL_MS * 1000) {
            stalled = true;
            break;
        }
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

    /* BEFORE the `download failed` branch: after the break `err` is still IN_PROGRESS,
     * which would read as a failed download. Same posture as that branch: abort, never
     * finish(), so otadata and the transaction record are not touched. */
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

    /* THE check, and it happens while the boot pointer still names the running slot.
     * perform() has returned ESP_OK and every one of the `got` bytes is on flash in
     * `target` already (property 2), so reading it back here hashes exactly what a reboot
     * would run. A failure on this side of finish() is an esp_https_ota_abort(): otadata
     * has not been touched, so there is nothing to undo.
     *
     * There is deliberately NO second hash after finish(). Without flash encryption
     * esp_ota_end() writes nothing more to the slot, and what it does do —
     * ota_verify_partition(), which re-reads the image and checks IDF's own appended
     * SHA-256 and checksum — is a second, independent read-back anyway. */
    char digest[65];
    err = partition_digest(target, got, digest);
    if (err != ESP_OK) {
        esp_https_ota_abort(handle);
        ESP_LOGE(TAG, "cannot read %s back: %s", target->label, esp_err_to_name(err));
        fail(cmd, "cannot verify the staged image");
        goto done;
    }
    /* Exact comparison: the command seam (ff_mqtt.c::on_stage) only lets a 64-character
     * lowercase digest through, and hex_encode() emits lowercase. */
    if (strcmp(digest, cmd->sha256) != 0) {
        esp_https_ota_abort(handle);
        /* Both digests are public facts about the artifact — unlike the URL — so they are
         * printed in full, exactly as the simulator does. */
        ESP_LOGE(TAG, "update %s: sha256 MISMATCH, flash holds %s, the command says %s — "
                      "the boot partition was never moved",
                 cmd->cmd_id, digest, cmd->sha256);
        fail(cmd, "sha256 mismatch");
        goto done;
    }
    ESP_LOGI(TAG, "update %s: sha256 %s matches what is on flash in %s; switching the boot "
                  "partition",
             cmd->cmd_id, digest, target->label);

    /* IDF's own validation (magic byte, image length, appended SHA-256, secure-boot
     * signature when it is on) AND the boot-partition switch. finish() frees the handle
     * on every path, success or not. */
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

    /* The transaction is live from HERE, not from `applying`: finish() has just moved the
     * boot pointer to `target`, so any reset from now on — our esp_restart(), a power
     * cut, a watchdog — boots the new image, including after `apply: "on_command"`. The
     * record is what lets that image (or the one the board falls back to) report the
     * outcome against this cmd_id. AFTER finish(), never before: a record saved ahead of a
     * finish() that then fails would name a slot that is not bootable. Written BEFORE the
     * "staged and bootable" line below, because that line is what an operator waits for
     * before pulling the plug.
     * Report-only: a failed save loses the outcome report, never the deploy or the
     * rollback, so the update carries on. */
    esp_err_t txn_err = ff_txn_save(cmd->cmd_id, target->address);
    if (txn_err != ESP_OK) {
        ESP_LOGE(TAG, "update %s: cannot record the transaction (%s) — the outcome will not "
                      "be reported after the reboot",
                 cmd->cmd_id, esp_err_to_name(txn_err));
    }

    ESP_LOGI(TAG, "update %s: %s is staged and bootable", cmd->cmd_id, target->label);

    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_STAGED, 100, NULL);

    if (!cmd->apply_now) {
        /* `apply: "on_command"`. The device owns the reboot (design/architecture.md
         * principle 5) and R1 ships no `apply` command, so this board stays here — the
         * same place the simulator stops. Deliberately NOT `awaiting_safe_window`: this
         * agent is always-on and has no window to wait for, so reporting one would be a
         * state nothing will ever leave. Until it reboots, a further `stage` is refused
         * (choose_target_slot(): the boot pointer no longer names the running slot). */
        ESP_LOGW(TAG, "update %s: apply=on_command — staged and waiting (no apply command "
                      "exists before R2; a further stage is refused until this board "
                      "reboots)",
                 cmd->cmd_id);
        goto done;
    }

    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_APPLYING, 100, NULL);
    ff_mqtt_publish_status(cmd->cmd_id, FF_STATUS_REBOOTING, 100, NULL);
    ESP_LOGW(TAG, "update %s: rebooting into %s. The bootloader will run it as "
                  "PENDING_VERIFY; ff_mqtt confirms it only once the retained announce is "
                  "acknowledged, and rolls back otherwise.",
             cmd->cmd_id, target->label);
    /* The transaction record was written at `staged` (ff_txn), so the image that comes
     * up — or the one the bootloader falls back to — reports `confirming`/`confirmed` or
     * `rolled_back` against this cmd_id. That is ff_mqtt.c's job, not this file's. */
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
         * explain. This is a DIFFERENT cmd_id: the same one was dropped by
         * ff_ota_is_handling(). The caller publishes `failed` for the NEW cmd_id. */
        return ESP_ERR_INVALID_STATE;
    }

    ff_ota_cmd_t *copy = malloc(sizeof(*copy));
    if (copy == NULL) {
        return ESP_ERR_NO_MEM;
    }
    memcpy(copy, cmd, sizeof(*copy));

    /* Before `s_running = true`, so the predicate never sees the flag with a stale id.
     * cmd_id is NUL-terminated and fits: on_stage() refused a longer one. */
    strlcpy(s_running_cmd_id, cmd->cmd_id, sizeof(s_running_cmd_id));
    s_running = true;
    if (xTaskCreate(ota_task, "ff_ota", OTA_TASK_STACK, copy, OTA_TASK_PRIO, NULL) != pdPASS) {
        s_running = false;
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
    /* 1. In flight. A running update for a DIFFERENT id answers false right here and never
     *    falls through: between finish() and `done:` the boot pointer already names the
     *    new slot and the ff_txn record holds the RUNNING update's id, and that id is the
     *    only one this board is carrying out. */
    if (s_running) {
        return strcmp(cmd_id, s_running_cmd_id) == 0;
    }
    /* 2. Staged by `apply: "on_command"` and waiting for a reboot: the boot pointer names a
     *    slot that is not the running one, and the record ota_task saved at `staged` is for
     *    this id. Both go false by themselves: once the staged image boots, boot == running,
     *    and a reset mid-download never wrote a record, so a reused id after a reset runs
     *    again (the recovery path). Read-only — no publish, no otadata write, no ff_txn
     *    save or clear. ff_txn_load() does clear a TORN record (missing a key), as it does
     *    at boot; that transaction's report was already lost, so it changes nothing. */
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
