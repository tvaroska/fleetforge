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
#if defined(ARDUINO)
/* R3-fw-3. The Arduino library ships its own map, `ab-4m-arduino-v1`
 * (design/decisions/arduino-gets-its-own-layout-id.md): the Arduino upload recipe writes
 * boot_app0 at 0xe000 and the app at 0x10000 whatever the table says, so ab-4m-v1's
 * offsets are unreachable from it. Its partitions.csv travels with the example
 * (examples/Basic/partitions.csv). `ARDUINO` is predefined by every Arduino builder, so
 * the IDF agent build compiles the #else line only and is unchanged. Same slot size. */
#define FF_PARTITION_LAYOUT "ab-4m-arduino-v1"
#else
#define FF_PARTITION_LAYOUT "ab-4m-v1"
#endif
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

#if defined(ARDUINO)
/* R3-fw-3, Arduino builds only. An Arduino build's esp_app_desc_t.version is the core's
 * own string (`esp-idf: v4.4.7 …` on core 2.0.17, the R3-spec-3 side finding; the
 * lib-builder hash `6671d0b` on core 3.3.12, re-measured by R3-fw-3), because
 * the descriptor comes from the core's precompiled libraries and a sketch cannot write it.
 * So the maker's firmware version, and the library's own version, are handed in here
 * instead: `fw_version` becomes `fw_version` in up/announce and up/hb, `agent_version`
 * becomes `agent_version` in up/announce (DECISIONS R3-spec-3: in a library build that is
 * the library's version).
 *
 * Called ONCE, by Fleetforge.cpp's begin(), before the task that reads either version is
 * created; nothing else may call it (tests/test_arduino_library.py). Both strings must be
 * constants compiled into the running image — that is what keeps R1-fw-2's "what booted,
 * never what a command asked for" true: neither is ever persisted, and ff_ota.c never
 * reaches them. Each must be 1-31 bytes of printable ASCII (0x20-0x7e); otherwise
 * ESP_ERR_INVALID_ARG and nothing is kept. */
esp_err_t ff_identity_set_app_versions(const char *fw_version, const char *agent_version);
#endif

#ifdef __cplusplus
}
#endif
