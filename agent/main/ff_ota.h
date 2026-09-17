/*
 * ff_ota — the device half of a deploy: download, write the inactive slot, verify, apply.
 *
 * CRITICAL.md: "A/B slot apply logic (agent firmware) — writing the wrong slot, or a
 * non-atomic switch, bricks the device." This is the first code in the agent that moves
 * the boot partition, and the only code that can move it back.
 *
 * Scope at R1 (R1-fw-1). The walk ends at `rebooting` → esp_restart(): `confirming`,
 * `confirmed`, `rolling_back` and `rolled_back` belong to the confirm/rollback pair
 * already living in ff_mqtt.c, which this task makes LIVE for the first time (an image
 * written by OTA boots ESP_OTA_IMG_PENDING_VERIFY). Nothing is persisted across the
 * reboot: the board that comes back up simply announces, and `cmd_id` dies with the
 * old image. R2 owns the state machine that survives a reset.
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
 * ESP_ERR_INVALID_STATE — an update is already running; the caller publishes `failed`
 *                         for the new cmd_id, which is the honest answer.
 * ESP_ERR_NO_MEM        — the task or its copy could not be allocated. */
esp_err_t ff_ota_start(const ff_ota_cmd_t *cmd);

#ifdef __cplusplus
}
#endif
