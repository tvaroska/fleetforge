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

/* The `up/status` states this agent can publish, spelled once because the publisher
 * (ff_mqtt.c) and its only caller (ff_ota.c) have to agree byte for byte with
 * `src/fleetforge/ingestor/protocol.py` and with spec/device-protocol.md's machine.
 *
 * R1 emits exactly these. `awaiting_safe_window` is absent because an always-on board has
 * no window to wait for, and `confirming`/`confirmed`/`rolling_back`/`rolled_back` are
 * absent because the confirm/rollback pair below is R2's to report on — this agent
 * confirms silently, at the announce PUBACK. */
#define FF_STATUS_STAGING "staging"
#define FF_STATUS_DOWNLOADING "downloading"
#define FF_STATUS_VERIFYING "verifying"
#define FF_STATUS_STAGED "staged"
#define FF_STATUS_APPLYING "applying"
#define FF_STATUS_REBOOTING "rebooting"
#define FF_STATUS_FAILED "failed"

/* `pct` for a state that has no meaningful percentage: published as JSON `null` rather
 * than as a plausible-looking 0, for the same reason ff_identity reports a null `rssi`. */
#define FF_STATUS_PCT_NONE (-1)

/* Publish one `up/status` transition — QoS 1 and **RETAINED**.
 *
 * Retained because spec/device-protocol.md's retain matrix says so: `up/status` is the
 * CURRENT STATE of an update transaction, so an outcome published while the ingestor was
 * down has to survive until it reconnects, and there is no ack to replay it with.
 *
 * `pct < 0` serialises as JSON `null` (the state has no meaningful percentage);
 * `detail` may be NULL, and must NEVER carry a URL — the signed artifact link is a
 * credential and `detail` is stored forever.
 *
 * Callable from any task once the session exists — it is called from ff_ota's task, never
 * from the esp-mqtt event handler. Before the client exists it logs and drops. */
void ff_mqtt_publish_status(const char *cmd_id, const char *state, int pct, const char *detail);

#ifdef __cplusplus
}
#endif
