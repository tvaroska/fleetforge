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

#define FF_CRED_MAX_USER 64
#define FF_CRED_MAX_PASS 128
#define FF_CRED_MAX_API_BASE 160
#define FF_CRED_MAX_TIMESTAMP 24

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

/* True when the credential was issued by the server this board is now configured for.
 * A false answer means the board was re-flashed at a different api_base while holding a
 * credential only the old server knows — worth one loud line, not a wipe: erasing would
 * throw away a working credential on nothing more than a hostname change. */
bool ff_store_matches_api_base(const ff_cred_t *cred, const char *api_base);

#ifdef __cplusplus
}
#endif
