/*
 * Fleetforge agent — R0 skeleton.
 *
 * DELIBERATELY DOES NO NETWORKING. Wi-Fi, SNTP, HTTPS enroll, MQTT, announce and
 * heartbeat are R0-fw-1; a half-implemented network stack in the skeleton is worse
 * than none, because it would be flashed onto boards to prove the *build pipeline*
 * and then behave like an agent that is failing.
 *
 * What it exists for: to be a real, bootable, A/B-capable image, so R0-infra-2's
 * bundle (bootloader + partition table + otadata + app) is something that can be
 * flashed and observed rather than an artefact that merely compiles. What it prints is
 * therefore exactly the set of facts the build pipeline claims to have got right.
 *
 * It does NOT call esp_ota_mark_app_valid_cancel_rollback(). With
 * CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y and no `factory` partition, a serially
 * flashed board's otadata (written from ota_data_initial.bin) is invalid, so the
 * bootloader boots ota_0 in state UNDEFINED and arms no rollback timer. Only an image
 * written BY OTA enters PENDING_VERIFY. R0-fw-1 owns the confirm call, together with
 * the thing worth confirming.
 */

#include <inttypes.h>

#include "esp_app_desc.h"
#include "esp_chip_info.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ff-agent";

/* Slow: nothing is waiting on this and a chatty skeleton is a confusing skeleton. */
#define HEARTBEAT_LOG_INTERVAL_MS 30000

void app_main(void)
{
    const esp_app_desc_t *app = esp_app_get_description();
    esp_chip_info_t chip;
    esp_chip_info(&chip);

    ESP_LOGI(TAG, "fleetforge agent %s (idf %s), built %s %s", app->version,
             esp_get_idf_version(), app->date, app->time);
    ESP_LOGI(TAG, "chip: model=%d cores=%d revision=%d", (int)chip.model, chip.cores,
             chip.revision);

    /* The running slot proves the A/B layout is real: on a freshly flashed board this
     * is ota_0, and after R2's first update it is ota_1. A board reporting `factory`
     * here was flashed with the wrong partition table and can never OTA. */
    const esp_partition_t *running = esp_ota_get_running_partition();
    if (running != NULL) {
        ESP_LOGI(TAG, "running partition: %s type=%d subtype=%d offset=0x%" PRIx32
                      " size=%" PRIu32,
                 running->label, (int)running->type, (int)running->subtype, running->address,
                 running->size);
    } else {
        ESP_LOGE(TAG, "no running partition: this image was not flashed into an OTA slot");
    }

    /* R0-fe-3's flasher writes per-board configuration here. Its absence means the
     * board carries a superseded partition layout, which no OTA can repair. */
    const esp_partition_t *cfg = esp_partition_find_first(
        ESP_PARTITION_TYPE_DATA, (esp_partition_subtype_t)0x40, "ff_cfg");
    if (cfg != NULL) {
        ESP_LOGI(TAG, "config partition ff_cfg at 0x%" PRIx32 " (%" PRIu32 " bytes); R0-fw-1 reads it",
                 cfg->address, cfg->size);
    } else {
        ESP_LOGW(TAG, "no ff_cfg partition: this board cannot be configured at flash time");
    }

    ESP_LOGI(TAG, "no networking in the R0 skeleton — that is R0-fw-1. Idling.");

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(HEARTBEAT_LOG_INTERVAL_MS));
        ESP_LOGI(TAG, "alive: %s on %s", app->version, running ? running->label : "?");
    }
}
