/*
 * ff_net_adapter — the private side of the ff_net seam: what an adapter must provide, and
 * the one callback it uses to say "there is an IP".
 *
 * Not included by anything above ff_net.c. Keeping it out of ff_net.h is the point of the
 * seam: the enroll and MQTT layers must not be able to reach a link-specific symbol even
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

/* Called by an adapter's IP_EVENT handler. Unblocks ff_net_bring_up(). */
void ff_net_report_got_ip(esp_netif_t *netif);

#ifdef __cplusplus
}
#endif
