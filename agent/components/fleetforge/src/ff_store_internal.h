/*
 * ff_store, the component-private half — the NVS namespace and keys, and the
 * rollback_capable observation. PRIVATE (src/, never reachable from a consumer's main):
 * see include/ff_store.h for the credential and the reason this file is careful.
 */

#pragma once

#include <stdbool.h>

#include "esp_err.h"
#include "ff_store.h"

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
