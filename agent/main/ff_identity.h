/*
 * ff_identity — who this board says it is, and the three JSON bodies that say it.
 *
 * `device_id` is the eFuse MAC as 12 lowercase hex digits with no separators
 * (spec/device-protocol.md -> Topic namespace). It is also the MQTT username, and the two
 * `%u` pattern ACLs in mosquitto/acl are the entire fleet authz — so this string is a
 * security boundary, not a label.
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

/* Read the eFuse MAC and format the device id once. Call before anything publishes. */
esp_err_t ff_identity_init(void);

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
