# Fleetforge: over-the-air updates for your own ESP32 firmware

This library enrolls your board with a Fleetforge server, announces it and keeps it
heartbeating, and runs the four-verb update (stage, apply, confirm, rollback) with automatic
rollback. It is the same C as the stock agent. This one directory is an Arduino library
(`Fleetforge.h`) and an ESP-IDF component.

## What your firmware does

- **Arduino / PlatformIO:** call `Fleetforge.begin(FW_VERSION)` as the first line of `setup()`.
- **ESP-IDF:** copy `fleetforge_start.c` and `fleetforge_start.h` unchanged from
  `examples/basic_idf/main/` and call `fleetforge_start()` first.

The library confirms an update only after the board has reached the fleet. A build that
cannot reach it is rolled back by the board itself.

## Telemetry

Report a number to the fleet from anywhere in your firmware:

- **Arduino / PlatformIO:** `Fleetforge.metric("battery_v", 3.71);` (returns `bool`).
- **ESP-IDF:** `#include "ff_telemetry.h"`, then `ff_telemetry("battery_v", 3.71);` (returns
  `ESP_OK` or `ESP_ERR_INVALID_ARG`).

A name is 1-31 characters of `a-z`, `0-9` and `_` and starts with a letter. Names starting
`ff_` are reserved for the library. The value is any finite number. The call never blocks: it
records the sample in RAM, where the newest 64 are kept, and it works before `begin()` and
while the board is offline. The library sends samples on `up/telemetry` at most once every
5 s while connected, and right after a reconnect. Each sample keeps the time it was taken, so
samples recorded offline arrive with their original spacing. A reboot loses samples not yet
sent. Both examples report `morse_loops`, one sample per blinked message.

## Structured logs & OpenTelemetry (`up/log`)

Send structured log lines from your firmware to the Fleetforge dashboard and any project-configured
OpenTelemetry (`OTLP/HTTP`) collector:

- **Arduino / PlatformIO:** `Fleetforge.logf('I', "refresh ok (%d ms)", elapsed_ms);` (returns `bool`).
- **ESP-IDF:** `#include "ff_log.h"`, then `ff_logf('I', "refresh ok (%d ms)", elapsed_ms);`
  (returns `ESP_OK` or `ESP_ERR_INVALID_ARG`).

Levels are `'D'`, `'I'`, `'W'`, and `'E'`. Up to 32 lines are buffered in RAM and flushed on
`up/log` every 5 s while connected; the newest 4 lines are also mirrored into crash-surviving
`RTC_NOINIT_ATTR` memory (`magic + CRC32`) so lines logged just before a panic or watchdog reset
are recovered and shipped on the next boot prefixed with `[prev-boot] `. `isReadyForSleep()` waits
for the log buffer to drain before allowing deep sleep.

## Rotating secrets without reflashing (`nvs/ff_sec`)

Read project-scoped rotating secrets (such as backend API keys) without reflashing firmware or
`ff_cfg`:

- **Arduino / PlatformIO:** `Fleetforge.getSecret("api_key", buf, sizeof(buf));` (returns `bool`).
- **ESP-IDF:** `#include "ff_sec.h"`, then `ff_get_secret("api_key", buf, sizeof(buf));`
  (returns `ESP_OK` or `ESP_ERR_NOT_FOUND`).

When you rotate a secret in the dashboard (`PUT /v1/projects/{slug}/secrets/{key}`), the server
publishes the bumped `secrets_rev` on the retained `dn/cfg` topic. When the board wakes or is
online and sees `dn/cfg.secrets_rev` greater than its local NVS revision, it fetches
`GET /v1/device-secrets` over HTTPS using its per-device broker credential, updates the `ff_sec`
NVS namespace atomically, and re-announces its new `secrets_rev`. `isReadyForSleep()` stays false
while a secret sync is in flight.

## Multi-Wi-Fi & Improv Wi-Fi over BLE

`ff_cfg` carries up to 4 known networks (`--net SSID:PSK`), and the Wi-Fi adapter also prepends up
to 4 runtime-provisioned networks stored in the `ff_net` NVS namespace (`ssid_0..3` / `psk_0..3`)
ahead of `ff_cfg`. When all known networks fail to associate, the board opens a bounded 120 s
Improv Wi-Fi over BLE (`00467768-6228-2272-4663-277478268000`) advertising window so you can add
a new Wi-Fi network from a phone or browser without reflashing.

## Self-test

Keep an update only if your firmware says it works, not just because it reached the fleet:

- **Arduino / PlatformIO:** `Fleetforge.onSelfTest(test);` right after `begin()` in
  `setup()`, then answer with `Fleetforge.selfTestDone(passed, reason)` (`reason` optional),
  from inside `test()` or later from `loop()` or any task.
- **ESP-IDF:** `#include "ff_selftest.h"`, then `ff_set_selftest(fn, ctx);` right after
  `fleetforge_start()`. `fn` returns `FF_SELFTEST_PASS`, `FF_SELFTEST_FAIL` (with a reason
  written into its buffer) or `FF_SELFTEST_PENDING`, and a pending test is answered later with
  `ff_selftest_report(passed, reason)`.

The library asks at most once per boot, and only on an image an OTA just wrote: after the
board has reached the fleet, in the library's own low-priority task (never `loop()`'s, never
an interrupt). The update is kept only if the answer is a pass before the confirm deadline;
a fail or no answer returns the board to the previous image. A serially flashed board is
never asked. One self-test per image; register it right after `begin()`/`fleetforge_start()`,
because a registration after the library's session has started is refused. The first answer
wins, and an answer when nothing is waiting is a harmless no-op, so `loop()` may answer on
every pass. A fail's reason is printable ASCII, and the fleet sees at most 27 characters of
it. Both examples keep an update only if their loop still runs: the test answers "pending",
and the loop answers a pass after every whole blinked message. An image that registers a
self-test announces the `selftest` capability. The deploy then ends `confirmed` with the detail
`selftest passed`, or `rolled_back` with `selftest failed: <reason>; returned to <slot>` or
`selftest timed out; returned to <slot>`.

## Safe window

Let your firmware say when the board may be rebooted, so an update or a rollback never cuts a
job short:

- **Arduino / PlatformIO:** `Fleetforge.onSafeWindow(isSafe);` right after `begin()` (and
  `onSelfTest()`) in `setup()`. `isSafe()` returns `true` when a reboot right now is safe.
- **ESP-IDF:** `#include "ff_safe_window.h"`, then `ff_set_safe_window_cb(fn, ctx);` right
  after `fleetforge_start()` (and `ff_set_selftest()`). `fn(ctx)` returns `true` when a reboot
  right now is safe.

Every reboot the library makes waits for it: applying an update (`apply: auto` or the `apply`
command), the rollback after a failed confirm or a failed self-test, and the `rollback` and
`reboot` commands. While it says unsafe the fleet sees `awaiting_safe_window` with the detail
`apply waits for a safe window` or `rollback waits for a safe window`, and a command is answered
`deferred`; when it says safe, the reboot follows within about a quarter of a second plus the
time the last messages need to leave. Crashes, watchdogs, brownouts and power loss do not wait.
The confirm deadline keeps counting while the rule says unsafe: a rollback is decided on time,
the image is not confirmed afterwards, and only the reboot waits, with no limit. A reset during
that wait returns the board to the previous image too. A rule that never says safe holds the
board until it is reset.

The rule must be quick and must not block (read a flag): the library calls it from its own
tasks, every 250 ms while a reboot waits and once more just before it, never from an interrupt.
One rule per image, never removed; a registration after the library's session has started is
refused. Without a rule every moment is safe. Both examples reboot only in the gap between two
blinked messages. An image that registers a rule announces the `safe_window` capability.

## Sleepy boards

A board that deep-sleeps between jobs declares it, and asks before each sleep whether a sleep
right now loses anything:

- **Arduino / PlatformIO:** `Fleetforge.setPowerClass(Fleetforge.SLEEPY, wakeSeconds);` right
  after `begin()` (and `onSelfTest()`/`onSafeWindow()`) in `setup()`; then
  `Fleetforge.isReadyForSleep()` and `Fleetforge.isConnected()` from `loop()`.
- **ESP-IDF:** `#include "ff_sleep.h"`, then `ff_set_power_class(FF_POWER_SLEEPY, wake_s);`
  right after `fleetforge_start()` and the other registrations; then `ff_is_ready_for_sleep()`
  and `ff_is_connected()` from your loop.

```cpp
void loop() {
  doTheWork();
  if (Fleetforge.isReadyForSleep() || millis() > WAKE_LIMIT_MS) {
    esp_deep_sleep(WAKE_SECONDS * 1000000ULL);
  }
  delay(100);
}
```

The power class tells the fleet when the board is offline: `always_on` as soon as its
connection drops, `sleepy` only when it misses its expected wake. The board announces the
declared class and interval instead of the ones in its `ff_cfg` blob, which is written at flash
time and never by an update, so firmware that starts sleeping through an update is not shown as
offline after its first sleep. `SLEEPY` needs an interval of 1 to 2147483647 seconds,
`ALWAYS_ON` takes 0. You can declare it once per image, before the library's session starts;
a later call is refused. The serial log says which class the board announces in place of the
`ff_cfg` value. The enrollment that may come before the first connection still carries the
`ff_cfg` class, and the first announce replaces it.

A deep-sleep wake is a reset. At a reset the board boots whatever update is staged, and rolls
back an update that is not yet confirmed. So `isReadyForSleep()` is false until this wake has
checked in with the fleet (an update queued while the board slept has started by then), and
again while any of these holds:

- an update is in progress, or staged with `apply: on_command` and waiting for its `apply`;
- the running image waits for its confirm or its self-test;
- the fleet has not yet acknowledged the outcome (`confirmed`, `rolled_back`) this boot owes it;
- a rollback is decided, or a rollback or reboot waits for the safe window;
- a message to the fleet is not yet acknowledged (heartbeats included, so a wake whose network
  dropped is normally not ready);
- a `metric()` sample has not been published yet (samples go out at most every 5 s, so readiness
  can lag the last `metric()` by that much).

`isConnected()` is true while the broker connection is up and the fleet has acknowledged this
board's announce on it. Neither function logs, so `loop()` can call them on every pass. Call
them from a task, never from an interrupt. `isReadyForSleep()` may wait briefly for the
connection. Give your loop its own wake limit, because a wake that cannot reach the fleet never
becomes ready. Make the limit longer than the confirm deadline (300 s by default) on a slow
network: if the board sleeps before a new image is confirmed, it rolls the update back. The
library itself never sleeps the board.

## Quickstart: pick your toolchain

| Toolchain | Go to |
|---|---|
| PlatformIO | [`examples/Basic/README.md`](examples/Basic/README.md) (steps 0-7) |
| Arduino IDE 2.x | [`examples/Basic/README.md`, *Arduino IDE*](examples/Basic/README.md#arduino-ide) |
| ESP-IDF v5.5 | [`examples/basic_idf/README.md`](examples/basic_idf/README.md) |
| ESP-IDF + LVGL v9 E-Paper | [`examples/eink_lvgl/README.md`](examples/eink_lvgl/README.md) |

No board? `just lib-quickstart` (from the repository root) plays the PlatformIO path in the
QEMU emulator against a local Fleetforge server.

## In your own PlatformIO project

Copy `Basic.ino`, `partitions.csv` and `platformio.ini` from the example (see
[*Your own project*](examples/Basic/README.md#your-own-project)). Then either use the clone and
symlink described there, or, without a clone, this line in `platformio.ini`:

```ini
lib_deps = Fleetforge=https://github.com/tvaroska/fleetforge.git#v0.5.0
```

PlatformIO finds the library in the repository's `agent/components/fleetforge` folder. Pin a
tag, never a branch.

## Requirements

- An ESP32 or ESP32-S3 with 4 MB of flash.
- PlatformIO: pioarduino `55.03.312-1` (Arduino-ESP32 core 3.3.12 on ESP-IDF 5.5.5).
  PlatformIO's official `espressif32` platform cannot build it.
- Arduino IDE: esp32 core `3.3.12` exactly.
- ESP-IDF: `>=5.5` for the component.
- A Fleetforge server.

## Flash layouts (never edit)

| Layout | Used by | `ff_cfg` at | OTA slot |
|---|---|---|---|
| `ab-4m-arduino-v1` | Arduino, PlatformIO | `0x3D0000` | 1966080 bytes |
| `ab-4m-v1` | ESP-IDF | `0x12000` | 1966080 bytes |

The board carries its partition table for life. A different table announces `unknown` and is
refused at deploy time until you name it in the dashboard (*Partition profiles*). Never erase
all flash: it wipes `ff_cfg` and the board's credential.

## Rollback is not optional

The library does not compile without `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE` (stock core
3.3.12 has it on).

## Version and marker

The library version is `0.5.0`, one number in `agent/version.txt`, `library.json` and
`src/ff_lib_version.h`. Every image that calls the library carries a 64-byte library marker.
The server records it at upload, and a build without one needs an explicit override
(`no_library_marker`) before it deploys. Both Arduino and ESP-IDF builds report the library's
version (`FF_LIB_VERSION`) in the announce's `agent_version`, while `fw_version` reports your
application's version (`FW_VERSION` in Arduino, `PROJECT_VER` in ESP-IDF).

## ESP-IDF hardware & e-paper bring-up notes

- **100 Hz FreeRTOS tick truncation (`pdMS_TO_TICKS(ms) == 0` for `ms < 10`):**
  ESP-IDF defaults to `CONFIG_FREERTOS_HZ=100` (10 ms per tick). Calling
  `vTaskDelay(pdMS_TO_TICKS(2))` integer-divides to `0` ticks (`vTaskDelay(0)`), producing a
  ~1 µs yield instead of a 2 ms delay. That is too short to wake SPI e-paper controllers
  (such as the SSD1677 after Deep Sleep `0x10 0x01`, which requires `RST` low for `>= 10 ms`
  and leaves `BUSY` stuck high otherwise). Use `pdMS_TO_TICKS(20)` for `>= 10 ms` task delays
  or `esp_rom_delay_us()` for sub-10 ms hardware pulses.
- **GPIO ESD-diode back-powering when cycling a PMIC/LDO rail:**
  An e-paper controller in deep sleep draws ~1 µA. If you turn off its PMIC LDO rail (e.g.,
  AXP2101 `ALDO3`) to force a Power-On Reset while `RST` or `CS` GPIOs remain driven high
  (`3.3 V`), current leaking through the controller's input ESD diodes keeps `VDD` above its
  POR threshold. Always drive `RST`, `CS`, `DC`, `MOSI`, and `SCLK` low (`0 V`) before
  disabling the LDO rail.
- **LVGL v9 `LV_COLOR_FORMAT_I1` 8-byte palette header:**
  In LVGL v9, `LV_COLOR_FORMAT_I1` full-screen buffers prepend an **8-byte palette header**
  (two 32-bit `lv_color32_t` entries for index 0 and index 1) ahead of the packed 1-bpp pixel
  rows. Size your buffer to `FB_BYTES + 8` and pass `px_map + 8` to the SPI panel driver inside
  your LVGL `flush_cb` (see [`examples/eink_lvgl/README.md`](examples/eink_lvgl/README.md)).

## Public headers

- `include/`: `ff_cfg.h`, `ff_enroll.h`, `ff_identity.h`, `ff_log.h`, `ff_mqtt.h`, `ff_net.h`,
  `ff_progress.h`, `ff_safe_window.h`, `ff_sec.h`, `ff_selftest.h`, `ff_sleep.h`, `ff_store.h`,
  `ff_telemetry.h`, `ff_time.h`. This is the ESP-IDF surface. `ff_log.h`, `ff_safe_window.h`,
  `ff_sec.h`, `ff_selftest.h`, `ff_sleep.h` and `ff_telemetry.h` are the maker API: your firmware
  calls them, the stock agent does not.
- `src/Fleetforge.h`: the Arduino surface. It lives in `src/` because the Arduino build
  reads `src/`.

The public surface is additive-only from 0.5.0 on.

## Not yet

- Not on the PlatformIO registry, the Arduino Library Manager or the ESP-IDF component
  registry.
- The Arduino IDE's `Maximum is 1310720 bytes` is the board menu, not the 1966080-byte slot.
- Private headers can be included from a sketch: PlatformIO and the IDE put all of `src/` on
  the include path.
