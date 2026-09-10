/*
 * ff_store — see ff_store.h. The one rule worth restating at the top of the
 * implementation: a partially written credential returns an error, never
 * ESP_ERR_NVS_NOT_FOUND, because the caller treats NOT_FOUND as "enroll" and enrolling
 * burns a single-use token.
 */

#include "ff_store.h"

#include <string.h>

#include "esp_log.h"
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

bool ff_store_matches_api_base(const ff_cred_t *cred, const char *api_base)
{
    if (cred->api_base[0] == '\0' || api_base == NULL) {
        return true; /* nothing recorded to disagree with */
    }
    return strcmp(cred->api_base, api_base) == 0;
}
