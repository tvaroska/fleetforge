/*
 * ff_telemetry, the component-private half — the session hooks. PRIVATE (src/, never
 * reachable from a consumer's main): see include/ff_telemetry.h for the maker API. Only
 * ff_mqtt.c calls these, from the esp-mqtt task, on the session's connect and disconnect.
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

#ifdef __cplusplus
}
#endif
