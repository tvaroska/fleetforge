/*
 * ff_sec — rotating project secrets in NVS (`ff_sec`), synced over HTTPS when `dn/cfg`
 * advances `secrets_rev` (SEC-fw-1).
 *
 * This header is part of the `fleetforge` component's PUBLIC maker API (`include/` is
 * ADDITIVE-ONLY: see tests/test_ota_component.py). A firmware built on the component reads
 * secrets by key with `ff_get_secret()` (or `Fleetforge.getSecret()` in Arduino) instead of
 * baking rotating backend API keys into the flash image.
 *
 * Contract:
 *   - `key`: 1..15 characters of `[A-Za-z0-9_-]` (NVS key limit), never `"_rev"`.
 *   - `out`: caller-supplied buffer of `cap >= 2` bytes; NUL-terminated on `ESP_OK`.
 *   - Returns `ESP_OK` when found, `ESP_ERR_NVS_NOT_FOUND` when not yet provisioned,
 *     `ESP_ERR_INVALID_SIZE` when the value does not fit `cap`, or `ESP_ERR_INVALID_ARG`
 *     for invalid arguments.
 *   - Never logs the secret value.
 */

#pragma once

#include <stddef.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t ff_get_secret(const char *key, char *out, size_t cap);

#ifdef __cplusplus
}
#endif
