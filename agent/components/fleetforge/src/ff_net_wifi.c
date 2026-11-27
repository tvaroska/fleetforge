/*
 * ff_net_wifi — the Wi-Fi STA adapter behind the ff_net Port (scan, TX ladder, NVS overlay).
 *
 * Invariants:
 *  - Reconnect forever with capped backoff (1 s -> 30 s) and TX-power step-down ladder.
 *  - Passphrases are never logged; modem sleep (`WIFI_PS_MAX_MODEM`) is set explicitly.
 *  - Never scan while associated: `esp_wifi_scan_start()` is called from `begin_cycle()` only.
 *  - Improv BLE fallback window lives in `ff_net_wifi_improv.c`.
 */

#define FF_NET_WIFI_INTERNAL_IMPL
#include "ff_net_wifi_internal.h"

#include <limits.h>
#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "nvs.h"
#include "nvs_flash.h"

static const char *TAG = "ff-wifi";

#define FF_NET_NVS_NAMESPACE "ff_net"
#define FF_NET_NVS_MAX 4

typedef enum {
    RETRY_NONE = 0,
    RETRY_CONNECT_SINGLE,
    RETRY_BEGIN_CYCLE,
    RETRY_NEXT_CANDIDATE,
} retry_action_t;

#define WIFI_RETRY_MIN_MS 1000
#define WIFI_RETRY_MAX_MS 30000

#define TX_LADDER_ATTEMPTS 3
static const int8_t TX_LADDER_QDBM[] = {0, 56, 32};
#define TX_LADDER_RUNGS ((int)(sizeof(TX_LADDER_QDBM) / sizeof(TX_LADDER_QDBM[0])))

#define DHCP_TIMEOUT_MS 20000

static esp_netif_t *s_netif;
static int s_retry_ms = WIFI_RETRY_MIN_MS;

static ff_cfg_t s_saved_cfg;
static ff_cfg_net_t s_nets[FF_CFG_MAX_NETS];
static int s_net_count;

static bool s_visible[FF_CFG_MAX_NETS];
static int s_visible_count;
static int s_order[FF_CFG_MAX_NETS];
static int s_try;
static bool s_scanning;

static volatile int s_joined = -1;
volatile bool s_have_ip;
static volatile bool s_dhcp_armed;
static esp_timer_handle_t s_dhcp_timer;
static esp_timer_handle_t s_retry_timer;
static volatile retry_action_t s_retry_action = RETRY_NONE;
volatile bool s_venue_offline;
static bool s_wifi_started;

static int8_t s_default_qdbm;
static int s_fail_count;
static int s_working_rung;

static int8_t rung_qdbm(int rung)
{
    return rung == 0 ? s_default_qdbm : TX_LADDER_QDBM[rung];
}

static void apply_tx_rung(void)
{
    int rung = (s_working_rung + s_fail_count / TX_LADDER_ATTEMPTS) % TX_LADDER_RUNGS;
    int8_t qdbm = rung_qdbm(rung);
    esp_err_t err = esp_wifi_set_max_tx_power(qdbm);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "cannot set tx power to %d.%02d dBm (%s); continuing at the "
                      "driver's current setting",
                 qdbm / 4, (qdbm % 4) * 25, esp_err_to_name(err));
        return;
    }
    if (rung != 0) {
        ESP_LOGW(TAG, "retrying at REDUCED tx power %d.%02d dBm (rung %d of %d, %d "
                      "consecutive failures). A board that only associates here has a "
                      "supply or an antenna problem, not a Wi-Fi problem.",
                 qdbm / 4, (qdbm % 4) * 25, rung, TX_LADDER_RUNGS - 1, s_fail_count);
    }
}

static void dhcp_watchdog_arm(void)
{
    if (s_dhcp_timer == NULL) {
        return;
    }
    esp_timer_stop(s_dhcp_timer);
    s_dhcp_armed = true;
    esp_timer_start_once(s_dhcp_timer, (uint64_t)DHCP_TIMEOUT_MS * 1000);
}

static void dhcp_watchdog_disarm(void)
{
    s_dhcp_armed = false;
    if (s_dhcp_timer != NULL) {
        esp_timer_stop(s_dhcp_timer);
    }
}

static void on_retry_timeout(void *arg)
{
    (void)arg;
    if (s_have_ip || s_retry_action == RETRY_NONE) {
        return;
    }
    (void)esp_event_post(WIFI_EVENT, FF_WIFI_EVENT_RETRY, NULL, 0, 0);
}

static void retry_timer_arm(retry_action_t action, int delay_ms)
{
    s_retry_action = action;
    if (s_retry_timer != NULL) {
        esp_timer_stop(s_retry_timer);
        if (esp_timer_start_once(s_retry_timer, (uint64_t)delay_ms * 1000ULL) == ESP_OK) {
            return;
        }
    }
    (void)esp_event_post(WIFI_EVENT, FF_WIFI_EVENT_RETRY, NULL, 0, 0);
}

static void retry_timer_disarm(void)
{
    s_retry_action = RETRY_NONE;
    if (s_retry_timer != NULL) {
        esp_timer_stop(s_retry_timer);
    }
}

static void load_nvs_net_overlay(const ff_cfg_t *cfg)
{
    s_net_count = 0;
    memset(s_nets, 0, sizeof(s_nets));
    int cfg_max = cfg->net_count > FF_CFG_MAX_NETS ? FF_CFG_MAX_NETS : cfg->net_count;
    for (int i = 0; i < cfg_max && s_net_count < FF_CFG_MAX_NETS; i++) {
        s_nets[s_net_count++] = cfg->nets[i];
    }

    nvs_handle_t h;
    int nvs_loaded = 0;
    if (nvs_open(FF_NET_NVS_NAMESPACE, NVS_READONLY, &h) == ESP_OK) {
        for (int i = 0; i < FF_NET_NVS_MAX && s_net_count < FF_CFG_MAX_NETS; i++) {
            char k_ssid[16], k_sec[16];
            snprintf(k_ssid, sizeof(k_ssid), "ssid_%d", i);
            snprintf(k_sec, sizeof(k_sec), "psk_%d", i);
            char ssid_buf[sizeof(s_nets[0].ssid)] = {0};
            char sec_buf[sizeof(s_nets[0].psk)] = {0};
            size_t ssid_len = sizeof(ssid_buf), sec_len = sizeof(sec_buf);
            if (nvs_get_str(h, k_ssid, ssid_buf, &ssid_len) == ESP_OK && ssid_buf[0] != '\0') {
                bool dup = false;
                for (int j = 0; j < s_net_count; j++) {
                    if (strcmp(s_nets[j].ssid, ssid_buf) == 0) {
                        dup = true;
                        break;
                    }
                }
                if (!dup) {
                    (void)nvs_get_str(h, k_sec, sec_buf, &sec_len);
                    strncpy(s_nets[s_net_count].ssid, ssid_buf, sizeof(s_nets[0].ssid) - 1);
                    strncpy(s_nets[s_net_count].psk, sec_buf, sizeof(s_nets[0].psk) - 1);
                    s_net_count++;
                    nvs_loaded++;
                }
            }
        }
        nvs_close(h);
    }
    if (nvs_loaded > 0) {
        ESP_LOGI(TAG, "loaded %d Wi-Fi fallback network(s) from NVS overlay (%s)", nvs_loaded,
                 FF_NET_NVS_NAMESPACE);
    }
}

esp_err_t ff_net_wifi_save_nvs(const char *ssid, const char *passphrase)
{
    if (ssid == NULL || ssid[0] == '\0' || strlen(ssid) >= sizeof(s_nets[0].ssid) ||
        passphrase == NULL || strlen(passphrase) >= sizeof(s_nets[0].psk)) {
        return ESP_ERR_INVALID_ARG;
    }
    nvs_handle_t h;
    esp_err_t err = nvs_open(FF_NET_NVS_NAMESPACE, NVS_READWRITE, &h);
    if (err != ESP_OK) {
        return err;
    }
    for (int i = FF_NET_NVS_MAX - 1; i > 0; i--) {
        char src_ssid[16], src_sec[16], dst_ssid[16], dst_sec[16];
        snprintf(src_ssid, sizeof(src_ssid), "ssid_%d", i - 1);
        snprintf(src_sec, sizeof(src_sec), "psk_%d", i - 1);
        snprintf(dst_ssid, sizeof(dst_ssid), "ssid_%d", i);
        snprintf(dst_sec, sizeof(dst_sec), "psk_%d", i);
        char ssid_buf[sizeof(s_nets[0].ssid)] = {0};
        char sec_buf[sizeof(s_nets[0].psk)] = {0};
        size_t ssid_len = sizeof(ssid_buf);
        size_t sec_len = sizeof(sec_buf);
        if (nvs_get_str(h, src_ssid, ssid_buf, &ssid_len) == ESP_OK &&
            strcmp(ssid_buf, ssid) != 0) {
            (void)nvs_get_str(h, src_sec, sec_buf, &sec_len);
            (void)nvs_set_str(h, dst_ssid, ssid_buf);
            (void)nvs_set_str(h, dst_sec, sec_buf);
        }
    }
    err = nvs_set_str(h, "ssid_0", ssid);
    if (err == ESP_OK) {
        err = nvs_set_str(h, "psk_0", passphrase);
    }
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "saved Wi-Fi network \"%s\" (%u chars) to NVS overlay", ssid,
                 (unsigned)strlen(passphrase));
    }
    return err;
}

static void on_dhcp_timeout(void *arg)
{
    (void)arg;
    if (!s_dhcp_armed || s_have_ip) {
        return;
    }
    int idx = s_order[s_try];
    ESP_LOGW(TAG, "associated with \"%s\" but no address after %d s; trying the next known "
                  "network",
             s_nets[idx].ssid, DHCP_TIMEOUT_MS / 1000);
    esp_wifi_disconnect();
}

static esp_err_t try_candidate(int k)
{
    s_try = k;
    int idx = s_order[k];
    apply_tx_rung();

    wifi_config_t wifi = {0};
    strncpy((char *)wifi.sta.ssid, s_nets[idx].ssid, sizeof(wifi.sta.ssid) - 1);
    strncpy((char *)wifi.sta.password, s_nets[idx].psk, sizeof(wifi.sta.password) - 1);
    wifi.sta.threshold.authmode = WIFI_AUTH_OPEN;
    wifi.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
    wifi.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;

    ESP_LOGI(TAG, "trying \"%s\" (known network %d of %d%s)", s_nets[idx].ssid, idx + 1,
             s_net_count, s_visible[idx] ? "" : ", not seen in the scan");
    esp_err_t err = esp_wifi_set_config(WIFI_IF_STA, &wifi);
    if (err == ESP_OK) {
        err = esp_wifi_connect();
    }
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "cannot start an attempt on \"%s\" (%s); skipping it this cycle",
                 s_nets[idx].ssid, esp_err_to_name(err));
    }
    return err;
}

static bool attempt_from(int k)
{
    for (; k < s_net_count; k++) {
        if (try_candidate(k) == ESP_OK) {
            return true;
        }
    }
    return false;
}

static void build_order(void)
{
    int n = 0;
    for (int i = 0; i < s_net_count; i++) {
        if (s_visible[i]) {
            s_order[n++] = i;
        }
    }
    for (int i = 0; i < s_net_count; i++) {
        if (!s_visible[i]) {
            s_order[n++] = i;
        }
    }
}

static void end_cycle_wait(void)
{
    if (s_visible_count == 0) {
        ESP_LOGW(TAG, "no known network in range (%d known); scanning again in %d s",
                 s_net_count, s_retry_ms / 1000);
    } else {
        ESP_LOGW(TAG, "none of the %d known networks in range could be joined (%d known); "
                      "scanning again in %d s",
                 s_visible_count, s_net_count, s_retry_ms / 1000);
    }
    improv_ble_window_start();
    int delay_ms = s_retry_ms;
    s_retry_ms = s_retry_ms * 2 > WIFI_RETRY_MAX_MS ? WIFI_RETRY_MAX_MS : s_retry_ms * 2;
    retry_timer_arm(RETRY_BEGIN_CYCLE, delay_ms);
}

static void begin_cycle(void)
{
    memset(s_visible, 0, sizeof(s_visible));
    s_visible_count = 0;
    s_try = 0;
    wifi_scan_config_t scan = {0};
    s_scanning = true;
    esp_err_t err = esp_wifi_scan_start(&scan, false);
    if (err == ESP_OK) {
        return;
    }
    s_scanning = false;
    ESP_LOGW(TAG, "cannot start a scan (%s); trying every known network directly",
             esp_err_to_name(err));
    build_order();
    if (attempt_from(0)) {
        return;
    }
    end_cycle_wait();
}

static void on_scan_done(const wifi_event_sta_scan_done_t *event)
{
    s_scanning = false;
    uint16_t count = 0;
    if (event == NULL || event->status == 0) {
        esp_wifi_scan_get_ap_num(&count);
    }
    for (uint16_t n = 0; n < count; n++) {
        wifi_ap_record_t rec;
        if (esp_wifi_scan_get_ap_record(&rec) != ESP_OK) {
            break;
        }
        for (int i = 0; i < s_net_count; i++) {
            if (!s_visible[i] && strcmp((const char *)rec.ssid, s_nets[i].ssid) == 0) {
                s_visible[i] = true;
                s_visible_count++;
            }
        }
    }
    esp_wifi_clear_ap_list();

    build_order();
    ESP_LOGI(TAG, "scan: %u access points, %d of %d known networks in range", (unsigned)count,
             s_visible_count, s_net_count);
    if (attempt_from(0)) {
        return;
    }
    end_cycle_wait();
}

static void on_disconnected_multi(int reason)
{
    if (s_scanning) {
        return;
    }
    dhcp_watchdog_disarm();
    bool had_ip = s_have_ip;
    s_have_ip = false;
    s_joined = -1;
    int failed = s_order[s_try];

    if (had_ip) {
        ESP_LOGW(TAG, "disconnected (reason %d); link to \"%s\" lost; re-selecting in %d ms",
                 reason, s_nets[failed].ssid, s_retry_ms);
        int delay_ms = s_retry_ms;
        s_retry_ms = s_retry_ms * 2 > WIFI_RETRY_MAX_MS ? WIFI_RETRY_MAX_MS : s_retry_ms * 2;
        retry_timer_arm(RETRY_BEGIN_CYCLE, delay_ms);
        return;
    }

    if (s_visible[failed] && s_fail_count < INT_MAX - 1) {
        s_fail_count++;
    }
    int next = s_try + 1;
    if (next < s_net_count) {
        ESP_LOGW(TAG, "disconnected (reason %d); trying \"%s\" next", reason,
                 s_nets[s_order[next]].ssid);
        retry_timer_arm(RETRY_NEXT_CANDIDATE, WIFI_RETRY_MIN_MS);
        return;
    }
    ESP_LOGW(TAG, "disconnected (reason %d); that was the last known network", reason);
    end_cycle_wait();
}

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;
    if (id == WIFI_EVENT_STA_START) {
        if (s_net_count > 1) {
            begin_cycle();
        } else {
            esp_wifi_connect();
        }
        return;
    }
    if (id == FF_WIFI_EVENT_RETRY) {
        if (s_have_ip) {
            return;
        }
        retry_action_t act = s_retry_action;
        s_retry_action = RETRY_NONE;
        if (act == RETRY_CONNECT_SINGLE) {
            apply_tx_rung();
            esp_wifi_connect();
        } else if (act == RETRY_NEXT_CANDIDATE) {
            if (!attempt_from(s_try + 1)) {
                end_cycle_wait();
            }
        } else if (act == RETRY_BEGIN_CYCLE) {
            begin_cycle();
        }
        return;
    }
    if (id == FF_WIFI_EVENT_IMPROV_CRED) {
        retry_timer_disarm();
        dhcp_watchdog_disarm();
        load_nvs_net_overlay(&s_saved_cfg);
        s_retry_ms = WIFI_RETRY_MIN_MS;
        if (s_net_count > 1) {
            if (!s_scanning) {
                (void)esp_wifi_disconnect();
                begin_cycle();
            }
        } else if (s_net_count == 1) {
            wifi_config_t wifi = {0};
            strncpy((char *)wifi.sta.ssid, s_nets[0].ssid, sizeof(wifi.sta.ssid) - 1);
            strncpy((char *)wifi.sta.password, s_nets[0].psk, sizeof(wifi.sta.password) - 1);
            wifi.sta.threshold.authmode = WIFI_AUTH_OPEN;
            (void)esp_wifi_disconnect();
            (void)esp_wifi_set_config(WIFI_IF_STA, &wifi);
            apply_tx_rung();
            (void)esp_wifi_connect();
        }
        return;
    }
    if (id == WIFI_EVENT_SCAN_DONE) {
        if (s_net_count > 1 && s_scanning) {
            on_scan_done((const wifi_event_sta_scan_done_t *)data);
        }
        return;
    }
    if (id == WIFI_EVENT_STA_CONNECTED) {
        retry_timer_disarm();
        s_retry_ms = WIFI_RETRY_MIN_MS;
        s_working_rung = (s_working_rung + s_fail_count / TX_LADDER_ATTEMPTS) % TX_LADDER_RUNGS;
        s_fail_count = 0;
        if (s_working_rung != 0) {
            int8_t qdbm = rung_qdbm(s_working_rung);
            ESP_LOGW(TAG, "associated at REDUCED tx power %d.%02d dBm — keeping it. Full "
                          "power is retried only if this stops working.",
                     qdbm / 4, (qdbm % 4) * 25);
        }
        ESP_LOGI(TAG, "associated; waiting for DHCP");
        if (s_net_count > 1) {
            dhcp_watchdog_arm();
        }
        return;
    }
    if (id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *event = (const wifi_event_sta_disconnected_t *)data;
        int reason = event != NULL ? event->reason : -1;
        improv_ble_on_sta_disconnected();
        if (s_net_count > 1) {
            on_disconnected_multi(reason);
            return;
        }
        s_have_ip = false;
        s_joined = -1;
        if (s_fail_count < INT_MAX - 1) {
            s_fail_count++;
        }
        ESP_LOGW(TAG, "disconnected (reason %d); reconnecting in %d ms", reason, s_retry_ms);
        if (reason == WIFI_REASON_NO_AP_FOUND) {
            ESP_LOGW(TAG, "no known network in range (1 known); trying again in %d s",
                     s_retry_ms / 1000);
            improv_ble_window_start();
        }
        int delay_ms = s_retry_ms;
        s_retry_ms = s_retry_ms * 2 > WIFI_RETRY_MAX_MS ? WIFI_RETRY_MAX_MS : s_retry_ms * 2;
        retry_timer_arm(RETRY_CONNECT_SINGLE, delay_ms);
    }
}

static void on_got_ip(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;
    (void)id;
    const ip_event_got_ip_t *event = (const ip_event_got_ip_t *)data;
    retry_timer_disarm();
    improv_ble_on_got_ip();
    if (s_net_count > 1) {
        dhcp_watchdog_disarm();
        int idx = s_order[s_try];
        s_have_ip = true;
        s_joined = idx;
        ESP_LOGI(TAG, "joined \"%s\" (known network %d of %d)", s_nets[idx].ssid, idx + 1,
                 s_net_count);
    } else {
        s_have_ip = true;
        s_joined = 0;
    }
    ff_net_report_got_ip(event != NULL ? event->esp_netif : s_netif);
}

esp_err_t ff_net_wifi_start(const ff_cfg_t *cfg)
{
    if (cfg != NULL) {
        s_saved_cfg = *cfg;
    }
    load_nvs_net_overlay(cfg);
    if (s_net_count == 0) {
        ESP_LOGE(TAG, "link=wifi but the config carries no ssid: this board cannot join a "
                      "network. Re-flash ff_cfg (agent/tools/ff_cfg.py --ssid …).");
        return ESP_ERR_INVALID_ARG;
    }

    if (s_retry_timer == NULL) {
        const esp_timer_create_args_t retry_args = {
            .callback = &on_retry_timeout,
            .name = "ff_wifi_retry",
        };
        if (esp_timer_create(&retry_args, &s_retry_timer) != ESP_OK) {
            s_retry_timer = NULL;
        }
    }

    if (s_net_count > 1) {
        const esp_timer_create_args_t watchdog = {
            .callback = &on_dhcp_timeout,
            .name = "ff_dhcp_wd",
        };
        esp_err_t err = esp_timer_create(&watchdog, &s_dhcp_timer);
        if (err != ESP_OK) {
            s_dhcp_timer = NULL;
            ESP_LOGE(TAG, "cannot create the dhcp watchdog (%s); a network with no dhcp may "
                          "hold this board",
                     esp_err_to_name(err));
        }
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

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    if (s_net_count == 1) {
        wifi_config_t wifi = {0};
        strncpy((char *)wifi.sta.ssid, s_nets[0].ssid, sizeof(wifi.sta.ssid) - 1);
        strncpy((char *)wifi.sta.password, s_nets[0].psk, sizeof(wifi.sta.password) - 1);
        wifi.sta.threshold.authmode = WIFI_AUTH_OPEN;
        ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi));
    }
    if (s_venue_offline) {
        ESP_LOGI(TAG, "venue offline mode active: skipping esp_wifi_start (0 RF frames emitted)");
        return ESP_OK;
    }
    ESP_ERROR_CHECK(esp_wifi_start());
    s_wifi_started = true;

    if (esp_wifi_get_max_tx_power(&s_default_qdbm) != ESP_OK) {
        s_default_qdbm = 80;
        ESP_LOGW(TAG, "cannot read the phy's tx power; assuming 20 dBm for the retry ladder");
    }
    ESP_LOGI(TAG, "tx power at start: %d.%02d dBm", s_default_qdbm / 4,
             (s_default_qdbm % 4) * 25);

    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_MAX_MODEM));

    if (s_net_count == 1) {
        ESP_LOGI(TAG, "wifi sta starting, ssid %s", s_nets[0].ssid);
    } else {
        ESP_LOGI(TAG, "wifi sta starting, %d known networks; scanning", s_net_count);
    }
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

const char *ff_net_wifi_ssid(void)
{
    int idx = s_joined;
    if (idx < 0 || idx >= s_net_count) {
        return NULL;
    }
    return s_nets[idx].ssid;
}

int ff_net_wifi_known_count(void)
{
    return s_net_count;
}

esp_err_t ff_net_wifi_set_offline_mode(bool offline)
{
    s_venue_offline = offline;
    if (offline) {
        retry_timer_disarm();
        dhcp_watchdog_disarm();
        improv_ble_window_stop();
        s_have_ip = false;
        s_joined = -1;
        if (s_wifi_started) {
            (void)esp_wifi_disconnect();
            esp_err_t err = esp_wifi_stop();
            s_wifi_started = false;
            return err;
        }
        return ESP_OK;
    }
    if (s_netif != NULL && !s_wifi_started) {
        esp_err_t err = esp_wifi_start();
        if (err == ESP_OK) {
            s_wifi_started = true;
        }
        return err;
    }
    return ESP_OK;
}

bool ff_net_wifi_is_offline_mode(void)
{
    return s_venue_offline;
}
