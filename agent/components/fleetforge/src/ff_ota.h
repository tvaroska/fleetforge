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
 *
 * The verbs (R5-fw-4): ff_ota_txn_phase() tells the command seam where a transaction stands
 * (in transfer, staged and waiting, or applying), so `apply`, `cancel`, `rollback` and
 * `reboot` can be answered without ff_mqtt.c ever reading or writing otadata for a stage.
 * ff_ota_cancel() stops a transfer, or puts the boot pointer back on the running image and
 * clears the record of a staged one: the transaction ends `cancelled`. `apply` itself needs
 * nothing from this module: the boot pointer already names the staged slot, so the session's
 * reboot is the apply.
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

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
    /* R5-fw-3: the command's deadline, already clamped by on_stage (30-3600 s;
     * CONFIRM_TIMEOUT_S when absent). ff_ota only records it. */
    uint32_t confirm_timeout_s;
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

/* R5-fw-4. Where an update transaction stands, for the verbs. */
typedef enum {
    FF_OTA_TXN_NONE = 0, /* no update for that cmd_id (or, for NULL, none at all) */
    FF_OTA_TXN_TRANSFER, /* staging/downloading/verifying, or finish() running: cancellable */
    FF_OTA_TXN_STAGED,   /* apply:on_command, record saved, boot pointer names it: waits */
    FF_OTA_TXN_APPLYING, /* apply:auto past staged: the ota task is about to reboot */
} ff_ota_txn_t;

/* R5-fw-4. The phase of the transaction `cmd_id`, or of whatever update this board holds when
 * `cmd_id` is NULL. With `held` non-NULL (`held_len` >= 1) it also receives the cmd_id of the
 * transaction found, or "" when none is found or the id is unknown (a slot the boot pointer
 * names with no record for it).
 *
 * A cancel already accepted for `cmd_id` reads as FF_OTA_TXN_NONE for that id, but a NULL
 * query keeps answering FF_OTA_TXN_TRANSFER until the ota task has ended: the boot pointer may
 * still be moving. STAGED outside the ota task means: the boot pointer names a slot that is
 * not the running one, and the transaction record (ff_txn) is for that slot and this cmd_id.
 * For a NULL query, a boot pointer on another slot alone answers STAGED: a reboot would boot
 * it.
 *
 * Read-only: no publish, no otadata write, no ff_txn save or clear (ff_txn_load() discards a
 * torn record, as at boot). Call from the esp-mqtt task, like ff_ota_start(). */
ff_ota_txn_t ff_ota_txn_phase(const char *cmd_id, char *held, size_t held_len);

/* R5-fw-4. Cancel the transaction `cmd_id` (R5-spec-1 D4). Call from the esp-mqtt task.
 *
 * ESP_OK                — accepted. `*reported` true: the ota task is still running and
 *                         publishes `cancelled` itself, at its next checkpoint (the transfer
 *                         is aborted, or the boot pointer put back, before it says so).
 *                         `*reported` false: the staged image was waiting; the boot pointer is
 *                         already back on the running image and the record cleared, and the
 *                         CALLER must report `cancelled` (enqueue: it runs in the handler).
 * ESP_ERR_NOT_FOUND     — nothing to cancel under that cmd_id.
 * ESP_ERR_INVALID_STATE — the transaction is past `staged` (apply:auto, about to reboot).
 * ESP_FAIL              — the staged image's boot pointer could not be put back; nothing else
 *                         was done, the record is untouched and it still boots at a reset.
 * ESP_ERR_INVALID_ARG   — NULL or "" cmd_id, or NULL `reported`. */
esp_err_t ff_ota_cancel(const char *cmd_id, bool *reported);

#ifdef __cplusplus
}
#endif
