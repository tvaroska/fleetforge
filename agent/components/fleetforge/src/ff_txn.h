/*
 * ff_txn — the one update transaction that has to survive the apply reboot (R2-be-1).
 *
 * `cmd_id` used to die with the image that received the `stage`, so the image the board
 * came back on could not say WHICH transaction it was confirming, and every deploy parked
 * at `rebooting` forever. This is the smallest thing that fixes that: a record of
 * `(cmd_id, target slot address)`, written by ff_ota the moment the boot pointer has moved
 * to a verified image, read once at boot by ff_mqtt, and cleared by ff_mqtt at the PUBACK
 * of the terminal `confirmed`/`rolled_back`.
 *
 * It is a STORE and nothing else. It makes no decisions: whether a record means
 * `confirming`, `confirmed` or `rolled_back` is decided in ff_mqtt.c from otadata, and
 * a record with no otadata evidence behind it is discarded, never reported.
 *
 * Its own namespace (FF_TXN_NAMESPACE), deliberately NOT ff_store's: ff_store erases its
 * namespace whenever the enrollment token changes, and that rule must stay about
 * credentials. Nothing here ever erases NVS wholesale — IDF's `phy` namespace lives in the
 * same partition (S0-fw-4).
 *
 * Losing a record loses a REPORT, never a rollback: the confirm timer and the bootloader
 * do not read this, and every failure here is logged and otherwise ignored by callers.
 *
 * R5-fw-2 adds one OPTIONAL third key, `detail`: the self-test outcome head (`selftest timed
 * out` or `selftest failed: <reason>`) that ff_mqtt.c writes while the image under test is
 * still running, so the image the board returns to can say WHY (`<head>; returned to
 * <slot>`). It never decides whether a record exists: a record without it (every record an
 * agent before R5 wrote) is complete, and ff_txn_save() erases it with the rest, so a stale
 * detail never survives into a new transaction.
 *
 * R5-fw-3 adds one more OPTIONAL key, `confirm_s`: the stage's `confirm_timeout_s`, already
 * clamped by the command seam, which the new image re-reads at boot to re-arm its confirm
 * deadline from image start. Written before `cmd_id`, so every complete record carries it; a
 * record without it (written before R5-fw-3) is complete and keeps the default deadline.
 */

#pragma once

#include <stdint.h>

#include "esp_err.h"
#include "nvs.h" /* ESP_ERR_NVS_NOT_FOUND is part of this API */

#ifdef __cplusplus
extern "C" {
#endif

#define FF_TXN_NAMESPACE "ff_txn"
#define FF_TXN_KEY_CMD_ID "cmd_id"
#define FF_TXN_KEY_TARGET "tgt_addr"
#define FF_TXN_KEY_DETAIL "detail" /* R5-fw-2: optional, see above */
#define FF_TXN_KEY_CONFIRM "confirm_s" /* R5-fw-3: optional, see above */

/* Same size as `ff_ota_cmd_t.cmd_id`: an id that did not fit there never got this far. */
#define FF_TXN_MAX_CMD_ID 64
/* ff_mqtt.c's s_txn.detail[64]: the whole wire `detail` is at most 63 bytes (R5-spec-1 D11). */
#define FF_TXN_MAX_DETAIL 64

typedef struct {
    char cmd_id[FF_TXN_MAX_CMD_ID];
    /* `esp_partition_t.address` of the slot the transaction booted into. An address and
     * not a label pointer, because it is compared across a reboot. */
    uint32_t target_addr;
    /* R5-fw-2: the self-test head stored by ff_txn_set_detail(), or "" (none stored, or a
     * record written before R5). */
    char detail[FF_TXN_MAX_DETAIL];
    /* R5-fw-3: the stage's confirm deadline in seconds. 0 = not stored (a pre-R5-fw-3
     * record); already clamped by the stager, and clamped again by the reader. */
    uint32_t confirm_timeout_s;
} ff_txn_t;

/* Create the lock. Call once, before any other ff_txn_* call (ff_mqtt_run does, before
 * a `stage` can possibly arrive). Cannot fail: the mutex is statically allocated. */
void ff_txn_init(void);

/* Replace whatever record exists with this one. The namespace is erased first, so a power
 * cut part-way leaves either nothing or a record missing a key — which ff_txn_load treats
 * as nothing — and never an old cmd_id paired with a new slot. `confirm_timeout_s` is
 * stored as given (R5-fw-3: the command seam has already clamped it). */
esp_err_t ff_txn_save(const char *cmd_id, uint32_t target_addr, uint32_t confirm_timeout_s);

/* ESP_OK                  — *out holds the record (`detail` "" and `confirm_timeout_s` 0
 *                           when none is stored).
 * ESP_ERR_NVS_NOT_FOUND   — there is none (a record missing cmd_id or target counts as
 *                           none: it is logged at WARN and cleared; a missing detail or
 *                           confirm deadline never does).
 * anything else           — NVS could not be read; treat as none. */
esp_err_t ff_txn_load(ff_txn_t *out);

/* Erase the record ONLY if it is for `cmd_id`. Load-bearing: the PUBACK for an old
 * transaction's terminal state can arrive after a NEW `stage` has verified and saved its
 * own record, and an unconditional clear would erase the new transaction's outcome.
 * ESP_OK when the record was cleared or there was none; ESP_ERR_INVALID_STATE when a record
 * for another cmd_id is present (and was left alone). */
esp_err_t ff_txn_clear_if(const char *cmd_id);

/* R5-fw-2. Store `detail` beside the record, ONLY if the record is for `cmd_id` (the
 * ff_txn_clear_if rule: never annotate a newer transaction with an older one's outcome).
 * Replaces any detail already stored; erases nothing else.
 * ESP_OK when stored; ESP_ERR_INVALID_ARG for NULL or a detail of FF_TXN_MAX_DETAIL bytes or
 * more; ESP_ERR_NVS_NOT_FOUND when there is no record; ESP_ERR_INVALID_STATE when the record
 * is for another cmd_id (left alone). Writes flash: never from esp_timer or under a
 * spinlock. */
esp_err_t ff_txn_set_detail(const char *cmd_id, const char *detail);

#ifdef __cplusplus
}
#endif
