/*
 * ff_mqtt, the component-private half — the `up/status` vocabulary and its publisher.
 * PRIVATE (src/, never reachable from a consumer's main): see include/ff_mqtt.h for the
 * session itself. Only ff_mqtt.c and ff_ota.c publish a transaction state; a firmware
 * built on the component never does.
 */

#pragma once

#include <stdbool.h>

#include "ff_mqtt.h"

#ifdef __cplusplus
extern "C" {
#endif

/* The `up/status` states this agent can publish, spelled once because the publisher
 * (ff_mqtt.c) and its only caller (ff_ota.c) have to agree byte for byte with
 * `src/fleetforge/ingestor/protocol.py` and with spec/device-protocol.md's machine.
 *
 * The first seven are ff_ota.c's walk up to the reboot. The last four are the outcome
 * (R2-be-1), and ONLY ff_mqtt.c publishes them, from the session that observed it:
 * `confirming` once the new image has a broker session, `confirmed` at the announce PUBACK
 * that confirms it, `rolling_back` best-effort when the confirm timer fires, and
 * `rolled_back` from the image the board RETURNED to.
 *
 * R5-fw-5: `awaiting_safe_window` is the wait for the firmware's safe-window rule
 * (ff_safe_window.h), and it is reported only when the rule says unsafe at the moment a reboot
 * is due: before an apply's reboot (pct 100, like `staged`/`applying`; detail
 * FF_DETAIL_APPLY_WAITS), published by ff_ota.c for apply:auto and enqueued by the session for
 * the `apply` command; and before a decided rollback's reboot (pct null, like `rolling_back`;
 * detail FF_DETAIL_ROLLBACK_WAITS), by the session only. A board with no rule never reports it.
 *
 * R5-fw-4: `cancelled` belongs to the pre-reboot walk too. It ends a transaction that the
 * `cancel` command stopped before its reboot: published by ff_ota.c's task when the cancel
 * caught the transfer (or the staged wait) still running, enqueued by the session when the
 * staged image was already waiting. It is ahead of the spec: R5-spec-1 Patch A3 adds it to
 * spec/device-protocol.md's machine, and is not applied yet. Never a status for the `cancel`
 * command itself, whose answer goes to `up/ack`. */
#define FF_STATUS_STAGING "staging"
#define FF_STATUS_DOWNLOADING "downloading"
#define FF_STATUS_VERIFYING "verifying"
#define FF_STATUS_STAGED "staged"
#define FF_STATUS_APPLYING "applying"
#define FF_STATUS_REBOOTING "rebooting"
#define FF_STATUS_FAILED "failed"
#define FF_STATUS_CONFIRMING "confirming"
#define FF_STATUS_CONFIRMED "confirmed"
#define FF_STATUS_ROLLING_BACK "rolling_back"
#define FF_STATUS_ROLLED_BACK "rolled_back"
#define FF_STATUS_CANCELLED "cancelled"
#define FF_STATUS_AWAITING_SAFE_WINDOW "awaiting_safe_window"

/* R5-fw-5, R5-spec-1 D12: the two `detail`s of `awaiting_safe_window`, which say which reboot
 * waits. Deliberately not named FF_STATUS_*: they are details, not states. Each fits the
 * 63-byte detail budget. */
#define FF_DETAIL_APPLY_WAITS "apply waits for a safe window"
#define FF_DETAIL_ROLLBACK_WAITS "rollback waits for a safe window"
#ifndef __cplusplus
_Static_assert(sizeof(FF_DETAIL_APPLY_WAITS) <= 64, "an up/status detail is at most 63 bytes");
_Static_assert(sizeof(FF_DETAIL_ROLLBACK_WAITS) <= 64, "an up/status detail is at most 63 bytes");
#endif

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
 * from the esp-mqtt event handler or a timer callback: it sends immediately, in the
 * caller's context, and ff_ota's drain timing before esp_restart() depends on that. The
 * outcome states published from the handler and from the confirm-timeout path use a
 * static enqueue variant in ff_mqtt.c instead (esp_mqtt_client_enqueue: queued, sent by
 * the mqtt task). Before the client exists it logs and drops. */
void ff_mqtt_publish_status(const char *cmd_id, const char *state, int pct, const char *detail);

/* R5-fw-7 (ff_sleep.h). A broker session is up and its announce was acknowledged on this
 * connection. Any task (never an ISR); never blocks, never logs. */
bool ff_mqtt_is_connected(void);

/* R5-fw-7 (ff_sleep.h). NULL when nothing the session holds forbids a deep sleep now, else a
 * short reason: this boot has not checked in, a rollback is decided or waits, a command reboot
 * is pending, the cross-reboot transaction is not closed, or the outbox is not empty. Task
 * context (never an ISR or the esp_timer task); may wait for the client lock (the outbox read);
 * never logs, never touches NVS. */
const char *ff_mqtt_sleep_blocker(void);

#ifdef __cplusplus
}
#endif
