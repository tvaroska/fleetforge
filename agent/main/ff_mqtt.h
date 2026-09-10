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
 */

#pragma once

#include "esp_err.h"
#include "ff_cfg.h"
#include "ff_store.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Connect and stay connected. Does not return under normal operation: esp-mqtt owns
 * reconnection (with its own backoff) and this call blocks on the session forever.
 * Returns only if the client could not be created or started at all. */
esp_err_t ff_mqtt_run(const ff_cfg_t *cfg, const ff_cred_t *cred);

#ifdef __cplusplus
}
#endif
