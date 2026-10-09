/*
 * ff_net, the component-private half — what the link that came up reports into
 * up/announce. PRIVATE (src/, never reachable from a consumer's main): see
 * include/ff_net.h for the seam itself.
 *
 * `ff_net_rssi()` returns false rather than a number on a link that has no radio. An
 * invented RSSI is a lie in a health field, and the dashboard cannot tell it from a
 * reading.
 */

#pragma once

#include <stdbool.h>

#include "ff_net.h"

#ifdef __cplusplus
extern "C" {
#endif

/* "wifi" | "ethernet" — what actually came up, and what up/announce reports. */
const char *ff_net_link_type(void);

/* The current RSSI in dBm, or false on a link that has none (Ethernet). */
bool ff_net_rssi(int *out_dbm);

/* The SSID of the known network the board joined, or NULL: on a link that has none
 * (Ethernet), or while no network is joined. Reported as `ssid` in up/announce
 * (spec/device-protocol.md). The string lives as long as the agent; never a passphrase. */
const char *ff_net_ssid(void);

#ifdef __cplusplus
}
#endif
