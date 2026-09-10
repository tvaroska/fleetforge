/*
 * ff_net_wifi — the Wi-Fi adapter behind the ff_net seam. The real fleet's link.
 *
 * Ordinary `esp_wifi` STA bring-up, with two behaviours that are not decoration:
 *
 *  - **reconnect forever, with a capped backoff.** An access point that reboots must not
 *    cost a board its fleet membership, and an agent that gives up after N tries needs a
 *    site visit. The cap exists so a board whose credentials are wrong does not hammer the
 *    AP (and, on a busy channel, everyone else's) once a second for the rest of its life.
 *  - **the credentials are copied out of ff_cfg and never logged.** The SSID appears in
 *    the log because it is what a field engineer needs to see; the passphrase never does.
 *
 * The config keys are `ssid` and `psk` (see ff_cfg.c for why those exact spellings).
 */

#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "ff_net_adapter.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ff-wifi";

/* 1 s → 30 s, doubling. Same ladder the ingestor and the simulator use for the broker;
 * one reconnect policy across the product is one thing to reason about. */
#define WIFI_RETRY_MIN_MS 1000
#define WIFI_RETRY_MAX_MS 30000

static esp_netif_t *s_netif;
static int s_retry_ms = WIFI_RETRY_MIN_MS;

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;
    if (id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
        return;
    }
    if (id == WIFI_EVENT_STA_CONNECTED) {
        s_retry_ms = WIFI_RETRY_MIN_MS;
        ESP_LOGI(TAG, "associated; waiting for DHCP");
        return;
    }
    if (id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *event = (const wifi_event_sta_disconnected_t *)data;
        ESP_LOGW(TAG, "disconnected (reason %d); reconnecting in %d ms",
                 event != NULL ? event->reason : -1, s_retry_ms);
        vTaskDelay(pdMS_TO_TICKS(s_retry_ms));
        s_retry_ms = s_retry_ms * 2 > WIFI_RETRY_MAX_MS ? WIFI_RETRY_MAX_MS : s_retry_ms * 2;
        esp_wifi_connect();
    }
}

static void on_got_ip(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;
    (void)id;
    const ip_event_got_ip_t *event = (const ip_event_got_ip_t *)data;
    ff_net_report_got_ip(event != NULL ? event->esp_netif : s_netif);
}

esp_err_t ff_net_wifi_start(const ff_cfg_t *cfg)
{
    if (cfg->ssid[0] == '\0') {
        ESP_LOGE(TAG, "link=wifi but the config carries no ssid: this board cannot join a "
                      "network. Re-flash ff_cfg (agent/tools/ff_cfg.py --ssid …).");
        return ESP_ERR_INVALID_ARG;
    }

    s_netif = esp_netif_create_default_wifi_sta();
    if (s_netif == NULL) {
        return ESP_FAIL;
    }

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(
        esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &on_wifi_event, NULL));
    ESP_ERROR_CHECK(
        esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &on_got_ip, NULL));

    wifi_config_t wifi = {0};
    /* strncpy into fixed driver buffers: the ff_cfg reader already refused anything longer
     * than the field, so this cannot truncate a usable value. */
    strncpy((char *)wifi.sta.ssid, cfg->ssid, sizeof(wifi.sta.ssid) - 1);
    strncpy((char *)wifi.sta.password, cfg->psk, sizeof(wifi.sta.password) - 1);
    /* An open network is legitimate (a lab bench), so the threshold is OPEN rather than
     * WPA2 — refusing to associate would be a policy this agent has no business having. */
    wifi.sta.threshold.authmode = WIFI_AUTH_OPEN;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "wifi sta starting, ssid %s", cfg->ssid);
    return ESP_OK;
}

bool ff_net_wifi_rssi(int *out_dbm)
{
    wifi_ap_record_t ap;
    if (out_dbm == NULL || esp_wifi_sta_get_ap_info(&ap) != ESP_OK) {
        return false;
    }
    *out_dbm = ap.rssi;
    return true;
}
