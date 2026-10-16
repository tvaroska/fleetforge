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
and the loop answers a pass after every whole blinked message. Until R5-fw-2 the library
records the registration but does not run it yet: an OTA'd image still confirms on reaching
the fleet.

## Quickstart: pick your toolchain

| Toolchain | Go to |
|---|---|
| PlatformIO | [`examples/Basic/README.md`](examples/Basic/README.md) (steps 0-7) |
| Arduino IDE 2.x | [`examples/Basic/README.md`, *Arduino IDE*](examples/Basic/README.md#arduino-ide) |
| ESP-IDF v5.5 | [`examples/basic_idf/README.md`](examples/basic_idf/README.md) |

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
(`no_library_marker`) before it deploys. In an Arduino build the announce's `agent_version`
is the library's version; in an ESP-IDF build it is your `PROJECT_VER`.

## Public headers

- `include/`: `ff_cfg.h`, `ff_enroll.h`, `ff_identity.h`, `ff_mqtt.h`, `ff_net.h`,
  `ff_progress.h`, `ff_selftest.h`, `ff_store.h`, `ff_telemetry.h`, `ff_time.h`. This is the
  ESP-IDF surface. `ff_selftest.h` and `ff_telemetry.h` are the maker API: your firmware calls
  them, the stock agent does not.
- `src/Fleetforge.h`: the Arduino surface. It lives in `src/` because the Arduino build
  reads `src/`.

The public surface is additive-only from 0.5.0 on.

## Not yet

- Not on the PlatformIO registry, the Arduino Library Manager or the ESP-IDF component
  registry.
- The Arduino IDE's `Maximum is 1310720 bytes` is the board menu, not the 1966080-byte slot.
- Private headers can be included from a sketch: PlatformIO and the IDE put all of `src/` on
  the include path.
