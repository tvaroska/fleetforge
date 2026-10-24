/*
 * ff_log, the component-private half — session hooks for ff_mqtt.c and readiness check for
 * ff_sleep.c. PRIVATE (src/, never reachable from a consumer's main).
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "mqtt_client.h"

#ifdef __cplusplus
extern "C" {
#endif

void ff_log_session_up(esp_mqtt_client_handle_t client, const char *topic);
void ff_log_session_down(void);
bool ff_log_drained(void);
int ff_log_drain_json(char *buf, size_t cap);

#ifdef __cplusplus
}
#endif
