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
  `ff_progress.h`, `ff_store.h`, `ff_time.h`. This is the ESP-IDF surface.
- `src/Fleetforge.h`: the Arduino surface. It lives in `src/` because the Arduino build
  reads `src/`.

The public surface is additive-only from 0.5.0 on.

## Not yet

- Not on the PlatformIO registry, the Arduino Library Manager or the ESP-IDF component
  registry.
- The Arduino IDE's `Maximum is 1310720 bytes` is the board menu, not the 1966080-byte slot.
- Private headers can be included from a sketch: PlatformIO and the IDE put all of `src/` on
  the include path.
