/*
 * Fleetforge for Arduino — over-the-air updates with automatic rollback for ESP32 sketches.
 *
 * This is the whole Arduino API: begin(), metric(), onSelfTest(), selfTestDone(),
 * onSafeWindow(), setPowerClass(), isConnected() and isReadyForSleep(). It is
 * ADDITIVE-ONLY from R3-fw-3, like the component's include/: a firmware built on it goes to
 * boards nobody can reach again.
 *
 *     #include <Fleetforge.h>
 *
 *     void setup() {
 *       Fleetforge.begin("1.0.0");   // FIRST line of setup(); your firmware's version
 *       Fleetforge.onSelfTest(selfTest);   // optional: right after begin()
 *       Fleetforge.onSafeWindow(safeNow);  // optional: then this
 *       Fleetforge.setPowerClass(Fleetforge.SLEEPY, 3600);  // optional: a board that sleeps
 *       Serial.begin(115200);
 *       ...
 *     }
 *
 *     void loop() {
 *       Fleetforge.metric("battery_v", readBattery());   // optional: a number for the fleet
 *       Fleetforge.selfTestDone(true);   // optional: this firmware works
 *       ...
 *       if (Fleetforge.isReadyForSleep()) esp_deep_sleep(3600ULL * 1000000);  // optional
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
 *     / rollback of updates. setup() and loop() keep running; begin() returns at once,
 *     except in safe mode (point 3).
 *  3. Safe mode (R5-fw-6): after three crash resets (a panic or a watchdog) within 60 s on
 *     this image, begin() starts the library and never returns, so the rest of setup() and
 *     loop() do not run until an update, a reboot command or a power cycle. The board stays
 *     online and announces `"safe_mode": true`, so a crash-looping sketch can be updated out
 *     of trouble. A brownout does not count; crashes of an update still waiting for its
 *     confirm do not either (it rolls back instead).
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

#include <stdint.h>

#ifdef __cplusplus

class FleetforgeClass {
 public:
  constexpr FleetforgeClass() = default;

  // Call as the FIRST line of setup(). fw_version: this firmware's version, a string
  // literal compiled into the image (1-31 printable ASCII); it is what the fleet dashboard
  // shows. Returns false (and logs why) if called twice, if fw_version is invalid, or if
  // the task cannot be created. In safe mode it does not return at all (see point 3 above).
  bool begin(const char *fw_version);

  // Report one sample of a metric to the fleet (`up/telemetry`, R4-fw-2). Safe to call
  // before begin(), from any task (not from an ISR), connected or not: it records the
  // sample in RAM (the newest 64 are kept) and never blocks. The library sends samples
  // within about 5 s, or after the next connect, with the time they were taken.
  // name: 1-31 of a-z, 0-9 and _, starting with a letter; names starting "ff_" are
  // reserved for the library. value: a finite number. Returns false, recording nothing,
  // when the name or the value is not acceptable.
  bool metric(const char *name, double value);

  // Register this sketch's self-test (R5-fw-1): keep an update only if the sketch says it
  // works. Call it right after begin() in setup(). The library calls test() once, in its
  // own task (not loop()'s), after an image an OTA just wrote has reached the fleet, and
  // keeps the update only if the sketch answers selfTestDone(true) before the confirm
  // deadline; otherwise the board returns to the previous image. A serially flashed board
  // never calls it.
  // Returns false for nullptr, for a second registration, or once the library's session
  // has started (so do not register after slow initialisation).
  bool onSelfTest(void (*test)());

  // Answer the self-test, from inside test() or later from loop() or any task (not from an
  // ISR). reason: why it failed, printable ASCII, the fleet sees at most 27 characters;
  // ignored on a pass. The first answer wins. Returns true only when this call decided the
  // result; a call when nothing is waiting returns false and is harmless, so loop() may
  // call it on every pass.
  bool selfTestDone(bool passed, const char *reason = nullptr);

  // Register when this sketch may be rebooted (R5-fw-5): isSafe() returns true when a reboot
  // right now is safe (between two jobs, say). Every reboot the library makes waits for it:
  // applying an update, rolling one back, and the fleet's rollback and reboot commands. The
  // fleet sees the board waiting. Crashes, watchdogs and power loss do not wait, and the
  // confirm deadline keeps counting: a rollback is decided on time, only its reboot waits.
  // Call it right after begin() (and onSelfTest()) in setup(). isSafe() must be quick and
  // must not block (read a flag); the library calls it from its own tasks, every 250 ms or
  // so while a reboot waits. Without it every moment is safe.
  // Returns false for nullptr, for a second registration, or once the library's session
  // has started (so do not register after slow initialisation).
  bool onSafeWindow(bool (*isSafe)());

  // How the fleet decides this board is offline (R5-fw-7): ALWAYS_ON, offline as soon as its
  // connection drops; SLEEPY, offline only when it misses its expected wake.
  enum PowerClass : uint8_t { ALWAYS_ON = 0, SLEEPY = 1 };

  // Declare this sketch's power class: Fleetforge.setPowerClass(Fleetforge.SLEEPY, 3600) for
  // a board that deep-sleeps and wakes every hour. The board announces it instead of the
  // power class in its ff_cfg (which no update rewrites), so a sketch that starts sleeping
  // through an update is not shown offline after its first sleep. SLEEPY needs a wake
  // interval of 1 to 2147483647 seconds; ALWAYS_ON takes 0. Call it right after begin() (and
  // onSelfTest()/onSafeWindow()) in setup().
  // Returns false for an invalid class or interval, for a second call, or once the library's
  // session has started (so do not call it after slow initialisation).
  bool setPowerClass(PowerClass powerClass, uint32_t wakeIntervalSeconds = 0);

  // True while the board is connected to the fleet: its broker session is up and the fleet
  // acknowledged its announce on it. False before begin() and while the link or the broker
  // is down. Call it from loop() or any task, not from an ISR. It never logs.
  bool isConnected();

  // True when a deep sleep right now loses nothing and breaks nothing. A deep-sleep wake is a
  // reset, and a reset boots whatever update is staged and rolls back one not yet confirmed,
  // so it is false until this wake has checked in with the fleet, while an update is in
  // progress or staged (until its apply), while the running image waits for its confirm or
  // self-test, while a rollback or a reboot waits, and while a message to the fleet is not
  // yet acknowledged. A metric() sample is published within about 5 s, so readiness can lag
  // the last metric() by that much. Call it from loop() or any task, not from an ISR; it may
  // wait briefly for the connection, and it never logs. Give your loop its own wake limit
  // (a wake that cannot reach the fleet never becomes ready), longer than the confirm
  // deadline (300 s by default) so a slow network does not roll an update back.
  bool isReadyForSleep();
};

extern FleetforgeClass Fleetforge;

#endif
