/*
 * ff_identity — who this board says it is.
 *
 * `device_id` is the eFuse MAC as 12 lowercase hex digits with no separators
 * (spec/device-protocol.md -> Topic namespace). It is also the MQTT username, and the two
 * `%u` pattern ACLs in mosquitto/acl are the entire fleet authz — so this string is a
 * security boundary, not a label.
 *
 * This is the PUBLIC half of the `fleetforge` component's identity module: the two calls a
 * firmware makes. The device id, the announce / enroll / heartbeat bodies, the layout
 * contract and the rollback_capable observation are the component's own business and live
 * in src/ff_identity_internal.h (R3-fw-2: what is public here is additive-only, forever).
 */

#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Read the eFuse MAC and format the device id once. Call before anything publishes, and
 * after ff_store_sync_token() (it reads the stored rollback_capable observation, which a
 * token change erases).
 *
 * R2b-fw-2: it also takes the board's two flash-time measurements once — the physical
 * flash chip size and the partition table fingerprint — and loads `rollback_capable`. A
 * measurement that fails is logged and left unknown; it never fails this function, which
 * parks the board on failure. */
esp_err_t ff_identity_init(void);

/* The version this board is RUNNING, taken from the running image's own `esp_app_desc_t`
 * (R1-fw-2). This is the ONLY source for `fw_version` in up/announce and up/hb: after an
 * OTA the descriptor is the new slot's, and after a bootloader rollback it is the old
 * slot's again — with no state of ours to get wrong. It must never come from a `stage`
 * command (`ff_ota_cmd_t::version` is the version we were TOLD to install), from NVS, or
 * from a compile-time macro: those disagree with reality exactly when something went
 * wrong, which is the moment the field has to be right.
 *
 * Never NULL, and valid before ff_identity_init() — it reads no eFuse. */
const char *ff_identity_fw_version(void);

#ifdef __cplusplus
}
#endif
