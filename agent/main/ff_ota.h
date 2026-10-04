/*
 * ff_ota — the device half of a deploy: download, write the inactive slot, verify, apply.
 *
 * CRITICAL.md: "A/B slot apply logic (agent firmware) — writing the wrong slot, or a
 * non-atomic switch, bricks the device." This is the first code in the agent that moves
 * the boot partition, and the only code that can move it back.
 *
 * Scope (R1-fw-1, R2-be-1). This walk ends at `rebooting` → esp_restart(): `confirming`,
 * `confirmed`, `rolling_back` and `rolled_back` belong to the confirm/rollback pair in
 * ff_mqtt.c (an image written by OTA boots ESP_OTA_IMG_PENDING_VERIFY). What crosses the
 * reboot is one record, `(cmd_id, target slot)`, which this module writes to NVS through
 * ff_txn the moment a verified image is bootable (`staged`). ff_mqtt.c reads it at the next
 * boot and reports the outcome against that cmd_id. This module never reports an outcome
 * itself: the session that observed it does.
 *
 * Verify before switch (R2-fw-1): the slot is read back and hashed BEFORE the boot pointer
 * moves, so an image that fails the digest is never bootable, not even for a moment.
 *
 * One slot, chosen once (R2-fw-2): a stage is refused before any I/O while the running
 * image is unconfirmed or a staged image waits for a reboot, so no slot the boot pointer
 * names is ever written.
 *
 * A re-delivered stage (R2-fw-6): ff_ota_is_handling() lets the command seam drop a stage
 * for the transaction already in flight or staged, instead of failing it.
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* A `stage` command, already parsed. Deliberately a plain C struct and not cJSON: the
 * command seam is `ff_mqtt.c::on_command()`, which is the single place the wire JSON is
 * interpreted, and ff_ota never sees a cJSON object or builds a topic string. */
typedef struct {
    char cmd_id[64];
    /* The signed download link (spec/device-protocol.md → `dn/cmd`). It is a BEARER
     * CREDENTIAL — the artifact endpoint is public and the signature is the whole
     * authorization — so it is never logged, not even truncated, and never put in a
     * `detail`. 512 bytes: our own `/v1/artifact/{sha}/bin?exp=&sig=` is short, but it
     * answers 307 to a presigned S3/GCS URL whose query string runs 400-500 bytes, and
     * `esp_http_client` rebuilds the request line from it. */
    char url[512];
    char sha256[65];        /* lowercase hex, 64 chars + NUL */
    size_t size;            /* bytes, from the command; 0 = the server did not say */
    char version[32];       /* the artifact's version, for the log only */
    bool apply_now;         /* false only for `apply: "on_command"` */
} ff_ota_cmd_t;

/* Spawn the OTA task and return immediately; every outcome is reported on `up/status`.
 *
 * Runs on its own task, not on the caller's, for two reasons that are both silent
 * failures: a QoS-1 publish from inside the esp-mqtt event handler can deadlock on the
 * client it is trying to use, and a multi-minute download in the event loop stops the
 * keepalive — the broker drops the session, the LWT fires, and the fleet watches the
 * board die in the middle of its own deploy.
 *
 * Takes a COPY of *cmd: the caller's struct lives on the event-handler stack.
 *
 * ESP_ERR_INVALID_STATE — an update for a DIFFERENT cmd_id is already running (the
 *                         caller has already dropped the same cmd_id via
 *                         ff_ota_is_handling()); the caller publishes `failed` for the new
 *                         cmd_id, which is the honest answer.
 * ESP_ERR_NO_MEM        — the task or its copy could not be allocated. */
esp_err_t ff_ota_start(const ff_ota_cmd_t *cmd);

/* True when `cmd_id` is the update this board is already carrying out: the download in
 * progress, or the image staged by `apply: "on_command"` that waits for a reboot (the
 * boot pointer names it and the ff_txn record is for this cmd_id). The caller drops such
 * a `stage` without a status (R2-fw-6): it is a re-delivery of a transaction this board
 * already has (spec/device-protocol.md → "A retried command reuses its id"), and a
 * `failed` against it would end the server's row while the update carries on.
 * Read-only. Call from the same task as ff_ota_start() (the esp-mqtt task). */
bool ff_ota_is_handling(const char *cmd_id);

#ifdef __cplusplus
}
#endif
