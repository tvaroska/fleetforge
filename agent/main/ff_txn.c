/*
 * ff_txn — see ff_txn.h. One static mutex serialises the two writers: ff_ota's task saves
 * a record, the esp-mqtt task clears one, and a save racing a clear is exactly the case
 * ff_txn_clear_if() exists for.
 */

#include "ff_txn.h"

#include <inttypes.h>
#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"

static const char *TAG = "ff-txn";

static StaticSemaphore_t s_lock_buf;
static SemaphoreHandle_t s_lock;

void ff_txn_init(void)
{
    if (s_lock == NULL) {
        s_lock = xSemaphoreCreateMutexStatic(&s_lock_buf);
    }
}

static bool lock(void)
{
    if (s_lock == NULL) {
        ESP_LOGE(TAG, "ff_txn_init() was not called — the transaction record is unavailable");
        return false;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    return true;
}

static void unlock(void)
{
    xSemaphoreGive(s_lock);
}

/* Erase this namespace's keys — never the partition. Caller holds the lock and the handle
 * is open read-write. */
static esp_err_t erase_locked(nvs_handle_t handle)
{
    esp_err_t err = nvs_erase_all(handle);
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    return err;
}

esp_err_t ff_txn_save(const char *cmd_id, uint32_t target_addr)
{
    if (cmd_id == NULL || cmd_id[0] == '\0' || strlen(cmd_id) >= FF_TXN_MAX_CMD_ID) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!lock()) {
        return ESP_ERR_INVALID_STATE;
    }

    nvs_handle_t handle;
    esp_err_t err = nvs_open(FF_TXN_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        unlock();
        ESP_LOGE(TAG, "cannot open nvs namespace '%s': %s", FF_TXN_NAMESPACE,
                 esp_err_to_name(err));
        return err;
    }

    /* Erase first, then the two keys: NVS writes each key as it is set, so this order is
     * what makes every torn state read back as "no record" rather than as a mismatched
     * pair. */
    err = nvs_erase_all(handle);
    if (err == ESP_OK) {
        err = nvs_set_u32(handle, FF_TXN_KEY_TARGET, target_addr);
    }
    if (err == ESP_OK) {
        err = nvs_set_str(handle, FF_TXN_KEY_CMD_ID, cmd_id);
    }
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    unlock();

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot save the transaction record for %s: %s", cmd_id,
                 esp_err_to_name(err));
        return err;
    }
    ESP_LOGI(TAG, "transaction %s recorded (target slot at 0x%08" PRIx32 ")", cmd_id,
             target_addr);
    return ESP_OK;
}

esp_err_t ff_txn_load(ff_txn_t *out)
{
    memset(out, 0, sizeof(*out));
    if (!lock()) {
        return ESP_ERR_INVALID_STATE;
    }

    /* Read-only first: the common boot has no record, and should write nothing. */
    nvs_handle_t handle;
    esp_err_t err = nvs_open(FF_TXN_NAMESPACE, NVS_READONLY, &handle);
    if (err != ESP_OK) {
        unlock();
        if (err != ESP_ERR_NVS_NOT_FOUND) {
            ESP_LOGE(TAG, "cannot open nvs namespace '%s': %s", FF_TXN_NAMESPACE,
                     esp_err_to_name(err));
        }
        return err;
    }

    size_t len = sizeof(out->cmd_id);
    esp_err_t id_err = nvs_get_str(handle, FF_TXN_KEY_CMD_ID, out->cmd_id, &len);
    esp_err_t addr_err = nvs_get_u32(handle, FF_TXN_KEY_TARGET, &out->target_addr);
    nvs_close(handle);

    esp_err_t rc = ESP_OK;
    if (id_err == ESP_ERR_NVS_NOT_FOUND && addr_err == ESP_ERR_NVS_NOT_FOUND) {
        rc = ESP_ERR_NVS_NOT_FOUND; /* the namespace exists but holds nothing */
    } else if (id_err != ESP_OK || addr_err != ESP_OK || out->cmd_id[0] == '\0') {
        /* A torn save, or a value this firmware cannot hold. There is no transaction we
         * could honestly report against, so it is discarded rather than guessed at. */
        ESP_LOGW(TAG, "incomplete transaction record (cmd_id: %s, target: %s) — discarded",
                 esp_err_to_name(id_err), esp_err_to_name(addr_err));
        esp_err_t erase_err = nvs_open(FF_TXN_NAMESPACE, NVS_READWRITE, &handle);
        if (erase_err == ESP_OK) {
            erase_err = erase_locked(handle);
            nvs_close(handle);
        }
        if (erase_err != ESP_OK) {
            ESP_LOGE(TAG, "cannot discard it: %s", esp_err_to_name(erase_err));
        }
        rc = ESP_ERR_NVS_NOT_FOUND;
    }
    unlock();

    if (rc != ESP_OK) {
        memset(out, 0, sizeof(*out));
    }
    return rc;
}

esp_err_t ff_txn_clear_if(const char *cmd_id)
{
    if (cmd_id == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!lock()) {
        return ESP_ERR_INVALID_STATE;
    }

    nvs_handle_t handle;
    esp_err_t err = nvs_open(FF_TXN_NAMESPACE, NVS_READWRITE, &handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        unlock();
        return ESP_OK; /* never written: nothing to clear */
    }
    if (err != ESP_OK) {
        unlock();
        ESP_LOGE(TAG, "cannot open nvs namespace '%s': %s", FF_TXN_NAMESPACE,
                 esp_err_to_name(err));
        return err;
    }

    char stored[FF_TXN_MAX_CMD_ID] = {0};
    size_t len = sizeof(stored);
    err = nvs_get_str(handle, FF_TXN_KEY_CMD_ID, stored, &len);
    esp_err_t rc;
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        rc = ESP_OK;
    } else if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot read the transaction record: %s", esp_err_to_name(err));
        rc = err;
    } else if (strcmp(stored, cmd_id) != 0) {
        /* A newer transaction saved its record after this one was reported. Its outcome
         * is still owed, so its record stays. */
        ESP_LOGI(TAG, "transaction %s is already superseded by %s — record kept", cmd_id,
                 stored);
        rc = ESP_ERR_INVALID_STATE;
    } else {
        rc = erase_locked(handle);
        if (rc == ESP_OK) {
            ESP_LOGI(TAG, "transaction %s closed — record cleared", cmd_id);
        } else {
            ESP_LOGE(TAG, "cannot clear the record for %s: %s (the next boot reports it "
                          "again, which the server deduplicates)",
                     cmd_id, esp_err_to_name(rc));
        }
    }
    nvs_close(handle);
    unlock();
    return rc;
}
