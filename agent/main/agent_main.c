/*
 * Fleetforge agent — R0: connect-only.
 *
 * netif -> SNTP -> HTTPS enroll -> MQTT announce/presence/heartbeat, and nothing else.
 * It does not apply firmware: `capabilities` is an empty array and a `dn/cmd` is logged
 * rather than executed, because a board that claims a capability it does not have gets
 * offered a deployment it cannot perform. OTA is R2.
 *
 * Two rules the boot sequence below exists to keep, both of which cost real money to get
 * wrong:
 *
 *  - **A credential in NVS means never enroll again.** Enrollment tokens are single-use;
 *    a board that re-enrolls on every boot burns the operator's token pool and, after the
 *    first burn, cannot come back. `ff_store_load()` therefore distinguishes "absent" from
 *    "corrupt", and only "absent" enrolls (`simulator/state.py` states the same rule for
 *    the simulator).
 *  - **The clock is set before the first TLS handshake.** An ESP32 boots at epoch 0, and
 *    every certificate on earth is then "not yet valid" — an error that reads like a
 *    broken server (`spec/device-protocol.md` -> Clock).
 *
 * Nothing here reboots on failure. A board that reboot-loops on a bad access point, a
 * revoked token or a wrong password is indistinguishable from a hardware fault, and each
 * reset throws away the serial log that says which one it is. Every failure path either
 * retries on a capped backoff or parks with one line saying what to fix.
 */

#include <inttypes.h>

#include "esp_app_desc.h"
#include "esp_chip_info.h"
#include "esp_event.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "ff_cfg.h"
#include "ff_enroll.h"
#include "ff_identity.h"
#include "ff_mqtt.h"
#include "ff_net.h"
#include "ff_progress.h"
#include "ff_store.h"
#include "ff_time.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

static const char *TAG = "ff-agent";

/* Long enough for a slow AP and a slow DHCP server; short enough that the log says
 * "still no address" while someone is still watching. */
#define NET_TIMEOUT_MS 30000
#define SNTP_TIMEOUT_MS 15000

/* The enroll ladder: 60 s -> 15 min, doubling. The first number is not a guess — the
 * server's 600 s enroll_retry_window_s means a 503 (enrollment committed, broker
 * provisioning down) has ten minutes of retries that can still succeed, and a 60 s start
 * fits several of them inside it. */
#define ENROLL_RETRY_MIN_MS 60000
#define ENROLL_RETRY_MAX_MS 900000

/* Park. Used for the failures no retry can fix: a config partition that does not parse, a
 * token the server has permanently refused. The board stays up, logs its reason every
 * five minutes and waits for someone to re-flash it. */
static void park(const char *reason) __attribute__((noreturn));
static void park(const char *reason)
{
    /* Before the loop, once: this is the single most valuable report the board makes,
     * because a parked board says nothing else for the rest of its life and the
     * dashboard would otherwise show the last stage it reached with no explanation.
     * Best effort by construction — ff_progress never fails and never blocks long. */
    ff_progress_report(FF_PROGRESS_HALTED, reason);

    while (true) {
        ESP_LOGE(TAG, "halted: %s", reason);
        vTaskDelay(pdMS_TO_TICKS(300000));
    }
}

static void log_boot_facts(void)
{
    const esp_app_desc_t *app = esp_app_get_description();
    esp_chip_info_t chip;
    esp_chip_info(&chip);

    ESP_LOGI(TAG, "fleetforge agent %s (idf %s), built %s %s", app->version,
             esp_get_idf_version(), app->date, app->time);
    ESP_LOGI(TAG, "chip: model=%d cores=%d revision=%d", (int)chip.model, chip.cores,
             chip.revision);

    /* The running slot proves the A/B layout is real: on a freshly flashed board this is
     * ota_0, and after R2's first update it is ota_1. A board reporting `factory` here was
     * flashed with the wrong partition table and can never OTA. */
    const esp_partition_t *running = esp_ota_get_running_partition();
    if (running != NULL) {
        ESP_LOGI(TAG, "running partition: %s type=%d subtype=%d offset=0x%" PRIx32
                      " size=%" PRIu32,
                 running->label, (int)running->type, (int)running->subtype, running->address,
                 running->size);
    } else {
        ESP_LOGE(TAG, "no running partition: this image was not flashed into an OTA slot");
    }
}

/* NVS holds the broker credential, so a board that cannot mount it cannot remember an
 * enrollment. Erase-and-retry is the standard IDF recipe for the two recoverable causes
 * (a full page table, a format from a different IDF major); it costs this board its
 * credential and therefore a token, which is why it is logged as loudly as it is. */
static esp_err_t nvs_ready(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "nvs is unusable (%s) — erasing it. ANY STORED CREDENTIAL IS NOW "
                      "GONE and this board will need a fresh enrollment token.",
                 esp_err_to_name(err));
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    return err;
}

/* Enroll, retrying on the failures that can change (network down, broker provisioning
 * unavailable inside the grace window) and parking on the ones that cannot (a token the
 * server has refused outright — retrying that only fills a log). */
static void enroll_until_credentialed(const ff_cfg_t *cfg, ff_cred_t *cred)
{
    uint32_t backoff_ms = ENROLL_RETRY_MIN_MS;
    while (true) {
        ff_progress_report(FF_PROGRESS_ENROLLING, NULL);

        esp_err_t err = ff_enroll(cfg, cred);
        if (err == ESP_OK) {
            /* Persist BEFORE connecting: the password exists exactly once, in the response
             * body that was just parsed. A crash between here and the broker would lose it
             * and cost another token (spec/flows.md Flow 1). */
            if (ff_store_save(cred) != ESP_OK) {
                park("the credential could not be written to NVS");
            }
            /* After the save, not before: the stage the dashboard shows should be one
             * the board can actually come back from after a reset. */
            ff_progress_report(FF_PROGRESS_ENROLLED, NULL);
            return;
        }
        if (err == ESP_ERR_INVALID_RESPONSE || err == ESP_ERR_INVALID_ARG) {
            park("this board's enrollment token was refused for good — re-flash ff_cfg "
                 "with a fresh ffe_ token (POST /v1/enrollment-tokens)");
        }
        ESP_LOGW(TAG, "retrying enrollment in %" PRIu32 " s", backoff_ms / 1000);
        vTaskDelay(pdMS_TO_TICKS(backoff_ms));
        backoff_ms = backoff_ms * 2 > ENROLL_RETRY_MAX_MS ? ENROLL_RETRY_MAX_MS : backoff_ms * 2;
    }
}

void app_main(void)
{
    log_boot_facts();

    ESP_ERROR_CHECK(nvs_ready());

    /* Flash-time configuration. No compiled-in fallback exists on purpose: a board that
     * boots with a bad config and silently does nothing is far easier to diagnose than one
     * that connects somewhere unexpected. */
    ff_cfg_t cfg;
    if (ff_cfg_load(&cfg) != ESP_OK) {
        park("no usable ff_cfg partition — re-flash it (agent/tools/ff_cfg.py)");
    }
    ff_cfg_log(&cfg);

    /* Armed as early as the config allows — but it stays silent until there is both a
     * device_id and a link, so the first stage it can ever produce is `link_up`. On an
     * `https://` base that report is HELD until the clock is set and flushed with
     * `time_synced` (S0-fw-2, ff_progress.h property 5); over `http://` it goes out at
     * link time. Either way it is the first stage the server sees. */
    ff_progress_init(&cfg);

    if (ff_identity_init() != ESP_OK) {
        park("no eFuse MAC, therefore no device_id");
    }

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    /* Retry forever rather than reboot: see the file header. */
    while (ff_net_bring_up(&cfg, pdMS_TO_TICKS(NET_TIMEOUT_MS)) != ESP_OK) {
        ESP_LOGW(TAG, "no network yet; waiting for the link");
        vTaskDelay(pdMS_TO_TICKS(5000));
    }

    /* The first thing this board is able to tell the server, and the report that turns
     * "nothing at all" into "arriving" on the dashboard. Reported HERE, at the moment
     * the link comes up, deliberately: against an `https://` base ff_progress holds it
     * over the sync below and sends it ahead of `time_synced` (S0-fw-2). Do not move
     * this call — reporting it after the clock would make it a lie about when the link
     * came up and delay the first sign of life by up to SNTP_TIMEOUT_MS. */
    ff_progress_report(FF_PROGRESS_LINK_UP, ff_cfg_link_name(cfg.link));

    /* Before the first TLS handshake, on BOTH channels. A failure here is not fatal: a
     * plaintext lab (http:// + mqtt://) works fine at epoch 0, and ff_time_sync has
     * already said what an unsynced clock costs. */
    (void)ff_time_sync(cfg.ntp, SNTP_TIMEOUT_MS);
    /* Reported unconditionally, including after a failed sync: the interesting case is
     * a board that reaches `time_synced` and then stalls at `enrolling` because every
     * TLS handshake says "not yet valid". Hiding the stage would hide the clue. */
    ff_progress_report(FF_PROGRESS_TIME_SYNCED, NULL);

    ff_cred_t cred;
    esp_err_t stored = ff_store_load(&cred);
    if (stored == ESP_ERR_NVS_NOT_FOUND) {
        enroll_until_credentialed(&cfg, &cred);
    } else if (stored != ESP_OK) {
        /* Deliberately NOT falling through to enrollment: that would burn a second
         * single-use token and still leave this board unable to connect. */
        park("the stored credential is present but unusable; erase NVS to re-enroll");
    } else if (!ff_store_matches_api_base(&cred, cfg.api_base)) {
        ESP_LOGW(TAG, "this board holds a credential issued by %s but ff_cfg now points at "
                      "%s. Keeping the credential — a hostname change is not a reason to "
                      "throw away a working one — but the broker may refuse it.",
                 cred.api_base, cfg.api_base);
    }

    /* Never returns. */
    esp_err_t err = ff_mqtt_run(&cfg, &cred);
    ESP_LOGE(TAG, "the mqtt session could not be started: %s", esp_err_to_name(err));
    park("no mqtt session");
}
