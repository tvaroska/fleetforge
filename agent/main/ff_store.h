/*
 * ff_store — the credential in NVS. The device-side half of "a token is single-use".
 *
 * The whole reason this file is careful: `src/fleetforge/simulator/state.py` states the
 * rule the hardware has to obey too — *state present means never enroll again*, and a
 * *corrupt* state is a loud failure, not a fall-through to enrollment. Falling through
 * burns a second token per boot, and since a token is single-use the second attempt fails
 * anyway; the board ends up permanently offline AND the operator's token pool is empty.
 * So: absent and corrupt are different answers, and only absent enrolls.
 *
 * The stored password is a fleet credential. It is written before the first MQTT connect
 * (spec/flows.md Flow 1: persist, then connect — a crash between the two otherwise loses
 * a credential that exists exactly once, in one HTTP response body) and it is never
 * logged, not even truncated.
 *
 * S0-fw-4: NOTHING IN THIS SYSTEM ERASES NVS WHOLESALE ANY MORE. The browser flasher used
 * to fill the whole `nvs` partition with 0xFF on every flash to stop a re-flashed board
 * reusing its old credential; the cached RF calibration lives in the same partition, in
 * IDF's `phy` namespace, so that also cost every flashed board the cold full calibration —
 * the largest current draw in startup — on every boot, forever. Deciding *when* a board
 * discards its credential moved here (`ff_store_sync_token`), because a flasher writing
 * raw bytes cannot act on one namespace and this can. The `phy` namespace is a neighbour
 * we must not evict: erase FF_STORE_NAMESPACE and nothing else. The one remaining
 * wholesale eraser is `agent_main.c::nvs_ready()`'s recovery path, for an NVS that cannot
 * be mounted at all.
 */

#pragma once

#include <stdbool.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* NVS namespace and keys. `mqtt_pass`, NOT the eleven-letter spelling the HTTP response
 * uses: tests/test_agent_partitions.py::test_agent_holds_no_credential greps every file
 * under agent/ for that word so a real credential can never be committed here. The short
 * key is also under NVS's 15-character limit, which the long one would not be. */
#define FF_STORE_NAMESPACE "ff"
#define FF_STORE_KEY_DEVICE_ID "dev_id"
#define FF_STORE_KEY_MQTT_USER "mqtt_user"
#define FF_STORE_KEY_MQTT_PASS "mqtt_pass"
#define FF_STORE_KEY_API_BASE "api_base"
#define FF_STORE_KEY_ENROLLED_AT "enrolled_at"
#define FF_STORE_KEY_TOKEN_FP "tok_fp"
/* R2b-fw-2. u8, only ever 1: "an OTA-written image booted in PENDING_VERIFY on this board",
 * the measurement behind the announce's `rollback_capable: true`. It lives in THIS
 * namespace, next to the credential, on purpose:
 *   1. ff_store_sync_token() erases the namespace whenever the ff_cfg token changes. A
 *      re-flash through our flasher always carries a new token and writes a new
 *      bootloader, so a reading cannot outlive the bootloader it was taken on.
 *   2. An OTA keeps it (an OTA rewrites neither the bootloader nor ff_cfg).
 *   3. A tokenless config (the QEMU smoke run) never erases it.
 *   4. ff_store_load() probes `mqtt_pass`, so a namespace holding only `rb_cap` does not
 *      fake a credential.
 * NOT in `ff_txn`: ff_txn_save() erases its own namespace at every stage, which would
 * erase the observation. The known gap is a flash that rewrites the bootloader without a
 * new token (Arduino IDE upload, a future "keep identity" flash), accepted in
 * docs/features/board-profiles.md. */
#define FF_STORE_KEY_ROLLBACK_CAPABLE "rb_cap"

#define FF_CRED_MAX_USER 64
#define FF_CRED_MAX_PASS 128
#define FF_CRED_MAX_API_BASE 160
#define FF_CRED_MAX_TIMESTAMP 24
/* 16 hex characters + NUL. See ff_store.c: the first 8 bytes of sha256(token). A digest
 * and not the token, because this one is loggable and a live single-use fleet-join
 * credential is not. */
#define FF_CRED_MAX_TOKEN_FP 17

typedef struct {
    char device_id[FF_CRED_MAX_USER];
    char mqtt_user[FF_CRED_MAX_USER];
    char mqtt_pass[FF_CRED_MAX_PASS];
    char api_base[FF_CRED_MAX_API_BASE];
    char enrolled_at[FF_CRED_MAX_TIMESTAMP];
} ff_cred_t;

/* Read the stored credential.
 *
 *   ESP_OK                 — a complete credential; DO NOT enroll.
 *   ESP_ERR_NVS_NOT_FOUND  — nothing stored; enrollment is the correct next step.
 *   anything else          — present but unusable (partial write, oversized value, NVS
 *                            error). The caller must NOT enroll: see the header. */
esp_err_t ff_store_load(ff_cred_t *out);

/* Persist a credential. Committed before the first MQTT connect, never after. */
esp_err_t ff_store_save(const ff_cred_t *cred);

/* Reconcile the stored credential with the enrollment token now in ff_cfg.
 *
 * Call once per boot, after nvs_flash_init() and ff_cfg_load(), BEFORE ff_store_load().
 *
 * This is the device-side replacement for the flasher's NVS wipe (S0-fw-4). The browser
 * flasher used to fill the whole `nvs` partition with 0xFF to stop a re-flashed board
 * reusing its old credential; that also destroyed IDF's `phy` namespace, where the RF
 * calibration is cached, so every flashed board re-ran the cold full calibration — the
 * largest current draw in startup — on every boot, forever. A flasher writing raw bytes
 * cannot erase one namespace. This can, and it also covers boards re-flashed in the field
 * with agent/tools/ff_cfg.py, which a browser flasher never reaches.
 *
 * What it compares: a fingerprint of `token` against FF_STORE_KEY_TOKEN_FP. The token
 * stays in ff_cfg after enrollment (ff_enroll.c) and nothing blanks it, so the fingerprint
 * is stable across reboots and changes only when someone writes a new ff_cfg.
 *
 *   token absent          — nothing happens. A tokenless config (the QEMU smoke build, a
 *                           diagnostic flash) must never cost a board its credential.
 *   fingerprint matches   — nothing happens; the credential belongs to this token.
 *   fingerprint differs,
 *   or cannot be read     — the namespace is erased and the new fingerprint recorded. A
 *                           fingerprint we cannot compare is not proof of anything.
 *   fingerprint absent    — ADOPTED, not erased. See ff_store.c: erasing here would brick
 *                           a fleet the first time an OTA replaces the agent without
 *                           writing a new ff_cfg.
 *
 * Erases the FF_STORE_NAMESPACE namespace and nothing else. Never touches `phy`. Errors
 * are logged and returned; the caller continues, because a board that cannot read NVS has
 * bigger problems than a stale credential and ff_store_load() is about to say so. */
esp_err_t ff_store_sync_token(const char *token);

/* True when the credential was issued by the server this board is now configured for.
 * A false answer means the board was re-flashed at a different api_base while holding a
 * credential only the old server knows — worth one loud line, not a wipe: erasing would
 * throw away a working credential on nothing more than a hostname change. */
bool ff_store_matches_api_base(const ff_cred_t *cred, const char *api_base);

/* R2b-fw-2. True only when FF_STORE_KEY_ROLLBACK_CAPABLE reads back as exactly 1. Absent,
 * unreadable, a missing namespace or any other value is "unknown" and answers false;
 * nothing is logged when it is simply absent (every board before its first OTA). */
bool ff_store_load_rollback_capable(void);

/* R2b-fw-2. Record that this board's bootloader rolls back. No argument on purpose: this
 * agent can only ever store `true` (DECISIONS 2026-10-04 A3). Storing `false` is
 * R2b-test-5's follow-up. Errors are logged and returned. */
esp_err_t ff_store_save_rollback_capable(void);

#ifdef __cplusplus
}
#endif
