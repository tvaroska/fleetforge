/*
 * ff_time, the component-private half — the clock helpers. PRIVATE (src/, never
 * reachable from a consumer's main): see include/ff_time.h for the sync and why it runs
 * before any TLS socket opens.
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "ff_time.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Is the wall clock plausible (year >= 2024)? The one test that distinguishes "SNTP
 * worked" from "still at epoch 0" without pretending to know the real time. */
bool ff_time_is_sane(void);

/* "1970-01-01T00:00:00Z" / "2026-09-09T11:22:33Z" into a caller-provided buffer
 * (>= 21 bytes). Used for the before/after log pair and for `enrolled_at`. */
void ff_time_iso8601(char *out, size_t len);

#ifdef __cplusplus
}
#endif
