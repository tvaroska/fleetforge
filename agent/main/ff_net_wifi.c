/*
 * ff_net_wifi — the Wi-Fi adapter behind the ff_net seam. The real fleet's link.
 *
 * Ordinary `esp_wifi` STA bring-up, with two behaviours that are not decoration:
 *
 *  - **reconnect forever, with a capped backoff.** An access point that reboots must not
 *    cost a board its fleet membership, and an agent that gives up after N tries needs a
 *    site visit. The cap exists so a board whose credentials are wrong does not hammer the
 *    AP (and, on a busy channel, everyone else's) once a second for the rest of its life.
 *  - **the retry VARIES — it walks a TX-power ladder.** Retrying forever is only useful
 *    if the attempts differ; an identical attempt repeated for a year is a stuck board
 *    that merely looks busy. See the ladder comment below.
 *  - **the credentials are copied out of ff_cfg and never logged.** The SSID appears in
 *    the log because it is what a field engineer needs to see; the passphrase never does.
 *  - **modem sleep is set explicitly, not inherited.** See ff_net_wifi_start().
 *
 * The config keys are `ssid` and `psk` (see ff_cfg.c for why those exact spellings).
 */

#include <limits.h>
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

/*
 * The TX-power ladder. Steps DOWN, in 0.25 dBm units (the esp_wifi_set_max_tx_power
 * unit), after every TX_LADDER_ATTEMPTS consecutive failures, and wraps.
 *
 * Why lowering power can make association SUCCEED, which is the counter-intuitive part:
 * the biggest current transient in this whole sequence is the radio transmitting the
 * auth/assoc frames at full power. On a marginal 3.3 V rail — the S0-fw-3 board, a thin
 * cable, a hub — that transient is what drops the rail under the brownout threshold, and
 * the board resets mid-association. Backing the radio off trades range, which this board
 * has to spare sitting next to an AP, for a peak draw its supply can actually deliver.
 *
 * Rung 0 is NOT a hardcoded 20 dBm: it is whatever the PHY came up with, captured at
 * start. That distinction is load-bearing. CONFIG_ESP_PHY_REDUCE_TX_POWER (S0-fw-3)
 * brings the PHY up at minimum power for a boot that follows a brownout, and a ladder
 * that "restored" a literal 20 dBm on rung 0 would silently undo it on exactly the board
 * it was written for.
 *
 * This is the RUNTIME, per-board version of the knob S0-fw-3 deliberately refused to
 * turn fleet-wide. CONFIG_ESP_PHY_MAX_WIFI_TX_POWER would cost every board range to help
 * the few with bad supplies; this costs range only on a board that has already proven it
 * cannot associate at full power, and only for as long as that stays true.
 */
#define TX_LADDER_ATTEMPTS 3
static const int8_t TX_LADDER_QDBM[] = {0 /* placeholder: the captured default */, 56, 32};
#define TX_LADDER_RUNGS ((int)(sizeof(TX_LADDER_QDBM) / sizeof(TX_LADDER_QDBM[0])))

static esp_netif_t *s_netif;
static int s_retry_ms = WIFI_RETRY_MIN_MS;

/* Rung 0's power, read from the driver once at start rather than assumed. */
static int8_t s_default_qdbm;
/* Consecutive failures since the last association. Drives the ladder position. */
static int s_fail_count;
/* The rung that last produced an association. The ladder restarts from HERE, not from 0,
 * because a board that could only associate at 8 dBm will not survive its first data
 * frame at 20 — the rail that failed during association has not been repaired by it
 * succeeding. Full power is still reached again on wrap-around, which is what lets a
 * board that was moved, or whose supply was fixed, climb back without a re-flash. */
static int s_working_rung;

/* The power for a rung: rung 0 is the captured default, the rest are the table's. */
static int8_t rung_qdbm(int rung)
{
    return rung == 0 ? s_default_qdbm : TX_LADDER_QDBM[rung];
}

/* Move to the rung this failure count calls for, and tell the driver. Applied before
 * every reconnect rather than only on a change: esp_wifi_stop()/start() and some
 * disconnect reasons reset the driver's idea of max power, and re-asserting it costs a
 * register write nobody will ever measure. */
static void apply_tx_rung(void)
{
    int rung = (s_working_rung + s_fail_count / TX_LADDER_ATTEMPTS) % TX_LADDER_RUNGS;
    int8_t qdbm = rung_qdbm(rung);
    esp_err_t err = esp_wifi_set_max_tx_power(qdbm);
    if (err != ESP_OK) {
        /* Not fatal, and not retried: a board that cannot set its TX power can still
         * associate at whatever the driver is already using. Losing the ladder is worth
         * a line in the log, not a failed bring-up. */
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
        /* Freeze the ladder where it succeeded, and stop counting. */
        s_working_rung = (s_working_rung + s_fail_count / TX_LADDER_ATTEMPTS) % TX_LADDER_RUNGS;
        s_fail_count = 0;
        if (s_working_rung != 0) {
            int8_t qdbm = rung_qdbm(s_working_rung);
            ESP_LOGW(TAG, "associated at REDUCED tx power %d.%02d dBm — keeping it. Full "
                          "power is retried only if this stops working.",
                     qdbm / 4, (qdbm % 4) * 25);
        }
        ESP_LOGI(TAG, "associated; waiting for DHCP");
        return;
    }
    if (id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *event = (const wifi_event_sta_disconnected_t *)data;
        /* Saturating: at 3 attempts a rung this wraps the ladder roughly every 9
         * failures, and nothing else reads the count. Left to saturate rather than wrap
         * at INT_MAX so the log's "N consecutive failures" stays honest for a board that
         * has been failing for months. */
        if (s_fail_count < INT_MAX - 1) {
            s_fail_count++;
        }
        ESP_LOGW(TAG, "disconnected (reason %d); reconnecting in %d ms",
                 event != NULL ? event->reason : -1, s_retry_ms);
        vTaskDelay(pdMS_TO_TICKS(s_retry_ms));
        s_retry_ms = s_retry_ms * 2 > WIFI_RETRY_MAX_MS ? WIFI_RETRY_MAX_MS : s_retry_ms * 2;
        apply_tx_rung();
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

    /* Rung 0 of the TX ladder, READ rather than assumed — see the ladder comment above.
     * After a brownout reset CONFIG_ESP_PHY_REDUCE_TX_POWER has already brought the PHY
     * up at minimum power, and that is a decision S0-fw-3 made deliberately for this
     * boot. Capturing whatever the driver reports means the ladder's "full power" rung
     * is that reduced figure on such a boot, instead of silently overriding it. */
    if (esp_wifi_get_max_tx_power(&s_default_qdbm) != ESP_OK) {
        /* 20 dBm, IDF's own default and the fleet-wide figure in sdkconfig.defaults.
         * Only reached if the driver refuses to answer, which it should not after a
         * successful start. */
        s_default_qdbm = 80;
        ESP_LOGW(TAG, "cannot read the phy's tx power; assuming 20 dBm for the retry ladder");
    }
    ESP_LOGI(TAG, "tx power at start: %d.%02d dBm", s_default_qdbm / 4,
             (s_default_qdbm % 4) * 25);

    /* Stated, not inherited. IDF's default is WIFI_PS_MIN_MODEM, so leaving this out
     * still gets modem sleep — but it gets it as an accident of the SDK's default rather
     * than as a decision, and the next person to read this file cannot tell which.
     *
     * MAX_MODEM rather than MIN: the radio then sleeps through multiple DTIM beacons
     * instead of waking for every one. The workload tolerates it by construction — the
     * MQTT keepalive is 30 s (ff_mqtt.c::KEEPALIVE_S) and the heartbeat is `hb_s`, so
     * this board is already choosing to be deaf for tens of seconds at a stretch.
     *
     * The cost is downlink latency: a `dn/cmd` can now wait up to roughly a DTIM
     * interval times the listen interval before the board hears it — seconds, not
     * milliseconds. Accepted deliberately (owner's call, 2026-09-13): every command this
     * product sends is part of a deploy, and a deploy that takes 2 s longer to start is
     * not a worse deploy. Revisit only if R2 ever grows a command that a human is
     * waiting on interactively.
     *
     * Not verifiable off the bench: QEMU has no Wi-Fi (the emulated link is openeth), so
     * no test in this repo can prove the radio actually sleeps. The current-draw check
     * belongs on S0-test-1's bench list. */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_MAX_MODEM));

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
