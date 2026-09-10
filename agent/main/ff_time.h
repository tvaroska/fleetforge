/*
 * ff_time — SNTP, and the reason it runs before anything opens a TLS socket.
 *
 * spec/device-protocol.md -> "Clock — SNTP before TLS": an ESP32 wakes at epoch 0 (1 Jan
 * 1970). Every certificate on earth has a notBefore in the past *relative to now* and the
 * future relative to 1970, so the first HTTPS handshake fails with "certificate is not yet
 * valid" — an error that reads like a broken server and has sent people to look at the
 * wrong machine for an afternoon. Syncing first turns a confusing TLS failure into either
 * a working connection or one honest line about the NTP server.
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Start SNTP against `server` and block up to `timeout_ms` for the first sync.
 *
 * An empty `server` means "no NTP configured": this logs a WARNING naming the
 * consequence (TLS will fail on any https:// or mqtts:// endpoint) and returns
 * ESP_ERR_INVALID_STATE without starting anything. A timeout is ESP_ERR_TIMEOUT and is
 * NOT fatal — the caller decides, and on a plaintext lab setup an unsynced clock is
 * survivable. */
esp_err_t ff_time_sync(const char *server, uint32_t timeout_ms);

/* Is the wall clock plausible (year >= 2024)? The one test that distinguishes "SNTP
 * worked" from "still at epoch 0" without pretending to know the real time. */
bool ff_time_is_sane(void);

/* "1970-01-01T00:00:00Z" / "2026-09-09T11:22:33Z" into a caller-provided buffer
 * (>= 21 bytes). Used for the before/after log pair and for `enrolled_at`. */
void ff_time_iso8601(char *out, size_t len);

#ifdef __cplusplus
}
#endif
