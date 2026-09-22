# The Arduino library gets its own layout id, not `ab-4m-v1`

**Date:** 2026-09-22 · **Area:** ota-library · **Status:** decided, unimplemented.
Spike: `R3-fw-1`. Evidence and reproduction:
[../../docs/runbooks/arduino-partition-measurement.md](../../docs/runbooks/arduino-partition-measurement.md)
and [../../spec/open-questions.md](../../spec/open-questions.md) → *ota-library*.
The layout this supplements: [../partitions.md](../partitions.md).

## Context

`ff_cfg` — broker URL, API origin, Wi-Fi credentials, enrollment token — is a 4 KB flash
partition at `0x12000` that the browser flasher writes per board. That design works
because the agent owns its whole flash layout. CUJ-1's persona does not: Alex compiles in
the Arduino IDE and gets whatever table their board definition ships.

A partition cannot be added by OTA, so whatever the library requires at first flash is a
flash-time immutable, and `CRITICAL.md` prices getting one wrong at a physical recall of
the fleet. `spec/open-questions.md` had framed this as **(a)** package a table and require
it versus **(b)** move config to NVS, and said outright that choosing needed a measurement
nobody had made. `R3-fw-1` made it, against `esp32:esp32@3.3.12`.

## Decision

**(a) — a packaged partition table, delivered as a sketch-local `partitions.csv`,
under a new layout id `ab-4m-arduino-v1`. Config stays in a flashable `ff_cfg`.**

```
nvs        data  nvs       0x9000    0x5000
otadata    data  ota       0xe000    0x2000
ota_0      app   ota_0     0x10000   0x1E0000   <- 1966080, as ab-4m-v1
ota_1      app   ota_1     0x1F0000  0x1E0000
ff_cfg     data  0x40      0x3D0000  0x1000
coredump   data  coredump  0x3F0000  0x10000
```

Same `ota_slot_size` as `ab-4m-v1`, **different map**. Two boards in one fleet will carry
different layouts and the server supports both — which is what `partitions.md` §6 has
always said a new layout means.

## Why not `ab-4m-v1` itself

This is the finding that decided it, and it is not visible without compiling.

The Arduino upload and merge recipes hardcode four offsets — bootloader, `0x8000` table,
`0xe000` `boot_app0.bin`, `0x10000` app — **independently of the table being flashed**.
A sketch-local `ab-4m-v1` builds cleanly and produces an exact, correct `ab-4m-v1`
partition binary; the upload then writes `boot_app0` across the tail of `nvs`
(`0x9000`–`0xF000`, so `0xe000` is inside it) and into the head of `otadata`, and writes
the app at `0x10000` — the second half of `otadata` (`0xF000`–`0x11000`), from where it
runs on over `phy_init`, `ff_cfg` and the 64 KB alignment gap, because `ota_0` does not
start until `0x20000`. Green build, board that never boots its sketch, no OTA path to fix
it. `ab-4m-v1`'s offsets are simply not reachable from the Arduino toolchain.

`ab-4m-arduino-v1` is the stock `min_spiffs` map — which already carries two 1966080 B
slots at exactly the offsets the recipe writes to — plus a 4 KB `ff_cfg` taken from the
SPIFFS region. It was compiled and decoded on `esp32`, `esp32s3` and a menu-less board.

## Why not (b), NVS

Not for the reason that looked likely. Flash cost is a rounding error: measured against a
268588 B baseline, an NVS read costs +7460 B and a partition read +324 B, against a
1920 KB slot.

**Provisioning is what kills it.** The upload writes those four offsets and nothing else,
so nothing can place credentials into NVS before first boot — (b) would require a serial
handshake or Improv, which is new product surface, not a different place to keep a blob.
And *Erase All Flash Before Sketch Upload* is a one-click IDE menu that passes `-e`: it
wipes NVS, taking the device credential with it, and re-enrolling needs a token that was
single-use. (b) trades a partition the flasher can write for a credential store the user
can destroy by accident from a menu.

## Why a sketch-local csv rather than a board definition

`platform.txt`'s prebuild hooks resolve the table in priority order `build.partitions` <
variant < `{build.source.path}`, source last and winning. Measured beating the default
menu, an explicitly selected `min_spiffs`, and a board's own variant table.

It is also the only mechanism that covers the whole board list: **52 of 409 boards expose
no `PartitionScheme` menu at all** — 35 of them hardwired to `default` — including
`esp32doit-devkit-v1`. Telling a maker to pick a menu entry is advice that does not exist
on their board. A file in the sketch folder works everywhere, and travels with the sketch
across a board change.

Publishing a Fleetforge *board definition* was rejected as the primary route for the same
reason in reverse: it asks the user to install a third-party core URL and then select a
board that is not their board, before they have any reason to trust the project.

## Consequences

- **PROPOSED, not applied** (`spec/` is protected during `/implement`):
  `ab-4m-arduino-v1` must be added to `spec/device-protocol.md` and to
  `firmware/manifest.py::SUPPORTED_LAYOUTS` with `ota_slot_size` 1966080.
  `SUPPORTED_LAYOUTS` is currently a one-entry dict built from two constants; a second
  layout makes it a real table, and `tests/test_agent_partitions.py` guards the first
  entry only.
- **`R3-fw-5` cannot use the IDE's size guard.** `Sketch uses … Maximum is N` reads the
  board menu's `upload.maximum_size`, not the built table: a build with 1966080 B slots
  reported `Maximum is 1310720 bytes`. The layout check has to come from the firmware
  announcing what it actually carries.
- **`R3-fw-3`/`R3-fw-4`: the table ships with the example, not the library.** The hook
  reads the sketch folder, so a `partitions.csv` inside a library is never consulted. The
  quickstart's first step is a file copy, and the override happens silently — the IDE
  gives no sign that a sketch-local table replaced the board's.
- **The safety posture needs no custom bootloader.** The core's prebuilt bootloader is
  built `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y` with anti-rollback, Secure Boot and
  flash encryption all off — exactly `partitions.md` §2 — on both `esp32` and `esp32s3`.
  `esp_https_ota.h`, `esp_ota_ops.h`, `nvs.h` and `mqtt_client.h` all ship in the core, so
  the component from `R3-fw-2` compiles in a sketch unmodified.
- **Re-run the measurement on every core bump.** Every finding above is a property of
  `esp32:esp32@3.3.12`'s `platform.txt` and `boards.txt`. The hardcoded `0x10000` is a
  convention, not a contract, and it is the one this layout is built around.
