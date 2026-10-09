/*
 * ff_enroll — POST {api_base}/v1/enroll: a single-use token in, a broker credential out.
 *
 * The C twin of `src/fleetforge/simulator/client.py`. It exists to be the same
 * conversation the simulator has, so a board and a simulated board are indistinguishable
 * to the server (that is the whole point of R0-test-1's simulator: if these two ever
 * disagree, one of them is wrong and the tests only cover the other).
 */

#pragma once

#include "esp_err.h"
#include "ff_cfg.h"
#include "ff_store.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Enroll once. On ESP_OK, `out` holds a complete credential the caller must persist
 * BEFORE connecting to the broker.
 *
 * Returns ESP_ERR_INVALID_RESPONSE for a server answer that is refused for good
 * (401/409/400/422 — a new token or a firmware fix is needed, retrying changes nothing),
 * and ESP_FAIL / a transport error for anything worth retrying. Every branch logs one
 * actionable line. The token, the password and the raw response body are never logged. */
esp_err_t ff_enroll(const ff_cfg_t *cfg, ff_cred_t *out);

#ifdef __cplusplus
}
#endif
