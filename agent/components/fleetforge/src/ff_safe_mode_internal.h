/*
 * ff_safe_mode, the component-private half. PRIVATE (src/, never reachable from a consumer's
 * main): see include/ff_safe_mode.h for the boot-time check. Only ff_identity.c calls this.
 */

#pragma once

#include <stdbool.h>

#include "ff_safe_mode.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ff_safe_mode_check()'s cached answer, for the announce: true while this boot is in safe
 * mode, false before the check ran. Any context; never logs, never blocks. Not a maker call:
 * in safe mode the firmware's own code never runs, so it could only ever read false. */
bool ff_safe_mode_active(void);

#ifdef __cplusplus
}
#endif
