/*
 * ff_store — see ff_store.h. The one rule worth restating at the top of the
 * implementation: a partially written credential returns an error, never
 * ESP_ERR_NVS_NOT_FOUND, because the caller treats NOT_FOUND as "enroll" and enrolling
 * burns a single-use token.
 */

#include "ff_store.h"

#include <stdint.h>
#include <string.h>

#include "esp_log.h"
#include "mbedtls/sha256.h"
#include "nvs.h"
#include "nvs_flash.h"

static const char *TAG = "ff-store";

/* Read one string key into a fixed buffer.
 *
 * `required` distinguishes the two failure modes that matter: a missing REQUIRED key in a
 * namespace that exists at all is a half-written credential (ESP_FAIL), while a missing
 * optional key is simply an older record. ESP_ERR_NVS_INVALID_LENGTH — the value is longer
 * than the buffer — is also corruption, not absence. */
static esp_err_t read_string(nvs_handle_t handle, const char *key, char *out, size_t len,
                             bool required)
{
    size_t actual = len;
    esp_err_t err = nvs_get_str(handle, key, out, &actual);
    if (err == ESP_OK) {
        return ESP_OK;
    }
    if (err == ESP_ERR_NVS_NOT_FOUND && !required) {
        out[0] = '\0';
        return ESP_OK;
    }
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        ESP_LOGE(TAG, "the stored credential is incomplete: '%s' is missing. NOT enrolling "
                      "again — that would burn a second token and still leave this board "
                      "unable to connect. Erase the ff namespace to start over.",
                 key);
        return ESP_FAIL;
    }
    if (err == ESP_ERR_NVS_INVALID_LENGTH) {
        ESP_LOGE(TAG, "the stored '%s' is longer than this firmware's %u-byte buffer", key,
                 (unsigned)len);
        return ESP_FAIL;
    }
    ESP_LOGE(TAG, "cannot read '%s': %s", key, esp_err_to_name(err));
    return err;
}

esp_err_t ff_store_load(ff_cred_t *out)
{
    memset(out, 0, sizeof(*out));

    nvs_handle_t handle;
    esp_err_t err = nvs_open(FF_STORE_NAMESPACE, NVS_READONLY, &handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        /* The namespace has never been written: a factory-fresh board. The ONLY path that
         * leads to enrollment. */
        return ESP_ERR_NVS_NOT_FOUND;
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot open nvs namespace '%s': %s", FF_STORE_NAMESPACE,
                 esp_err_to_name(err));
        return err;
    }

    /* The password is probed first and separately: if it alone is absent the namespace
     * exists for some other reason and there is no credential here at all, which IS the
     * enroll case. Any other combination of missing keys is a torn write. */
    size_t pass_len = 0;
    err = nvs_get_str(handle, FF_STORE_KEY_MQTT_PASS, NULL, &pass_len);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        nvs_close(handle);
        return ESP_ERR_NVS_NOT_FOUND;
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot size the stored credential: %s", esp_err_to_name(err));
        nvs_close(handle);
        return err;
    }

    esp_err_t rc = ESP_OK;
    if ((err = read_string(handle, FF_STORE_KEY_MQTT_PASS, out->mqtt_pass,
                           sizeof(out->mqtt_pass), true)) != ESP_OK) {
        rc = err;
    } else if ((err = read_string(handle, FF_STORE_KEY_MQTT_USER, out->mqtt_user,
                                  sizeof(out->mqtt_user), true)) != ESP_OK) {
        rc = err;
    } else if ((err = read_string(handle, FF_STORE_KEY_DEVICE_ID, out->device_id,
                                  sizeof(out->device_id), true)) != ESP_OK) {
        rc = err;
    } else {
        /* Both additive, both diagnostics rather than contract: an older record without
         * them is still a perfectly good credential. */
        (void)read_string(handle, FF_STORE_KEY_API_BASE, out->api_base, sizeof(out->api_base),
                          false);
        (void)read_string(handle, FF_STORE_KEY_ENROLLED_AT, out->enrolled_at,
                          sizeof(out->enrolled_at), false);
    }
    nvs_close(handle);

    if (rc != ESP_OK) {
        memset(out, 0, sizeof(*out)); /* never hand back half a credential */
        return rc;
    }

    /* Lengths, never values. `mqtt_user` is device_id, which is public; the password is
     * the fleet credential and does not appear in a log line even truncated. */
    ESP_LOGI(TAG, "reusing the stored credential (no enrollment): %s, issued %s by %s",
             out->mqtt_user, out->enrolled_at[0] ? out->enrolled_at : "at an unrecorded time",
             out->api_base[0] ? out->api_base : "an unrecorded server");
    return ESP_OK;
}

esp_err_t ff_store_save(const ff_cred_t *cred)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open(FF_STORE_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot open nvs for writing: %s", esp_err_to_name(err));
        return err;
    }

    /* Order matters only in that the commit is atomic: nvs_commit() is what makes the
     * whole set durable, so a power cut before it leaves the namespace as it was. */
    err = nvs_set_str(handle, FF_STORE_KEY_DEVICE_ID, cred->device_id);
    if (err == ESP_OK) {
        err = nvs_set_str(handle, FF_STORE_KEY_MQTT_USER, cred->mqtt_user);
    }
    if (err == ESP_OK) {
        err = nvs_set_str(handle, FF_STORE_KEY_MQTT_PASS, cred->mqtt_pass);
    }
    if (err == ESP_OK && cred->api_base[0] != '\0') {
        err = nvs_set_str(handle, FF_STORE_KEY_API_BASE, cred->api_base);
    }
    if (err == ESP_OK && cred->enrolled_at[0] != '\0') {
        err = nvs_set_str(handle, FF_STORE_KEY_ENROLLED_AT, cred->enrolled_at);
    }
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);

    if (err != ESP_OK) {
        /* The loudest line in the agent, and it deserves to be: the password exists in
         * exactly one place in the universe — the HTTP response that has just been parsed
         * and is about to be freed. If it is not in flash now, it is gone, and this board
         * needs a fresh token to get another one. */
        ESP_LOGE(TAG, "CANNOT STORE THE CREDENTIAL (%s). It exists only in RAM and will be "
                      "lost on reset; this board will need a new enrollment token.",
                 esp_err_to_name(err));
        return err;
    }

    ESP_LOGI(TAG, "credential stored in NVS");
    return ESP_OK;
}

/* The first 8 bytes of sha256(token), as 16 lowercase hex characters.
 *
 * sha256 and not a CRC because `mbedtls` is already a REQUIRES and esp-tls already links
 * sha256, so this costs ~0 bytes and invites none of the questions a CRC would. Hex by
 * hand rather than snprintf: the digits cannot be truncated, so there is no format
 * warning for -Werror to turn into a build failure, and no stdio in this file. */
static esp_err_t token_fingerprint(const char *token, char out[FF_CRED_MAX_TOKEN_FP])
{
    static const char HEX[] = "0123456789abcdef";
    uint8_t digest[32];
    if (mbedtls_sha256((const unsigned char *)token, strlen(token), digest, 0) != 0) {
        return ESP_FAIL;
    }
    for (size_t i = 0; i < (FF_CRED_MAX_TOKEN_FP - 1) / 2; i++) {
        out[i * 2] = HEX[digest[i] >> 4];
        out[i * 2 + 1] = HEX[digest[i] & 0x0f];
    }
    out[FF_CRED_MAX_TOKEN_FP - 1] = '\0';
    return ESP_OK;
}

esp_err_t ff_store_sync_token(const char *token)
{
    /* A config with no token never erases anything. `just agent-qemu-smoke` and any
     * diagnostic flash write exactly that, and throwing away a working credential because
     * the operator flashed a tokenless config is the same mistake ff_store_matches_api_base
     * already refuses to make for a hostname change. */
    if (token == NULL || token[0] == '\0') {
        return ESP_OK;
    }

    char want[FF_CRED_MAX_TOKEN_FP];
    esp_err_t err = token_fingerprint(token, want);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot fingerprint the ff_cfg enrollment token; leaving the stored "
                      "credential alone");
        return err;
    }

    nvs_handle_t handle;
    /* NVS_READWRITE creates the namespace when it is absent, and that is intended: a
     * factory-fresh board records its fingerprint on the first boot, so the NEXT different
     * token erases. Safe because ff_store_load() probes `mqtt_pass`, not the namespace — a
     * namespace holding only `tok_fp` still reports "nothing stored, enroll". */
    err = nvs_open(FF_STORE_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot open nvs namespace '%s' to check the enrollment token: %s",
                 FF_STORE_NAMESPACE, esp_err_to_name(err));
        return err;
    }

    char have[FF_CRED_MAX_TOKEN_FP];
    size_t have_len = sizeof(have);
    esp_err_t read_err = nvs_get_str(handle, FF_STORE_KEY_TOKEN_FP, have, &have_len);

    if (read_err == ESP_OK && strcmp(have, want) == 0) {
        /* The common case, every boot of a settled board: nothing to do and nothing to say. */
        ESP_LOGD(TAG, "the ff_cfg enrollment token is the one this credential was issued "
                      "against (%s)",
                 want);
        nvs_close(handle);
        return ESP_OK;
    }

    if (read_err == ESP_ERR_NVS_NOT_FOUND) {
        /* Adopt, never erase. This is also the one-time migration case: a board enrolled by
         * a firmware older than S0-fw-4 has a credential and no fingerprint.
         *
         * The rejected alternative — "absent fingerprint means erase" — is a landmine. At R2
         * an OTA replaces the agent WITHOUT writing a new ff_cfg, so the first post-OTA boot
         * of every board in the fleet would find no fingerprint, erase its credential and
         * re-enroll with the long-spent token still sitting in ff_cfg: 409, park(), a
         * fleet-wide brick delivered by an update. The cost of adopting instead is one extra
         * flash for the handful of boards enrolled before today. */
        size_t pass_len = 0;
        bool credentialed = nvs_get_str(handle, FF_STORE_KEY_MQTT_PASS, NULL, &pass_len) == ESP_OK;
        err = nvs_set_str(handle, FF_STORE_KEY_TOKEN_FP, want);
        if (err == ESP_OK) {
            err = nvs_commit(handle);
        }
        nvs_close(handle);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "cannot record the enrollment token fingerprint: %s — this board "
                          "will re-check it on the next boot",
                     esp_err_to_name(err));
            return err;
        }
        if (credentialed) {
            ESP_LOGW(TAG, "this board enrolled before the token fingerprint existed, so there "
                          "is nothing to compare its credential against; KEEPING it and "
                          "recording the current token as %s. If you just re-flashed it with a "
                          "fresh token expecting it to re-enroll, flash it once more — the next "
                          "token will differ from the one now recorded and the credential will "
                          "be cleared then.",
                     want);
        } else {
            ESP_LOGI(TAG, "recording the ff_cfg enrollment token as %s; nothing was stored to "
                          "invalidate",
                     want);
        }
        return ESP_OK;
    }

    /* Either a genuinely different token, or a fingerprint we could not read — too long for
     * the buffer, wrong type, a torn write. An unreadable fingerprint is not proof that the
     * stored credential belongs to this token, so it counts as a mismatch. */
    if (read_err != ESP_OK) {
        ESP_LOGW(TAG, "the stored enrollment token fingerprint is unreadable (%s); treating it "
                      "as a mismatch",
                 esp_err_to_name(read_err));
        have[0] = '\0';
    }

    ESP_LOGW(TAG,
             "the ff_cfg enrollment token has changed (%s -> %s): erasing the stored credential "
             "so this board re-enrolls. ONLY the '%s' namespace is erased — the cached RF "
             "calibration lives in the 'phy' namespace of the same partition and must survive "
             "(S0-fw-4).",
             have[0] != '\0' ? have : "unreadable", want, FF_STORE_NAMESPACE);

    /* Order is load-bearing: nvs_erase_all() erases every key in the namespace, `tok_fp`
     * included, so the fingerprint is written AFTER it. One commit makes the pair atomic —
     * a power cut before it leaves the board exactly as it was and the next boot retries. */
    err = nvs_erase_all(handle);
    if (err == ESP_OK) {
        err = nvs_set_str(handle, FF_STORE_KEY_TOKEN_FP, want);
    }
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot clear the stored credential (%s). This board will try to reuse a "
                      "credential that no longer matches its enrollment token; re-flash it or "
                      "erase the '%s' namespace by hand.",
                 esp_err_to_name(err), FF_STORE_NAMESPACE);
        return err;
    }
    return ESP_OK;
}

bool ff_store_matches_api_base(const ff_cred_t *cred, const char *api_base)
{
    if (cred->api_base[0] == '\0' || api_base == NULL) {
        return true; /* nothing recorded to disagree with */
    }
    return strcmp(cred->api_base, api_base) == 0;
}
