/*
 * ff_log — opt-in firmware log lines on `up/log`, with a crash-surviving RTC tail
 * (R4-fw-3, R4-fw-4, OTEL-fw-1).
 *
 * This header is part of the `fleetforge` component's PUBLIC maker API (`include/` is
 * ADDITIVE-ONLY: see tests/test_ota_component.py). The stock agent has no application code
 * and never includes it; a firmware built on the component includes it (or calls
 * `Fleetforge.logf()` in Arduino) to forward selected diagnostic lines to the fleet
 * ingestor and OpenTelemetry exporter.
 *
 * Contract:
 *   - Explicit opt-in only: nothing is forwarded unless the firmware calls `ff_logf()`.
 *   - `level`: one of `'E'`, `'W'`, `'I'`, `'D'`, `'V'` (any other byte becomes `'I'`).
 *   - `text` is formatted with `vsnprintf` and truncated to 200 bytes; control characters
 *     and newlines are replaced with spaces.
 *   - Lines are buffered in a 32-entry RAM ring (plus a 4-line magic+CRC `RTC_NOINIT_ATTR`
 *     crash ring that survives `panic` / watchdog resets and flushes on the recovery boot)
 *     and published in batches under 2048 bytes at most once every 5 s on `up/log` (QoS 0,
 *     not retained).
 *   - Never log a passphrase, enrollment token, secret value, or signed URL.
 */

#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t ff_logf(char level, const char *fmt, ...);

#ifdef __cplusplus
}
#endif
