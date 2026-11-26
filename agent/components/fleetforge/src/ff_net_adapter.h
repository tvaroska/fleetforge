/*
 * ff_net_adapter — the private side of the ff_net Port: what an adapter must provide, and
 * the one callback it uses to say "there is an IP".
 *
 * Not included by anything above ff_net.c. Keeping it out of ff_net.h is the point of the
 * Port boundary: the enroll and MQTT layers must not be able to reach a link-specific symbol even
 * by accident, or the abstraction stops being one.
 */

#pragma once

#include "esp_err.h"
#include "esp_netif.h"
#include "ff_cfg.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Start the link. Returns as soon as the driver is running — NOT when an IP exists; the
 * adapter reports that by calling ff_net_report_got_ip() from its IP event handler.
 * ESP_ERR_NOT_SUPPORTED means this build was not compiled with that adapter. */
esp_err_t ff_net_wifi_start(const ff_cfg_t *cfg);
esp_err_t ff_net_openeth_start(const ff_cfg_t *cfg);

/* RSSI in dBm from the associated AP, or false when there is no radio / no association. */
bool ff_net_wifi_rssi(int *out_dbm);

/* The SSID of the known network that gave this board its address, or NULL while none has. */
const char *ff_net_wifi_ssid(void);

/* Total number of known networks (NVS `ff_net` overlay + `ff_cfg`), or 0 before start. */
int ff_net_wifi_known_count(void);

/* Persist a newly provisioned Wi-Fi network to the NVS `ff_net` overlay (`ssid_0`/`psk_0`)
 * and append it after flashed `ff_cfg` networks in the active scan list without reflashing `ff_cfg`. */
esp_err_t ff_net_wifi_save_nvs(const char *ssid, const char *passphrase);

/* CUJ-3: Venue Offline Mode radio silence (`esp_wifi_stop()`), Improv BLE suppression,
 * and physical/serial authorization release (D-8). */
esp_err_t ff_net_wifi_set_offline_mode(bool offline);
bool ff_net_wifi_is_offline_mode(void);
void ff_net_wifi_set_improv_ble_enabled(bool enabled);
void ff_net_wifi_improv_authorize(void);

/* Called by an adapter's IP_EVENT handler. Unblocks ff_net_bring_up(). */
void ff_net_report_got_ip(esp_netif_t *netif);

#ifdef __cplusplus
}
#endif
