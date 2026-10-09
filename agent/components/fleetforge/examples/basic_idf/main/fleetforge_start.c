/*
 * fleetforge_start() — the library's boot sequence for an ESP-IDF app (fleetforge_start.h).
 *
 * Copy this file unchanged. It is the same boot sequence as the Arduino library's
 * Fleetforge.begin() (src/Fleetforge.cpp) and the stock agent's app_main
 * (agent/main/agent_main.c), calling the fleetforge component's public include/ only;
 * tests/test_worked_example.py holds the three together.
 *
 * Its rollback posture (CRITICAL.md: "Device-side confirm timer / rollback path"):
 *
 *  1. The build REFUSES a configuration that cannot roll back (#error below). A runtime
 *     check would be a warning nobody reads on a board nobody can reach.
 *  2. fleetforge_start() arms the confirm timer before anything that can fail or wait, as
 *     agent_main.c's app_main does (R2-fw-4). Confirmation happens only inside ff_mqtt_run,
 *     on the announce PUBACK; nothing in this file confirms, marks or reboots.
 *
 * Nothing here reboots on failure, either: every failure retries on a capped backoff or
 * parks with one log line saying what to fix (agent_main.c's file header says why).
 */

#include "sdkconfig.h"

#if !CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
#error "Fleetforge needs CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y: without it an OTA'd image that cannot reach the fleet can never roll back. Keep the example's sdkconfig.defaults line, and delete sdkconfig so it is read again."
#endif

#include "fleetforge_start.h"

#include <inttypes.h>
#include <stdbool.h>

#include "esp_err.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_system.h"
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

static const char *TAG = "ff-example";

/* The same numbers as agent_main.c, for the same reasons (see the comments there). */
#define NET_TIMEOUT_MS 30000
#define SNTP_TIMEOUT_MS 15000
#define ENROLL_RETRY_MIN_MS 60000
#define ENROLL_RETRY_MAX_MS 900000

/* The task app_main runs in, on the agent: CONFIG_ESP_MAIN_TASK_STACK_SIZE=8192
 * (agent/sdkconfig.defaults), priority ESP_TASK_MAIN_PRIO (1), core 0. ESP-IDF's default
 * main task is 3584, too small for the TLS handshake ff_enroll does. */
#define FF_TASK_STACK 8192
#define FF_TASK_PRIO 1
#define FF_TASK_CORE 0

static bool s_started;

/* Park: the failures no retry can fix. Report HALTED once, then say why every five
 * minutes, forever. Never reboots, never returns. On an unconfirmed OTA image the confirm
 * timer armed in fleetforge_start() still rolls back. */
static void park(const char *reason) __attribute__((noreturn));
static void park(const char *reason)
{
    ff_progress_report(FF_PROGRESS_HALTED, reason);

    while (true) {
        ESP_LOGE(TAG, "halted: %s", reason);
        vTaskDelay(pdMS_TO_TICKS(300000));
    }
}

/* agent_main.c's recipe, without its ESP_ERROR_CHECK: a failed erase parks, never aborts. */
static esp_err_t nvs_ready(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "nvs is unusable (%s) — erasing it. ANY STORED CREDENTIAL IS NOW "
                      "GONE and this board will need a fresh enrollment token.",
                 esp_err_to_name(err));
        err = nvs_flash_erase();
        if (err == ESP_OK) {
            err = nvs_flash_init();
        }
    }
    return err;
}

/* agent_main.c's enroll ladder, unchanged: retry what can change, park on a refusal. */
static void enroll_until_credentialed(const ff_cfg_t *cfg, ff_cred_t *cred)
{
    uint32_t backoff_ms = ENROLL_RETRY_MIN_MS;
    while (true) {
        ff_progress_report(FF_PROGRESS_ENROLLING, NULL);

        esp_err_t err = ff_enroll(cfg, cred);
        if (err == ESP_OK) {
            /* Persist BEFORE connecting: the password exists exactly once. */
            if (ff_store_save(cred) != ESP_OK) {
                park("the credential could not be written to NVS");
            }
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

/* esp_netif_init() and the default event loop may already exist: your app_main may have
 * created them for its own use. That is not an error here. */
static bool created_or_existing(esp_err_t err)
{
    return err == ESP_OK || err == ESP_ERR_INVALID_STATE;
}

/* agent_main.c's app_main after its fault-test blocks, calling the public ff_* only. */
static void fleetforge_task(void *arg)
{
    (void)arg;

    ESP_LOGI(TAG, "fleetforge example, firmware %s", ff_identity_fw_version());

    if (nvs_ready() != ESP_OK) {
        park("nvs cannot be mounted, so no credential can be stored");
    }

    ff_cfg_t cfg;
    if (ff_cfg_load(&cfg) != ESP_OK) {
        park("no usable ff_cfg partition — flash it at the partition table's ff_cfg offset "
             "(agent/tools/ff_cfg.py)");
    }
    ff_cfg_log(&cfg);

    /* S0-fw-4: a token this board has not seen before means re-enroll. Not fatal. */
    (void)ff_store_sync_token(cfg.token);

    ff_progress_init(&cfg);

    if (ff_identity_init() != ESP_OK) {
        park("no eFuse MAC, therefore no device_id");
    }

    if (!created_or_existing(esp_netif_init())) {
        park("esp_netif_init failed");
    }
    if (!created_or_existing(esp_event_loop_create_default())) {
        park("the default event loop could not be created");
    }

    while (ff_net_bring_up(&cfg, pdMS_TO_TICKS(NET_TIMEOUT_MS)) != ESP_OK) {
        ESP_LOGW(TAG, "no network yet; waiting for the link");
        vTaskDelay(pdMS_TO_TICKS(5000));
    }

    ff_progress_report(FF_PROGRESS_LINK_UP, ff_cfg_link_name(cfg.link));
    if (esp_reset_reason() == ESP_RST_BROWNOUT) {
        ff_progress_report(FF_PROGRESS_BROWNOUT,
                           "the 3.3 V rail collapsed during the previous boot");
    }

    (void)ff_time_sync(cfg.ntp, SNTP_TIMEOUT_MS);
    ff_progress_report(FF_PROGRESS_TIME_SYNCED, NULL);

    ff_cred_t cred;
    esp_err_t stored = ff_store_load(&cred);
    if (stored == ESP_ERR_NVS_NOT_FOUND) {
        enroll_until_credentialed(&cfg, &cred);
    } else if (stored != ESP_OK) {
        /* Deliberately NOT enrolling: that would burn a second single-use token. */
        park("the stored credential is present but unusable; erase NVS to re-enroll");
    } else if (!ff_store_matches_api_base(&cred, cfg.api_base)) {
        ESP_LOGW(TAG, "this board holds a credential issued by %s but ff_cfg now points at "
                      "%s. Keeping the credential, but the broker may refuse it.",
                 cred.api_base, cfg.api_base);
    }

    /* Never returns. */
    esp_err_t err = ff_mqtt_run(&cfg, &cred);
    ESP_LOGE(TAG, "the mqtt session could not be started: %s", esp_err_to_name(err));
    park("no mqtt session");
}

bool fleetforge_start(void)
{
    ff_mqtt_arm_confirm_timer(); /* FIRST of everything: see the file header, point 2 */

    if (s_started) {
        ESP_LOGE(TAG, "fleetforge_start() was called twice; the second call is ignored");
        return false;
    }
    if (xTaskCreatePinnedToCore(fleetforge_task, "fleetforge", FF_TASK_STACK, NULL,
                                FF_TASK_PRIO, NULL, FF_TASK_CORE) != pdPASS) {
        ESP_LOGE(TAG, "cannot create the fleetforge task (out of memory?); the library is "
                      "not started");
        return false;
    }
    s_started = true;
    return true;
}
