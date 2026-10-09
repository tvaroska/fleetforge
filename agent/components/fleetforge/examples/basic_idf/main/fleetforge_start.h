/*
 * fleetforge_start() — over-the-air updates with automatic rollback, for an ESP-IDF app.
 * Copy this file and fleetforge_start.c into your project unchanged.
 *
 * Call it as the FIRST statement of app_main(). It
 *
 *  1. arms the OTA confirm timer: an image an OTA just wrote rolls back to the previous one
 *     unless it reaches the fleet server in time (a serially flashed image is unaffected);
 *  2. starts one FreeRTOS task, "fleetforge", that reads the board's ff_cfg partition,
 *     brings up the link, sets the clock, enrolls once, then keeps the MQTT session:
 *     announce, heartbeat, and stage / apply / confirm / rollback of updates;
 *
 * and returns at once, so your firmware runs alongside it. Returns false (and logs why) if
 * it was already called or the task cannot be created; the confirm timer is armed either way.
 *
 * What the library owns, so your firmware must not touch it: Wi-Fi/Ethernet and SNTP, NVS
 * namespace "ff", the ff_cfg partition, and the OTA confirm decision (never call
 * esp_ota_mark_app_valid_cancel_rollback()).
 */

#pragma once

#include <stdbool.h>

bool fleetforge_start(void);
