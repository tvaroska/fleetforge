/*
 * ff_net_wifi_internal — private contract between STA scan/connect (`ff_net_wifi.c`)
 * and Improv BLE provisioning (`ff_net_wifi_improv.c`).
 */

#pragma once

#include <stdbool.h>

#include "ff_net_adapter.h"

#ifdef __cplusplus
extern "C" {
#endif

#ifdef FF_NET_WIFI_INTERNAL_IMPL
#define FF_WIFI_EVENT_RETRY 0x7F00
#define FF_WIFI_EVENT_IMPROV_CRED 0x7F01

#define s_have_ip ff_net_wifi__have_ip
#define s_venue_offline ff_net_wifi__venue_offline

#define improv_ble_window_start ff_net_wifi__improv_ble_window_start
#define improv_ble_window_stop ff_net_wifi__improv_ble_window_stop
#define improv_ble_on_sta_disconnected ff_net_wifi__improv_ble_on_sta_disconnected
#define improv_ble_on_got_ip ff_net_wifi__improv_ble_on_got_ip

extern volatile bool s_have_ip;
extern volatile bool s_venue_offline;

void improv_ble_window_start(void);
void improv_ble_window_stop(void);
void improv_ble_on_sta_disconnected(void);
void improv_ble_on_got_ip(void);
#endif

#ifdef __cplusplus
}
#endif
