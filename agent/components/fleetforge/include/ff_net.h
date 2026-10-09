/*
 * ff_net — THE SEAM. One narrow interface, two implementations, selection from
 * configuration, and a caller that knows about neither.
 *
 * design/architecture.md -> Transport already demanded this shape: "the agent keeps its
 * network setup behind `esp_netif` rather than calling `esp_wifi` directly". Everything
 * above this header — SNTP, the HTTPS enrol, the MQTT session — is link-agnostic and talks
 * only to the LwIP stack `esp_netif` has already wired up.
 *
 * It is the same local-vs-GCP adapter idiom the Python side uses (`storage/objectstore.py`,
 * `broker/dynsec.py`), written in C. Two concrete payoffs, today:
 *
 *  - `ff_net_openeth.c` is an OpenCores NIC that only exists inside QEMU, and it is what
 *    makes this task's acceptance criteria runnable on a box with no serial port and no
 *    board. Without the seam that adapter would be an #ifdef smeared through app_main.
 *  - the next link (cellular, Thread, a second Wi-Fi band) plugs in here and changes
 *    nothing above.
 *
 * This is the PUBLIC half (the `fleetforge` component's include/, additive-only from
 * R3-fw-2): bring the link up. What the link reports into up/announce (type, RSSI, SSID)
 * is component-private, in src/ff_net_internal.h.
 */

#pragma once

#include "esp_err.h"
#include "ff_cfg.h"
#include "freertos/FreeRTOS.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Bring up the configured link and block until DHCP has assigned an address.
 *
 * Safe to call again after a timeout: the adapter is started on the first successful call
 * only, and later calls just wait for the address it is still trying to get.
 *
 * Returns ESP_ERR_TIMEOUT if no IP arrives within `timeout`; the caller retries rather
 * than rebooting (a board that reboot-loops on a bad access point is indistinguishable
 * from a hardware fault, and reboots lose the serial log that says why).
 * ESP_ERR_NOT_SUPPORTED means this build has no adapter for the configured link. */
esp_err_t ff_net_bring_up(const ff_cfg_t *cfg, TickType_t timeout);

#ifdef __cplusplus
}
#endif
