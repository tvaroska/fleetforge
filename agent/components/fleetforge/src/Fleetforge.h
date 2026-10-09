/*
 * Fleetforge for Arduino — over-the-air updates with automatic rollback for ESP32 sketches.
 *
 * This is the whole Arduino API. It is ADDITIVE-ONLY from R3-fw-3, like the component's
 * include/: a firmware built on it goes to boards nobody can reach again.
 *
 *     #include <Fleetforge.h>
 *
 *     void setup() {
 *       Fleetforge.begin("1.0.0");   // FIRST line of setup(); your firmware's version
 *       Serial.begin(115200);
 *       ...
 *     }
 *
 * What begin() does, in order:
 *
 *  1. Arms the OTA confirm timer. On an image an OTA just wrote (the bootloader boots it
 *     in PENDING_VERIFY) the board rolls back to the previous image unless it reaches the
 *     fleet server in time. On a serially flashed image it does nothing. This happens
 *     before anything that can fail, so even a begin() that returns false leaves an OTA'd
 *     image able to roll back.
 *  2. Starts one FreeRTOS task, "fleetforge", that does what the stock Fleetforge agent
 *     does: reads the board's ff_cfg partition, brings up the link (Wi-Fi from the
 *     known-networks list, or Ethernet), sets the clock, enrolls once with the enrollment
 *     token, then keeps the MQTT session: announce, heartbeat, and stage / apply / confirm
 *     / rollback of updates. setup() and loop() keep running; begin() returns at once.
 *
 * What the library owns, so the sketch must not touch it:
 *
 *  - the network link and the clock: do not call WiFi.begin(), ETH.begin() or
 *    configTime();
 *  - the OTA confirm decision: do not define verifyRollbackLater() or verifyOta(), and do
 *    not call esp_ota_mark_app_valid_cancel_rollback(). The library overrides the Arduino
 *    core's verifyRollbackLater() so the core does not confirm an OTA'd image before
 *    setup() even runs; an image is confirmed only once it has reached the fleet;
 *  - NVS namespace "ff" (the broker credential) and the ff_cfg partition.
 *
 * Flash layout. The sketch MUST ship the example's partitions.csv (layout
 * `ab-4m-arduino-v1`) unchanged, next to the .ino: the board announces that layout and the
 * server deploys against it. The per-board config blob (agent/tools/ff_cfg.py) is flashed
 * at the table's ff_cfg offset, 0x3D0000 with this layout. "Erase All Flash Before Sketch
 * Upload" wipes it, and the enrolled credential with it.
 *
 * A build whose sdkconfig has CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE off does not compile:
 * an update that cannot roll back is not one this library will start.
 */

#pragma once

#ifdef __cplusplus

class FleetforgeClass {
 public:
  constexpr FleetforgeClass() = default;

  // Call as the FIRST line of setup(). fw_version: this firmware's version, a string
  // literal compiled into the image (1-31 printable ASCII); it is what the fleet dashboard
  // shows. Returns false (and logs why) if called twice, if fw_version is invalid, or if
  // the task cannot be created.
  bool begin(const char *fw_version);
};

extern FleetforgeClass Fleetforge;

#endif
