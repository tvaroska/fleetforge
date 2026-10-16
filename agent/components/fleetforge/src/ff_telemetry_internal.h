/*
 * ff_telemetry, the component-private half — the session hooks. PRIVATE (src/, never
 * reachable from a consumer's main): see include/ff_telemetry.h for the maker API. Only
 * ff_mqtt.c calls the first two, from the esp-mqtt task, on the session's connect and
 * disconnect; ff_sleep.c calls the third.
 */

#pragma once

#include "ff_telemetry.h"
#include "mqtt_client.h"

#ifdef __cplusplus
extern "C" {
#endif

/* A broker session is up. `topic` is ff_mqtt.c's `ff/v1/d/<id>/up/telemetry`, which lives
 * as long as the process. Starts the flush task on the first call and wakes it to drain
 * what was buffered, AFTER on_connected() has queued the announce and the owed outcome. */
void ff_telemetry_session_up(esp_mqtt_client_handle_t client, const char *topic);

/* The session dropped: the flush task stops publishing and samples buffer until the next
 * ff_telemetry_session_up(). */
void ff_telemetry_session_down(void);

/* R5-fw-7 (ff_sleep.h). True when every recorded sample has been published (or none was
 * recorded). Any task, never an ISR; never logs, never blocks beyond the ring's spinlock,
 * never wakes the flush task (a loop asking every pass would otherwise publish one payload
 * per pass). */
bool ff_telemetry_drained(void);

#ifdef __cplusplus
}
#endif
