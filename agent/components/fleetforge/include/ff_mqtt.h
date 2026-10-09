/*
 * ff_mqtt — the session. Everything a connect-only agent is for happens in here.
 *
 * The wire behaviour is not invented here: `src/fleetforge/simulator/device.py` already
 * encodes it, R0-sec-1 verified the ACL semantics against it, and the ingestor depends on
 * all of it. ff_mqtt.c repeats those properties deliberately and says so at each one.
 *
 * This file also owns the device half of CRITICAL.md's "confirm timer / rollback path".
 * Both halves are guarded by ESP_OTA_IMG_PENDING_VERIFY and are therefore inert on a
 * serially flashed board — read the comments there before changing anything: confirming
 * unconditionally at boot would silently disable auto-rollback on the entire fleet from
 * the moment R2 ships OTA.
 *
 * The confirm timer is armed at boot, from app_main, before anything there can wait
 * forever (R2-fw-4): ff_mqtt_arm_confirm_timer() is the only ff_mqtt function called
 * before ff_mqtt_run().
 *
 * This is the PUBLIC half (the `fleetforge` component's include/, additive-only from
 * R3-fw-2). The `up/status` vocabulary and its publisher are component-private, in
 * src/ff_mqtt_internal.h: a firmware that could publish `confirmed` itself could report an
 * image confirmed that never confirmed.
 */

#pragma once

#include "esp_err.h"
#include "ff_cfg.h"
#include "ff_store.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Arm the OTA confirm timer (CRITICAL.md: "Device-side confirm timer / rollback path").
 * Call ONCE, as the FIRST statement of app_main — before anything that can wait forever
 * (network bring-up, enrollment, park()). Needs nothing but esp_timer and otadata: no NVS,
 * no netif, no mqtt client. Inert unless the running image is PENDING_VERIFY, i.e. was
 * written by OTA and has not confirmed yet; on such an image it guarantees a rollback
 * CONFIRM_TIMEOUT_S after boot unless the announce PUBACK confirms it first (R2-fw-4). */
void ff_mqtt_arm_confirm_timer(void);

/* Connect and stay connected. Does not return under normal operation: esp-mqtt owns
 * reconnection (with its own backoff) and this call blocks on the session forever.
 * Returns only if the client could not be created or started at all. */
esp_err_t ff_mqtt_run(const ff_cfg_t *cfg, const ff_cred_t *cred);

#ifdef __cplusplus
}
#endif
