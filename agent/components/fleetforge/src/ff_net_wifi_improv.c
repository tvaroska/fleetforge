/*
 * ff_net_wifi_improv — Improv Wi-Fi BLE fallback provisioning window (`00467768-...`).
 *
 * Enforces physical/serial authorization (`IMPROV_STATE_AUTH_REQUIRED` -> `ff_net_wifi_improv_authorize`),
 * encrypted BLE GATT writes (`BLE_GATT_CHR_F_WRITE_ENC` + `desc.sec_state.encrypted`),
 * per-boot open-count and cooldown rate caps, and Venue Offline Mode suppression (`D-8`).
 */

#define FF_NET_WIFI_INTERNAL_IMPL
#include "ff_net_wifi_internal.h"

#include <stdint.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"

#if defined(CONFIG_BT_ENABLED) && defined(CONFIG_BT_NIMBLE_ENABLED)
#include "host/ble_hs.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"
#endif

static const char *TAG = "ff-wifi";

#define IMPROV_BLE_SERVICE_UUID "00467768-6228-2272-4663-277478268000"
#define IMPROV_BLE_STATE_CHAR_UUID "00467768-6228-2272-4663-277478268001"
#define IMPROV_BLE_ERROR_CHAR_UUID "00467768-6228-2272-4663-277478268002"
#define IMPROV_BLE_RPC_CMD_CHAR_UUID "00467768-6228-2272-4663-277478268003"
#define IMPROV_BLE_RPC_RES_CHAR_UUID "00467768-6228-2272-4663-277478268004"
#define IMPROV_BLE_WINDOW_S 120
#define IMPROV_BLE_MAX_OPENS_PER_BOOT 3
#define IMPROV_BLE_COOLDOWN_US (600LL * 1000000LL)

#define IMPROV_STATE_AUTH_REQUIRED 0x01
#define IMPROV_STATE_AUTHORIZED 0x02
#define IMPROV_STATE_PROVISIONING 0x03
#define IMPROV_STATE_PROVISIONED 0x04
#define IMPROV_ERR_NONE 0x00
#define IMPROV_ERR_INVALID_RPC 0x01
#define IMPROV_ERR_UNKNOWN_RPC 0x02
#define IMPROV_ERR_UNABLE_TO_CONNECT 0x03
#define IMPROV_ERR_NOT_AUTHORIZED 0x04
#define IMPROV_CMD_WIFI_SETTINGS 0x01
#define IMPROV_CMD_IDENTIFY 0x02

static volatile bool s_improv_active;
static volatile bool s_improv_ble_enabled = true;
static portMUX_TYPE s_improv_lock = portMUX_INITIALIZER_UNLOCKED;
static esp_timer_handle_t s_improv_timer;
static uint8_t s_improv_state = IMPROV_STATE_AUTH_REQUIRED;
static uint8_t s_improv_error = IMPROV_ERR_NONE;
static int s_improv_open_count;
static int64_t s_improv_last_open_us = -IMPROV_BLE_COOLDOWN_US;
static uint8_t s_improv_rpc_res_buf[16];
static size_t s_improv_rpc_res_len;
static char s_improv_ssid[FF_CFG_MAX_SSID];
static char s_improv_sec[FF_CFG_MAX_PSK];

static void improv_ble_notify_status(void);
static void improv_ble_notify_rpc_result(void);

static void improv_set_rpc_result(uint8_t cmd)
{
    s_improv_rpc_res_buf[0] = cmd;
    s_improv_rpc_res_buf[1] = 0x00;
    s_improv_rpc_res_buf[2] = cmd;
    s_improv_rpc_res_len = 3;
    improv_ble_notify_rpc_result();
}

static esp_err_t improv_rpc_fail(uint8_t err, esp_err_t rc)
{
    portENTER_CRITICAL(&s_improv_lock);
    s_improv_error = err;
    portEXIT_CRITICAL(&s_improv_lock);
    improv_ble_notify_status();
    return rc;
}

static esp_err_t improv_handle_rpc_command(const uint8_t *buf, size_t len)
{
    bool allowed;
    uint8_t state_now;
    portENTER_CRITICAL(&s_improv_lock);
    allowed = s_improv_active && s_improv_ble_enabled && !s_venue_offline;
    state_now = s_improv_state;
    portEXIT_CRITICAL(&s_improv_lock);
    if (!allowed) {
        return ESP_ERR_INVALID_STATE;
    }
    if (buf == NULL || len < 2) {
        return improv_rpc_fail(IMPROV_ERR_INVALID_RPC, ESP_ERR_INVALID_ARG);
    }
    uint8_t cmd = buf[0];
    size_t data_len = buf[1];
    if (2 + data_len > len) {
        return improv_rpc_fail(IMPROV_ERR_INVALID_RPC, ESP_ERR_INVALID_ARG);
    }
    if (len >= 2 + data_len + 1) {
        uint8_t expected_csum = 0;
        for (size_t i = 0; i < 2 + data_len; i++) {
            expected_csum = (uint8_t)(expected_csum + buf[i]);
        }
        if (buf[2 + data_len] != expected_csum) {
            return improv_rpc_fail(IMPROV_ERR_INVALID_RPC, ESP_ERR_INVALID_CRC);
        }
    }
    if (cmd == IMPROV_CMD_IDENTIFY) {
        ESP_LOGI(TAG, "Improv BLE identify requested");
        return improv_rpc_fail(IMPROV_ERR_NONE, ESP_OK);
    }
    if (cmd != IMPROV_CMD_WIFI_SETTINGS) {
        return improv_rpc_fail(IMPROV_ERR_UNKNOWN_RPC, ESP_ERR_NOT_SUPPORTED);
    }
    if (state_now != IMPROV_STATE_AUTHORIZED) {
        return improv_rpc_fail(IMPROV_ERR_NOT_AUTHORIZED, ESP_ERR_INVALID_STATE);
    }
    if (data_len < 2) {
        return improv_rpc_fail(IMPROV_ERR_INVALID_RPC, ESP_ERR_INVALID_ARG);
    }
    size_t ssid_len = buf[2];
    if (ssid_len == 0 || ssid_len >= sizeof(s_improv_ssid) || 1 + ssid_len + 1 > data_len) {
        return improv_rpc_fail(IMPROV_ERR_INVALID_RPC, ESP_ERR_INVALID_ARG);
    }
    size_t sec_len = buf[3 + ssid_len];
    if (sec_len >= sizeof(s_improv_sec) || 1 + ssid_len + 1 + sec_len > data_len) {
        return improv_rpc_fail(IMPROV_ERR_INVALID_RPC, ESP_ERR_INVALID_ARG);
    }
    memset(s_improv_ssid, 0, sizeof(s_improv_ssid));
    memset(s_improv_sec, 0, sizeof(s_improv_sec));
    memcpy(s_improv_ssid, &buf[3], ssid_len);
    if (sec_len > 0) {
        memcpy(s_improv_sec, &buf[4 + ssid_len], sec_len);
    }
    esp_err_t err = ff_net_wifi_save_nvs(s_improv_ssid, s_improv_sec);
    if (err != ESP_OK) {
        return improv_rpc_fail(IMPROV_ERR_INVALID_RPC, err);
    }
    portENTER_CRITICAL(&s_improv_lock);
    s_improv_error = IMPROV_ERR_NONE;
    s_improv_state = IMPROV_STATE_PROVISIONING;
    portEXIT_CRITICAL(&s_improv_lock);
    improv_ble_notify_status();
    (void)esp_event_post(WIFI_EVENT, FF_WIFI_EVENT_IMPROV_CRED, NULL, 0, 0);
    return ESP_OK;
}

#if defined(CONFIG_BT_ENABLED) && defined(CONFIG_BT_NIMBLE_ENABLED)
static bool s_nimble_started;
static uint16_t s_improv_state_val_handle;
static uint16_t s_improv_err_val_handle;
static uint16_t s_improv_rpc_res_val_handle;

#define IMPROV_UUID128(b0)                                                                     \
    BLE_UUID128_INIT((b0), 0x80, 0x26, 0x78, 0x74, 0x27, 0x63, 0x46, 0x72, 0x22, 0x28, 0x62,   \
                     0x68, 0x77, 0x46, 0x00)
static const ble_uuid128_t s_improv_svc_uuid = IMPROV_UUID128(0x00);
static const ble_uuid128_t s_improv_state_uuid = IMPROV_UUID128(0x01);
static const ble_uuid128_t s_improv_err_uuid = IMPROV_UUID128(0x02);
static const ble_uuid128_t s_improv_rpc_cmd_uuid = IMPROV_UUID128(0x03);
static const ble_uuid128_t s_improv_rpc_res_uuid = IMPROV_UUID128(0x04);

static void improv_ble_notify_status(void)
{
    if (!s_nimble_started) {
        return;
    }
    if (s_improv_state_val_handle != 0) {
        ble_gatts_chr_updated(s_improv_state_val_handle);
    }
    if (s_improv_err_val_handle != 0) {
        ble_gatts_chr_updated(s_improv_err_val_handle);
    }
}

static void improv_ble_notify_rpc_result(void)
{
    if (!s_nimble_started) {
        return;
    }
    if (s_improv_rpc_res_val_handle != 0) {
        ble_gatts_chr_updated(s_improv_rpc_res_val_handle);
    }
}

static int improv_gatt_access(uint16_t conn_handle, uint16_t attr_handle,
                              struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    (void)conn_handle;
    (void)attr_handle;
    uintptr_t ch = (uintptr_t)arg;
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        if (ch == 4 && s_improv_rpc_res_len > 0) {
            return os_mbuf_append(ctxt->om, s_improv_rpc_res_buf, s_improv_rpc_res_len) == 0
                       ? 0
                       : BLE_ATT_ERR_INSUFFICIENT_RES;
        }
        portENTER_CRITICAL(&s_improv_lock);
        uint8_t val = (ch == 1) ? s_improv_state : ((ch == 2) ? s_improv_error : 0);
        portEXIT_CRITICAL(&s_improv_lock);
        return os_mbuf_append(ctxt->om, &val, sizeof(val)) == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
    }
    if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR && ch == 3) {
        bool allowed;
        portENTER_CRITICAL(&s_improv_lock);
        allowed = s_improv_active && s_improv_ble_enabled && !s_venue_offline;
        portEXIT_CRITICAL(&s_improv_lock);
        if (!allowed) {
            return BLE_ATT_ERR_WRITE_NOT_PERMITTED;
        }
        struct ble_gap_conn_desc desc;
        if (ble_gap_conn_find(conn_handle, &desc) != 0 || !desc.sec_state.encrypted) {
            return BLE_ATT_ERR_INSUFFICIENT_ENC;
        }
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
                {.uuid = &s_improv_state_uuid.u, .access_cb = improv_gatt_access, .arg = (void *)1, .val_handle = &s_improv_state_val_handle, .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY},
                {.uuid = &s_improv_err_uuid.u, .access_cb = improv_gatt_access, .arg = (void *)2, .val_handle = &s_improv_err_val_handle, .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY},
                {.uuid = &s_improv_rpc_cmd_uuid.u, .access_cb = improv_gatt_access, .arg = (void *)3, .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_ENC},
                {.uuid = &s_improv_rpc_res_uuid.u, .access_cb = improv_gatt_access, .arg = (void *)4, .val_handle = &s_improv_rpc_res_val_handle, .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY},
                {0},
            },
    },
    {0},
};

static void improv_ble_advertise_start(void);

static int improv_ble_gap_event(struct ble_gap_event *event, void *arg)
{
    (void)arg;
    if (event == NULL) {
        return 0;
    }
    switch (event->type) {
    case BLE_GAP_EVENT_DISCONNECT:
    case BLE_GAP_EVENT_ADV_COMPLETE:
        if (s_improv_active && !s_have_ip && !s_venue_offline && s_improv_ble_enabled) {
            improv_ble_advertise_start();
        }
        break;
    default:
        break;
    }
    return 0;
}

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
    (void)ble_gap_adv_start(BLE_OWN_ADDR_PUBLIC, NULL, BLE_HS_FOREVER, &adv_params,
                            improv_ble_gap_event, NULL);
}

static void improv_nimble_host_task(void *param)
{
    (void)param;
    nimble_port_run();
    nimble_port_freertos_deinit();
}

static void improv_ble_gatt_start(void)
{
    if (s_venue_offline || !s_improv_ble_enabled) {
        return;
    }
    if (!s_nimble_started) {
        if (ble_hs_synced()) {
            return;
        }
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
static void improv_ble_notify_status(void) {}
static void improv_ble_notify_rpc_result(void) {}

static void improv_ble_gatt_start(void)
{
    (void)&improv_handle_rpc_command;
    if (s_venue_offline || !s_improv_ble_enabled) {
        return;
    }
}

static void improv_ble_gatt_stop(void) {}
#endif

static void on_improv_timeout(void *arg)
{
    (void)arg;
    bool was_active;
    portENTER_CRITICAL(&s_improv_lock);
    was_active = s_improv_active;
    s_improv_active = false;
    s_improv_state = IMPROV_STATE_AUTH_REQUIRED;
    portEXIT_CRITICAL(&s_improv_lock);
    if (!was_active) {
        return;
    }
    improv_ble_gatt_stop();
    ESP_LOGI(TAG, "Improv Wi-Fi BLE window (%d s) expired; continuing background scan",
             IMPROV_BLE_WINDOW_S);
}

void improv_ble_window_start(void)
{
    if (s_venue_offline || !s_improv_ble_enabled || s_improv_active || s_have_ip) {
        return;
    }
    int64_t now_us = esp_timer_get_time();
    if (s_improv_open_count >= IMPROV_BLE_MAX_OPENS_PER_BOOT ||
        (now_us - s_improv_last_open_us) < IMPROV_BLE_COOLDOWN_US) {
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
    s_improv_open_count++;
    s_improv_last_open_us = now_us;
    portENTER_CRITICAL(&s_improv_lock);
    s_improv_active = true;
    s_improv_state = IMPROV_STATE_AUTH_REQUIRED;
    s_improv_error = IMPROV_ERR_NONE;
    portEXIT_CRITICAL(&s_improv_lock);
    improv_ble_gatt_start();
    if (s_improv_timer != NULL) {
        esp_timer_stop(s_improv_timer);
        esp_timer_start_once(s_improv_timer, (uint64_t)IMPROV_BLE_WINDOW_S * 1000000ULL);
    }
    ESP_LOGI(TAG, "opening Improv Wi-Fi BLE window (%s) for %d s (auth required)",
             IMPROV_BLE_SERVICE_UUID, IMPROV_BLE_WINDOW_S);
}

void improv_ble_window_stop(void)
{
    bool was_active;
    portENTER_CRITICAL(&s_improv_lock);
    was_active = s_improv_active;
    s_improv_active = false;
    s_improv_state = IMPROV_STATE_PROVISIONED;
    portEXIT_CRITICAL(&s_improv_lock);
    if (!was_active) {
        return;
    }
    if (s_improv_timer != NULL) {
        esp_timer_stop(s_improv_timer);
    }
    improv_ble_gatt_stop();
    ESP_LOGI(TAG, "closing Improv Wi-Fi BLE window");
}

void improv_ble_on_sta_disconnected(void)
{
    portENTER_CRITICAL(&s_improv_lock);
    bool was_provisioning = s_improv_state == IMPROV_STATE_PROVISIONING;
    if (was_provisioning) {
        s_improv_state = IMPROV_STATE_AUTH_REQUIRED;
        s_improv_error = IMPROV_ERR_UNABLE_TO_CONNECT;
    }
    portEXIT_CRITICAL(&s_improv_lock);
    if (was_provisioning) {
        improv_ble_notify_status();
    }
}

void improv_ble_on_got_ip(void)
{
    bool notify = false;
    portENTER_CRITICAL(&s_improv_lock);
    if (s_improv_state == IMPROV_STATE_PROVISIONING || s_improv_active) {
        s_improv_state = IMPROV_STATE_PROVISIONED;
        s_improv_error = IMPROV_ERR_NONE;
        notify = true;
    }
    portEXIT_CRITICAL(&s_improv_lock);
    if (notify) {
        improv_set_rpc_result(IMPROV_CMD_WIFI_SETTINGS);
        improv_ble_notify_status();
    }
    improv_ble_window_stop();
}

void ff_net_wifi_set_improv_ble_enabled(bool enabled)
{
    s_improv_ble_enabled = enabled;
    if (!enabled) {
        improv_ble_window_stop();
    }
}

void ff_net_wifi_improv_authorize(void)
{
    bool changed = false;
    portENTER_CRITICAL(&s_improv_lock);
    if (s_improv_active && s_improv_ble_enabled && !s_venue_offline &&
        s_improv_state == IMPROV_STATE_AUTH_REQUIRED) {
        s_improv_state = IMPROV_STATE_AUTHORIZED;
        s_improv_error = IMPROV_ERR_NONE;
        changed = true;
    }
    portEXIT_CRITICAL(&s_improv_lock);
    if (changed) {
        improv_ble_notify_status();
    }
}
