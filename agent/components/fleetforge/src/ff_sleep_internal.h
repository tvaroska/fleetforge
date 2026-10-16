/*
 * ff_sleep, the component-private half — the session hooks. PRIVATE (src/, never reachable
 * from a consumer's main): see include/ff_sleep.h for the maker API. Only ff_mqtt.c (the
 * freeze) and ff_identity.c (the announce) call these.
 */

#pragma once

#include <stdbool.h>

#include "ff_cfg.h"
#include "ff_sleep.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ff_mqtt_run(), once, before the first connect: the declaration is fixed from here. Logs once
 * which class the session announces. */
void ff_sleep_freeze(const ff_cfg_t *cfg);

/* True (and fills both) only once frozen AND a class was declared; otherwise false and both
 * untouched. Any task; never logs, never blocks beyond a spinlock. ff_identity.c's
 * announce_object() is the one caller. `*power` is a string literal. */
bool ff_sleep_power_override(const char **power, int *wake_s);

#ifdef __cplusplus
}
#endif
