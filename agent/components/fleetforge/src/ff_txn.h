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

/* Same size as `ff_ota_cmd_t.cmd_id`: an id that did not fit there never got this far. */
#define FF_TXN_MAX_CMD_ID 64

typedef struct {
    char cmd_id[FF_TXN_MAX_CMD_ID];
    /* `esp_partition_t.address` of the slot the transaction booted into. An address and
     * not a label pointer, because it is compared across a reboot. */
    uint32_t target_addr;
} ff_txn_t;

/* Create the lock. Call once, before any other ff_txn_* call (ff_mqtt_run does, before
 * a `stage` can possibly arrive). Cannot fail: the mutex is statically allocated. */
void ff_txn_init(void);

/* Replace whatever record exists with this one. The namespace is erased first, so a power
 * cut part-way leaves either nothing or a record missing a key — which ff_txn_load treats
 * as nothing — and never an old cmd_id paired with a new slot. */
esp_err_t ff_txn_save(const char *cmd_id, uint32_t target_addr);

/* ESP_OK                  — *out holds the record.
 * ESP_ERR_NVS_NOT_FOUND   — there is none (a record missing either key counts as none: it
 *                           is logged at WARN and cleared).
 * anything else           — NVS could not be read; treat as none. */
esp_err_t ff_txn_load(ff_txn_t *out);

/* Erase the record ONLY if it is for `cmd_id`. Load-bearing: the PUBACK for an old
 * transaction's terminal state can arrive after a NEW `stage` has verified and saved its
 * own record, and an unconditional clear would erase the new transaction's outcome.
 * ESP_OK when the record was cleared or there was none; ESP_ERR_INVALID_STATE when a record
 * for another cmd_id is present (and was left alone). */
esp_err_t ff_txn_clear_if(const char *cmd_id);

#ifdef __cplusplus
}
#endif
