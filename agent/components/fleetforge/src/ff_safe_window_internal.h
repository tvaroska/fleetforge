/*
 * ff_safe_window, the component-private half — the session hooks. PRIVATE (src/, never
 * reachable from a consumer's main): see include/ff_safe_window.h for the maker API. Only
 * ff_mqtt.c and ff_ota.c call these.
 */

#pragma once

#include <stdbool.h>

#include "ff_safe_window.h"

#ifdef __cplusplus
extern "C" {
#endif

/* How often a waiting reboot asks the rule again (R5-spec-1: at most every second). 250 ms,
 * not 1000: a worked example's window is the 800 ms gap between two messages, and a 1 s poll
 * could phase-lock past it over and over. */
#define FF_SAFE_WINDOW_POLL_MS 250
#ifndef __cplusplus
_Static_assert(FF_SAFE_WINDOW_POLL_MS <= 1000, "R5-spec-1: poll the safe window at least every second");
#endif

/* ff_mqtt_run(), once, before the first connect: the registration is fixed from here.
 * Returns whether a rule is registered. Logs which, once. */
bool ff_safe_window_freeze(void);

/* Whether a rule is registered. Any context, the esp_timer task included: it reads the
 * registration under a spinlock and never calls the rule, never logs and never blocks. */
bool ff_safe_window_registered(void);

/* Whether a reboot right now is safe: true with no rule registered, otherwise the rule's
 * answer. TASK CONTEXT ONLY, never the esp_timer task or an ISR: it calls the maker's code
 * (outside any lock). Never logs. */
bool ff_safe_window_is_safe(void);

#ifdef __cplusplus
}
#endif
