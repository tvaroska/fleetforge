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
 *
 * **Known networks (R2b-fw-1, spec/device-protocol.md -> Known networks).** ff_cfg may list
 * up to FF_CFG_MAX_NETS networks in priority order. With exactly ONE, nothing below the
 * `s_net_count > 1` guards runs: the board connects directly, with no scan, exactly as
 * before the list existed. With more, one attempt cycle is:
 *
 *    scan once -> try every network the scan saw, in list order, each by the strongest AP
 *    of that SSID -> then try the ones it did not see, directly (a hidden SSID is never in
 *    a scan) -> one console line if none worked -> back off (1 s -> 30 s) -> scan again.
 *
 * Invariants a reviewer should be able to check by reading:
 *
 *  - **Never scan while associated.** esp_wifi_scan_start() is called from begin_cycle()
 *    only, and begin_cycle() only from STA_START and from the DISCONNECTED paths. A working
 *    association is never left for a higher-priority network; the board re-selects only
 *    after it loses the link.
 *  - **A visible network that gives no address does not block the next one.** An
 *    association that has no DHCP lease after DHCP_TIMEOUT_MS is dropped by a one-shot
 *    watchdog, which raises DISCONNECTED and moves the cycle on.
 *  - **The TX ladder counts only failures against networks the scan saw.** Cutting power
 *    never helps find an access point that is not there, so direct tries of unseen
 *    networks leave it alone.
 *  - **Bounded.** One cycle is at most one scan plus FF_CFG_MAX_NETS attempts, each capped
 *    by the driver's own association timeout plus the DHCP watchdog, then the backoff. The
 *    backoff doubles only between cycles, never between networks within one.
 *  - **All of it runs on the default event-loop task** (the handlers below), so the state
 *    needs no lock. The watchdog runs on the esp_timer task and only reads two flags and
 *    calls esp_wifi_disconnect().
 *
 * "no known network in range (" is the console phrase the flasher's classifier keys on.
 */

#include <limits.h>
#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "ff_net_adapter.h"
#include "freertos/FreeRTOS.h"
#include "nvs.h"
#include "nvs_flash.h"

#if defined(CONFIG_BT_ENABLED) && defined(CONFIG_BT_NIMBLE_ENABLED)
#include "host/ble_hs.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"
#endif

static const char *TAG = "ff-wifi";

#define FF_NET_NVS_NAMESPACE "ff_net"
#define FF_NET_NVS_MAX 4
#define IMPROV_BLE_SERVICE_UUID "00467768-6228-2272-4663-277478268000"
#define IMPROV_BLE_STATE_CHAR_UUID "00467768-6228-2272-4663-277478268001"
#define IMPROV_BLE_ERROR_CHAR_UUID "00467768-6228-2272-4663-277478268002"
#define IMPROV_BLE_RPC_CMD_CHAR_UUID "00467768-6228-2272-4663-277478268003"
#define IMPROV_BLE_RPC_RES_CHAR_UUID "00467768-6228-2272-4663-277478268004"
#define IMPROV_BLE_WINDOW_S 120

#define IMPROV_STATE_AUTHORIZED 0x02
#define IMPROV_STATE_PROVISIONING 0x03
#define IMPROV_STATE_PROVISIONED 0x04
#define IMPROV_ERR_NONE 0x00
#define IMPROV_ERR_INVALID_RPC 0x01
#define IMPROV_CMD_WIFI_SETTINGS 0x01

/* Internal event IDs posted to WIFI_EVENT on the default event loop by one-shot timers
 * and Improv BLE provisioning so the system event task never sleeps or blocks and
 * all Wi-Fi state transitions stay single-threaded on the default event loop. */
#define FF_WIFI_EVENT_RETRY 0x7F00
#define FF_WIFI_EVENT_IMPROV_CRED 0x7F01

typedef enum {
    RETRY_NONE = 0,
    RETRY_CONNECT_SINGLE,
    RETRY_BEGIN_CYCLE,
    RETRY_NEXT_CANDIDATE,
} retry_action_t;

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

/* Multi-network only: how long an association may sit without a DHCP lease before the
 * board gives up on that network and tries the next. Generous — a slow DHCP server on a
 * busy AP is normal — but bounded, or one broken network would hold the board forever. */
#define DHCP_TIMEOUT_MS 20000

static esp_netif_t *s_netif;
static int s_retry_ms = WIFI_RETRY_MIN_MS;

/* The known networks, copied out of ff_cfg at start and refreshed when Improv BLE writes
 * a new credential to the NVS `ff_net` overlay. */
static ff_cfg_t s_saved_cfg;
static ff_cfg_net_t s_nets[FF_CFG_MAX_NETS];
static int s_net_count;

/* The current cycle (multi-network only): which networks the scan saw, the order they are
 * tried in (seen first, then unseen, each in list order), and the position in that order. */
static bool s_visible[FF_CFG_MAX_NETS];
static int s_visible_count;
static int s_order[FF_CFG_MAX_NETS];
static int s_try;
static bool s_scanning;

/* Read by ff_net_wifi_ssid() from other tasks (the announce) and by the watchdog: aligned
 * ints and bools, single writer (the event-loop task). */
static volatile int s_joined = -1; /* index into s_nets of the network that gave an IP */
static volatile bool s_have_ip;
static volatile bool s_dhcp_armed;
static esp_timer_handle_t s_dhcp_timer;
static esp_timer_handle_t s_retry_timer;
static volatile retry_action_t s_retry_action = RETRY_NONE;
static volatile bool s_improv_active;
static esp_timer_handle_t s_improv_timer;
static uint8_t s_improv_state = IMPROV_STATE_AUTHORIZED;
static uint8_t s_improv_error = IMPROV_ERR_NONE;
static char s_improv_ssid[FF_CFG_MAX_SSID];
static char s_improv_sec[FF_CFG_MAX_PSK];

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

static void dhcp_watchdog_arm(void)
{
    if (s_dhcp_timer == NULL) {
        return;
    }
    esp_timer_stop(s_dhcp_timer); /* ESP_ERR_INVALID_STATE when not running: fine */
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

/* Parse an Improv Wi-Fi RPC command (`00467768-6228-2272-4663-277478268003`, cmd `0x01`:
 * `[0x01, data_len, ssid_len, ssid..., passphrase_len, passphrase..., checksum]`), persist
 * the credential to the NVS `ff_net` overlay, and trigger an immediate reconnect cycle on
 * the default event loop without blocking the BLE host or event task. */
static esp_err_t improv_handle_rpc_command(const uint8_t *buf, size_t len)
{
    if (buf == NULL || len < 4 || buf[0] != IMPROV_CMD_WIFI_SETTINGS) {
        s_improv_error = IMPROV_ERR_INVALID_RPC;
        return ESP_ERR_INVALID_ARG;
    }
    size_t data_len = buf[1];
    if (2 + data_len > len || data_len < 2) {
        s_improv_error = IMPROV_ERR_INVALID_RPC;
        return ESP_ERR_INVALID_ARG;
    }
    size_t ssid_len = buf[2];
    if (ssid_len == 0 || ssid_len >= sizeof(s_improv_ssid) || 1 + ssid_len + 1 > data_len) {
        s_improv_error = IMPROV_ERR_INVALID_RPC;
        return ESP_ERR_INVALID_ARG;
    }
    size_t sec_len = buf[3 + ssid_len];
    if (sec_len >= sizeof(s_improv_sec) || 1 + ssid_len + 1 + sec_len > data_len) {
        s_improv_error = IMPROV_ERR_INVALID_RPC;
        return ESP_ERR_INVALID_ARG;
    }
    memset(s_improv_ssid, 0, sizeof(s_improv_ssid));
    memset(s_improv_sec, 0, sizeof(s_improv_sec));
    memcpy(s_improv_ssid, &buf[3], ssid_len);
    if (sec_len > 0) {
        memcpy(s_improv_sec, &buf[4 + ssid_len], sec_len);
    }
    esp_err_t err = ff_net_wifi_save_nvs(s_improv_ssid, s_improv_sec);
    if (err != ESP_OK) {
        s_improv_error = IMPROV_ERR_INVALID_RPC;
        return err;
    }
    s_improv_error = IMPROV_ERR_NONE;
    s_improv_state = IMPROV_STATE_PROVISIONING;
    (void)esp_event_post(WIFI_EVENT, FF_WIFI_EVENT_IMPROV_CRED, NULL, 0, 0);
    return ESP_OK;
}

#if defined(CONFIG_BT_ENABLED) && defined(CONFIG_BT_NIMBLE_ENABLED)
static bool s_nimble_started;

/* UUID 00467768-6228-2272-4663-277478268000..8004 in little-endian byte order for NimBLE. */
static const ble_uuid128_t s_improv_svc_uuid =
    BLE_UUID128_INIT(0x00, 0x80, 0x26, 0x78, 0x74, 0x27, 0x63, 0x46,
                     0x72, 0x22, 0x28, 0x62, 0x68, 0x77, 0x46, 0x00);
static const ble_uuid128_t s_improv_state_uuid =
    BLE_UUID128_INIT(0x01, 0x80, 0x26, 0x78, 0x74, 0x27, 0x63, 0x46,
                     0x72, 0x22, 0x28, 0x62, 0x68, 0x77, 0x46, 0x00);
static const ble_uuid128_t s_improv_err_uuid =
    BLE_UUID128_INIT(0x02, 0x80, 0x26, 0x78, 0x74, 0x27, 0x63, 0x46,
                     0x72, 0x22, 0x28, 0x62, 0x68, 0x77, 0x46, 0x00);
static const ble_uuid128_t s_improv_rpc_cmd_uuid =
    BLE_UUID128_INIT(0x03, 0x80, 0x26, 0x78, 0x74, 0x27, 0x63, 0x46,
                     0x72, 0x22, 0x28, 0x62, 0x68, 0x77, 0x46, 0x00);
static const ble_uuid128_t s_improv_rpc_res_uuid =
    BLE_UUID128_INIT(0x04, 0x80, 0x26, 0x78, 0x74, 0x27, 0x63, 0x46,
                     0x72, 0x22, 0x28, 0x62, 0x68, 0x77, 0x46, 0x00);

static int improv_gatt_access(uint16_t conn_handle, uint16_t attr_handle,
                              struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    (void)conn_handle;
    (void)attr_handle;
    uintptr_t ch = (uintptr_t)arg;
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        uint8_t val = (ch == 1) ? s_improv_state : ((ch == 2) ? s_improv_error : 0);
        return os_mbuf_append(ctxt->om, &val, sizeof(val)) == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
    }
    if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR && ch == 3) {
        static uint8_t s_rpc_buf[128];
        uint16_t copied = 0;
        int rc = ble_hs_mbuf_to_flat(ctxt->om, s_rpc_buf, sizeof(s_rpc_buf), &copied);
        if (rc != 0) {
            return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        }
        return improv_handle_rpc_command(s_rpc_buf, copied) == ESP_OK ? 0 : BLE_ATT_ERR_UNLIKELY;
    }
    return BLE_ATT_ERR_UNLIKELY;
}

static const struct ble_gatt_svc_def s_improv_gatt_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &s_improv_svc_uuid.u,
        .characteristics =
            (struct ble_gatt_chr_def[]){
                {
                    .uuid = &s_improv_state_uuid.u,
                    .access_cb = improv_gatt_access,
                    .arg = (void *)1,
                    .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY,
                },
                {
                    .uuid = &s_improv_err_uuid.u,
                    .access_cb = improv_gatt_access,
                    .arg = (void *)2,
                    .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY,
                },
                {
                    .uuid = &s_improv_rpc_cmd_uuid.u,
                    .access_cb = improv_gatt_access,
                    .arg = (void *)3,
                    .flags = BLE_GATT_CHR_F_WRITE,
                },
                {
                    .uuid = &s_improv_rpc_res_uuid.u,
                    .access_cb = improv_gatt_access,
                    .arg = (void *)4,
                    .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY,
                },
                {0},
            },
    },
    {0},
};

static void improv_ble_advertise_start(void)
{
    struct ble_hs_adv_fields fields = {0};
    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    fields.uuids128 = (ble_uuid128_t *)&s_improv_svc_uuid;
    fields.num_uuids128 = 1;
    fields.uuids128_is_complete = 1;
    (void)ble_gap_adv_set_fields(&fields);

    struct ble_gap_adv_params adv_params = {0};
    adv_params.conn_mode = BLE_GAP_CONN_MODE_UND;
    adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN;
    (void)ble_gap_adv_start(BLE_OWN_ADDR_PUBLIC, NULL, BLE_HS_FOREVER, &adv_params, NULL, NULL);
}

static void improv_nimble_host_task(void *param)
{
    (void)param;
    nimble_port_run();
    nimble_port_freertos_deinit();
}

static void improv_ble_gatt_start(void)
{
    if (!s_nimble_started) {
        if (nimble_port_init() != ESP_OK) {
            return;
        }
        ble_svc_gap_init();
        ble_svc_gatt_init();
        (void)ble_svc_gap_device_name_set("fleetforge-improv");
        (void)ble_gatts_count_cfg(s_improv_gatt_svcs);
        (void)ble_gatts_add_svcs(s_improv_gatt_svcs);
        ble_hs_cfg.sync_cb = improv_ble_advertise_start;
        nimble_port_freertos_init(improv_nimble_host_task);
        s_nimble_started = true;
        return;
    }
    improv_ble_advertise_start();
}

static void improv_ble_gatt_stop(void)
{
    if (s_nimble_started && ble_gap_adv_active()) {
        (void)ble_gap_adv_stop();
    }
}
#else
static void improv_ble_gatt_start(void)
{
    /* When NimBLE is not enabled in sdkconfig, keep the RPC handler referenced so
     * -Werror=unused-function stays clean on non-BLE builds. */
    (void)&improv_handle_rpc_command;
}

static void improv_ble_gatt_stop(void) {}
#endif

static void on_improv_timeout(void *arg)
{
    (void)arg;
    if (!s_improv_active) {
        return;
    }
    s_improv_active = false;
    improv_ble_gatt_stop();
    ESP_LOGI(TAG, "Improv Wi-Fi BLE window (%d s) expired; continuing background scan",
             IMPROV_BLE_WINDOW_S);
}

static void improv_ble_window_start(void)
{
    if (s_improv_active || s_have_ip) {
        return;
    }
    if (s_improv_timer == NULL) {
        const esp_timer_create_args_t args = {
            .callback = &on_improv_timeout,
            .name = "ff_improv_ble",
        };
        if (esp_timer_create(&args, &s_improv_timer) != ESP_OK) {
            s_improv_timer = NULL;
        }
    }
    s_improv_active = true;
    s_improv_state = IMPROV_STATE_AUTHORIZED;
    s_improv_error = IMPROV_ERR_NONE;
    improv_ble_gatt_start();
    if (s_improv_timer != NULL) {
        esp_timer_stop(s_improv_timer);
        esp_timer_start_once(s_improv_timer, (uint64_t)IMPROV_BLE_WINDOW_S * 1000000ULL);
    }
    ESP_LOGI(TAG, "opening Improv Wi-Fi BLE window (%s) for %d s", IMPROV_BLE_SERVICE_UUID,
             IMPROV_BLE_WINDOW_S);
}

static void improv_ble_window_stop(void)
{
    if (!s_improv_active) {
        return;
    }
    s_improv_active = false;
    s_improv_state = IMPROV_STATE_PROVISIONED;
    if (s_improv_timer != NULL) {
        esp_timer_stop(s_improv_timer);
    }
    improv_ble_gatt_stop();
    ESP_LOGI(TAG, "closing Improv Wi-Fi BLE window");
}

static void load_nvs_net_overlay(const ff_cfg_t *cfg)
{
    s_net_count = 0;
    memset(s_nets, 0, sizeof(s_nets));

    nvs_handle_t h;
    int nvs_loaded = 0;
    if (nvs_open(FF_NET_NVS_NAMESPACE, NVS_READONLY, &h) == ESP_OK) {
        for (int i = 0; i < FF_NET_NVS_MAX && s_net_count < FF_CFG_MAX_NETS; i++) {
            char k_ssid[16];
            char k_sec[16];
            snprintf(k_ssid, sizeof(k_ssid), "ssid_%d", i);
            snprintf(k_sec, sizeof(k_sec), "psk_%d", i);
            char ssid_buf[sizeof(s_nets[0].ssid)] = {0};
            char sec_buf[sizeof(s_nets[0].psk)] = {0};
            size_t ssid_len = sizeof(ssid_buf);
            size_t sec_len = sizeof(sec_buf);
            if (nvs_get_str(h, k_ssid, ssid_buf, &ssid_len) == ESP_OK && ssid_buf[0] != '\0') {
                (void)nvs_get_str(h, k_sec, sec_buf, &sec_len);
                strncpy(s_nets[s_net_count].ssid, ssid_buf, sizeof(s_nets[0].ssid) - 1);
                strncpy(s_nets[s_net_count].psk, sec_buf, sizeof(s_nets[0].psk) - 1);
                s_net_count++;
                nvs_loaded++;
            }
        }
        nvs_close(h);
    }
    if (nvs_loaded > 0) {
        ESP_LOGI(TAG, "loaded %d Wi-Fi network(s) from NVS overlay (%s)", nvs_loaded,
                 FF_NET_NVS_NAMESPACE);
    }

    int cfg_max = cfg->net_count > FF_CFG_MAX_NETS ? FF_CFG_MAX_NETS : cfg->net_count;
    for (int i = 0; i < cfg_max && s_net_count < FF_CFG_MAX_NETS; i++) {
        bool dup = false;
        for (int j = 0; j < s_net_count; j++) {
            if (strcmp(s_nets[j].ssid, cfg->nets[i].ssid) == 0) {
                dup = true;
                break;
            }
        }
        if (!dup) {
            s_nets[s_net_count++] = cfg->nets[i];
        }
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

/* esp_timer task. Touches no state: reads the flags, and lets DISCONNECTED (on the event
 * loop) do the bookkeeping. A link that already has an address is never dropped. */
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

/* Start an association attempt with the k-th network of this cycle's order. The strongest
 * AP of that SSID (a mesh has several), and an all-channel probe so a hidden SSID is found
 * too. Returns ESP_OK when an attempt is in flight (DISCONNECTED or GOT_IP will follow). */
static esp_err_t try_candidate(int k)
{
    s_try = k;
    int idx = s_order[k];
    apply_tx_rung();

    wifi_config_t wifi = {0};
    strncpy((char *)wifi.sta.ssid, s_nets[idx].ssid, sizeof(wifi.sta.ssid) - 1);
    strncpy((char *)wifi.sta.password, s_nets[idx].psk, sizeof(wifi.sta.password) - 1);
    wifi.sta.threshold.authmode = WIFI_AUTH_OPEN; /* as in the single-network path */
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

/* Attempt the order from position k on. True when an attempt is in flight; false when the
 * cycle is exhausted without one. A loop, not a recursion: bounded by s_net_count. */
static bool attempt_from(int k)
{
    for (; k < s_net_count; k++) {
        if (try_candidate(k) == ESP_OK) {
            return true;
        }
    }
    return false;
}

/* Seen networks first, then unseen, each in priority (list) order. */
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

/* The cycle tried everything and nothing gave an address: say so ONCE, then schedule a
 * non-blocking retry timer so the default system event task (`sys_evt`) is never blocked. */
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

/* Begin an attempt cycle with a scan; SCAN_DONE carries it on. THE ONLY CALLER OF
 * esp_wifi_scan_start(), and only ever reached while not associated. If the scan cannot
 * start, every network is tried directly; if none can start an attempt, end_cycle_wait()
 * arms the one-shot retry timer rather than blocking or recursing. */
static void begin_cycle(void)
{
    memset(s_visible, 0, sizeof(s_visible));
    s_visible_count = 0;
    s_try = 0;
    wifi_scan_config_t scan = {0}; /* active, all channels, hidden SSIDs not shown */
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

/* SCAN_DONE: mark which known networks are in range, one record at a time (never an array
 * on the event loop's small stack), and ALWAYS free the driver's list. */
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

/* DISCONNECTED with several known networks: a dropped link re-selects from a fresh scan;
 * a failed attempt moves on to the next network, and only an exhausted cycle backs off. */
static void on_disconnected_multi(int reason)
{
    if (s_scanning) {
        /* No attempt is in flight while a scan runs, so there is nothing to move on from;
         * SCAN_DONE starts the next attempt. Acting here would race it. */
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

    /* Saturating, as in the single-network path. Only a network the scan saw moves the
     * ladder: lower power never finds an access point that is not there. */
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
        /* Only a scan begin_cycle() started; never the single-network path's. */
        if (s_net_count > 1 && s_scanning) {
            on_scan_done((const wifi_event_sta_scan_done_t *)data);
        }
        return;
    }
    if (id == WIFI_EVENT_STA_CONNECTED) {
        retry_timer_disarm();
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
        if (s_net_count > 1) {
            dhcp_watchdog_arm();
        }
        return;
    }
    if (id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *event = (const wifi_event_sta_disconnected_t *)data;
        int reason = event != NULL ? event->reason : -1;
        if (s_net_count > 1) {
            on_disconnected_multi(reason);
            return;
        }
        s_have_ip = false;
        s_joined = -1;
        /* Saturating: at 3 attempts a rung this wraps the ladder roughly every 9
         * failures, and nothing else reads the count. Left to saturate rather than wrap
         * at INT_MAX so the log's "N consecutive failures" stays honest for a board that
         * has been failing for months. */
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
    improv_ble_window_stop();
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
    /* Copied before esp_wifi_start(): STA_START reads them on the event loop.
     * Prepend any NVS `ff_net` overlay networks ahead of `cfg->nets`. */
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
            /* Not fatal: without it a network that associates but never leases an address
             * holds the board until the AP drops it. Loud, because that is a real cost. */
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
        /* The single-network path, unchanged: one config, set once, connected directly. */
        wifi_config_t wifi = {0};
        /* strncpy into fixed driver buffers: the ff_cfg reader already refused anything
         * longer than the field, so this cannot truncate a usable value. */
        strncpy((char *)wifi.sta.ssid, s_nets[0].ssid, sizeof(wifi.sta.ssid) - 1);
        strncpy((char *)wifi.sta.password, s_nets[0].psk, sizeof(wifi.sta.password) - 1);
        /* An open network is legitimate (a lab bench), so the threshold is OPEN rather than
         * WPA2 — refusing to associate would be a policy this agent has no business having. */
        wifi.sta.threshold.authmode = WIFI_AUTH_OPEN;
        ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi));
    }
    /* With several networks, try_candidate() sets the config per attempt. */
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
    int idx = s_joined; /* read once: another task may clear it */
    if (idx < 0 || idx >= s_net_count) {
        return NULL;
    }
    return s_nets[idx].ssid;
}
