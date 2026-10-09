/*
 * ff_identity, the component-private half — the device id, the layout contract, and the
 * three JSON bodies that say who this board is. PRIVATE (src/, never reachable from a
 * consumer's main): see include/ff_identity.h for the public half.
 *
 * The announce payload, the enroll body and the heartbeat are built HERE, in one place, so
 * that the identity presented at enrolment and the identity announced on the broker cannot
 * drift: spec/device-protocol.md step 2 says the enroll body IS `{token, <the announce
 * payload>}`, and here that is true by construction rather than by review.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "ff_cfg.h"
#include "ff_identity.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 12 hex digits + NUL. src/fleetforge/identity.py::DEVICE_ID_RE is the other end. */
#define FF_DEVICE_ID_LEN 12
#define FF_DEVICE_ID_SIZE (FF_DEVICE_ID_LEN + 1)

/* spec/device-protocol.md -> up/announce. Retyped with the spec named, per CRITICAL.md:
 * `partition_layout` + `ota_slot_size` are what let the server run spec/flows.md's
 * capability check, and agent/partitions.csv is the other half of the same contract. */
#define FF_PARTITION_LAYOUT "ab-4m-v1"
#define FF_OTA_SLOT_SIZE 1966080

/* spec/device-protocol.md -> Evolution rules: `proto` in announce is what lets the server
 * adapt per device, forever, to an agent it can never update. */
#define FF_PROTO_VERSION 1

/* R2b-fw-2. `rollback_capable` is MEASURED, never claimed (DECISIONS 2026-10-03 R2-spec-1):
 * it is never derived from CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE or any build setting,
 * because the bootloader is a flash-time immutable this image did not bring with it. The
 * one observation that proves it is an OTA-written image booting in PENDING_VERIFY — only
 * the bootloader ever writes that state. Call this when that has been seen: it sets the
 * announced value to true and persists it (once; no NVS write when it is already true).
 *
 * There is no way to note `false`: this agent emits `true` or null until R2b-test-5
 * benches what a rollback-less bootloader leaves behind (DECISIONS 2026-10-04 A3). */
void ff_identity_note_rollback_capable(void);

/* The 12-hex-digit device id. Valid after ff_identity_init(); "" before. */
const char *ff_device_id(void);

/* True when the eFuse MAC read back as all zeros — an emulator, never a board (see
 * ff_identity.c). Reported honestly rather than papered over. */
bool ff_identity_mac_is_blank(void);

/*
 * The three payloads. Each returns a heap string the caller must free() — cJSON's own
 * allocation, printed unformatted (compact), exactly as the simulator's `encode()` does.
 * NULL on allocation failure.
 */
char *ff_identity_announce_json(const ff_cfg_t *cfg);
char *ff_identity_enroll_body(const ff_cfg_t *cfg);
char *ff_identity_heartbeat_json(uint32_t uptime_s);

#ifdef __cplusplus
}
#endif
