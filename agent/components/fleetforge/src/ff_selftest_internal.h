/*
 * ff_selftest, the component-private half — the session hooks. PRIVATE (src/, never
 * reachable from a consumer's main): see include/ff_selftest.h for the maker API. Only
 * ff_mqtt.c calls these.
 */

#pragma once

#include <stdbool.h>

#include "esp_err.h"
#include "ff_selftest.h"

#ifdef __cplusplus
extern "C" {
#endif

/* What the done callback gets: passed, and the sanitized reason ("" on a pass). Called once,
 * from the self-test task, never from the caller of ff_selftest_run(). `reason` is valid only
 * for the call. */
typedef void (*ff_selftest_done_fn)(bool passed, const char *reason);

/* ff_mqtt_run(), once, before the first connect: the registration is fixed from here.
 * Returns whether a self-test is registered. Logs which, once. */
bool ff_selftest_freeze(void);

/* Start the registered self-test in its own task. Non-blocking: safe from the esp-mqtt event
 * handler (R5-fw-2 calls it from the announce-ack branch). At most once per boot.
 * ESP_ERR_INVALID_STATE: not frozen, nothing registered, or already started.
 * ESP_ERR_NO_MEM: the task could not be created (state returns to "not started"). */
esp_err_t ff_selftest_run(ff_selftest_done_fn done);

#ifdef __cplusplus
}
#endif
