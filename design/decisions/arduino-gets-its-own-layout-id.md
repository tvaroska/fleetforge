# The Arduino library gets its own layout id, not `ab-4m-v1`

**Date:** 2026-09-22 · **Area:** ota-library · **Status:** decided. Applied to `spec/device-protocol.md` 2026-10-02.
Spike: `R3-fw-1`. Evidence and reproduction:
[../../docs/runbooks/arduino-partition-measurement.md](../../docs/runbooks/arduino-partition-measurement.md)
and *Appendix — the measurements* below (moved from `spec/open-questions.md` on 2026-10-02, when the layout applied to `spec/device-protocol.md`).
The layout this supplements: [../partitions.md](../partitions.md).

## Context

`ff_cfg` (broker URL, API origin, Wi-Fi credentials, enrollment token) is a 4 KB flash
partition at `0x12000` that the browser flasher writes per board. That design works
because the agent owns its whole flash layout. CUJ-1's persona does not: Alex compiles in
the Arduino IDE and gets whatever table their board definition ships.

OTA cannot add a partition, so whatever the library requires at first flash is a
flash-time immutable, and `CRITICAL.md` prices getting one wrong at a physical recall of
the fleet. `spec/open-questions.md` framed this as **(a)** package a table and require
it versus **(b)** move config to NVS, and said outright that choosing needed a measurement
nobody made. `R3-fw-1` made it, against `esp32:esp32@3.3.12`.

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
different layouts and the server supports both. This is what `partitions.md` §6 has
always said a new layout means.

## Why not `ab-4m-v1` itself

This is the finding that decided it. It is not visible without compiling.

The Arduino upload and merge recipes hardcode four offsets — bootloader, `0x8000` table,
`0xe000` `boot_app0.bin`, `0x10000` app — **independently of the target flash table**.
A sketch-local `ab-4m-v1` builds cleanly and produces an exact, correct `ab-4m-v1`
partition binary. The upload then writes `boot_app0` across the tail of `nvs`
(`0x9000`–`0xF000`, so `0xe000` is inside it) and into the head of `otadata`, and writes
the app at `0x10000` — the second half of `otadata` (`0xF000`–`0x11000`), from where it
runs on over `phy_init`, `ff_cfg` and the 64 KB alignment gap. This is because `ota_0` does not
start until `0x20000`. Green build, board that never boots its sketch, no OTA path to fix
it. `ab-4m-v1`'s offsets are simply not reachable from the Arduino toolchain.

`ab-4m-arduino-v1` is the stock `min_spiffs` map (which already carries two 1966080 B
slots at exactly the offsets the recipe writes to) plus a 4 KB `ff_cfg` taken from the
SPIFFS region. We compiled and decoded it on `esp32`, `esp32s3` and a menu-less board.

## Why not (b), NVS

Not for the reason that looked likely. Flash cost is a rounding error: measured against a
268588 B baseline, an NVS read costs +7460 B and a partition read +324 B, against a
1920 KB slot.

**Provisioning is what kills it**. The upload writes those four offsets and nothing else.
Thus, nothing can place credentials into NVS before first boot — (b) would require a serial
handshake or Improv. This is new product surface, not a different place to keep a blob.
And *Erase All Flash Before Sketch Upload* is a one-click IDE menu that passes `-e`: it
wipes NVS, taking the device credential with it, and re-enrolling needs a token that was
single-use. (b) trades a partition the flasher can write for a credential store the user
can destroy by accident from a menu.

## Why a sketch-local csv rather than a board definition

`platform.txt`'s prebuild hooks resolve the table in priority order `build.partitions` <
variant < `{build.source.path}`, source last and winning. Measured beating the default
menu, an explicitly selected `min_spiffs`, and a board's own variant table.

It is also the only mechanism that covers the whole board list: **52 of 409 boards expose
no `PartitionScheme` menu at all** (35 of them hardwired to `default`) including
`esp32doit-devkit-v1`. Telling a maker to pick a menu entry is advice that does not exist
on their board. A file in the sketch folder works everywhere, and travels with the sketch
across a board change.

Publishing a Fleetforge *board definition* triggered a rejection as the primary route for the same
reason in reverse: it asks the user to install a third-party core URL and then select a
board that is not their board, before they have any reason to trust the project.

## Consequences

- **PROPOSED, not applied** (`spec/` has protection during `/implement`).
  `ab-4m-arduino-v1` must be added to `spec/device-protocol.md` and to
  `firmware/manifest.py::SUPPORTED_LAYOUTS` with `ota_slot_size` 1966080.
  `SUPPORTED_LAYOUTS` is currently a one-entry dict built from two constants. A second
  layout makes it a real table, and `tests/test_agent_partitions.py` guards the first
  entry only.
- **`R3-fw-5` cannot use the IDE's size guard**. `Sketch uses … Maximum is N` reads the
  board menu's `upload.maximum_size`, not the built table: a build with 1966080 B slots
  reported `Maximum is 1310720 bytes`. The layout check has to come from the firmware
  announcing what it actually carries.
- **`R3-fw-3`/`R3-fw-4`: the table ships with the example, not the library**. The hook
  reads the sketch folder. Thus, a `partitions.csv` inside a library never takes effect. The
  quickstart's first step is a file copy, and the override happens silently. The IDE gives no sign that a sketch-local table replaced the board's.
- **The safety posture needs no custom bootloader**. The core's prebuilt bootloader builds `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y` with anti-rollback, Secure Boot and
  flash encryption all off (exactly `partitions.md` §2) on both `esp32` and `esp32s3`.
  `esp_https_ota.h`, `esp_ota_ops.h`, `nvs.h` and `mqtt_client.h` all ship in the core, so
  the component from `R3-fw-2` compiles in a sketch unmodified.
- **Re-run the measurement on every core bump**. Every finding above is a property of
  `esp32:esp32@3.3.12`'s `platform.txt` and `boards.txt`. The hardcoded `0x10000` is a
  convention, not a contract. It is the one this layout centers on.

## Appendix — the measurements

Moved verbatim from `spec/open-questions.md` → *ota-library* (answered 2026-09-22). The
PROPOSED bullet below applied on 2026-10-02.

**Answer: (a) — a packaged partition table, shipped as a sketch-local `partitions.csv`.
Not `ab-4m-v1`. A second layout id whose offsets obey the Arduino upload recipe**.
Config stays in a flashable `ff_cfg` partition. NVS is not where it lives.

Measured by `R3-fw-1` against `esp32:esp32@3.3.12` (`arduino-cli` 1.5.2, real compiles for
`esp32` and `esp32s3`, tables decoded from the built `partitions.bin`). Reproduce with
[`docs/runbooks/arduino-partition-measurement.md`](../../docs/runbooks/arduino-partition-measurement.md).
Full reasoning: [`design/decisions/arduino-gets-its-own-layout-id.md`](arduino-gets-its-own-layout-id.md).

**What measured**.

1. **A stock build is not compatible, and not close**. Default scheme on both `esp32` and
   `esp32s3`: `app0`/`app1` **1310720** B at `0x10000`/`0x150000`, `nvs` 20K, no `ff_cfg`.
   `0x12000` (where `ff_cfg` lives on `ab-4m-v1`) is *inside* `app0`.
2. **A sketch-local `partitions.csv` does override everything. It is the only
   mechanism that works on every board**. `platform.txt` prebuild hooks 1–3 copy in
   priority order `build.partitions` < variant < `{build.source.path}`, source last and
   winning. Observed winning over the default menu, over an explicitly selected
   `min_spiffs` menu value, and on `esp32doit-devkit-v1` — one of **52 of 409 boards that
   have no `PartitionScheme` menu at all** (35 of those hardwired to `default`), where the
   menu route does not exist. The same file survived a board change (`esp32` → `esp32s3`,
   same sketch folder, bootloader offset re-resolved `0x1000` → `0x0`).
3. **`ab-4m-v1` itself cannot be that file**. The upload and merge recipes hardcode four
   offsets — bootloader, `0x8000` table, **`0xe000` `boot_app0.bin`**, **`0x10000` app** —
   independently of the target flash table. Under `ab-4m-v1` `0xe000` is the tail of
   `nvs` (`0x9000`–`0xF000`) and `0x10000` is the second half of `otadata`
   (`0xF000`–`0x11000`). The app then runs on over `phy_init`, `ff_cfg` and the 64 KB
   alignment gap. This is because `ota_0` does not start until `0x20000`. The
   build **succeeds and emits an exact `ab-4m-v1` table**, then writes the app to an
   offset no partition claims. That board never boots its sketch. No OTA can fix it.
4. **A compatible layout exists, compiles, and keeps the same `ota_slot_size`**. Stock
   `min_spiffs.csv` already carries two OTA slots of exactly **1966080** B (the number
   `device-protocol.md` fixes) at `0x10000`/`0x1F0000` with `otadata` at `0xe000`. This is precisely what the upload recipe expects. Adding a 4 KB `ff_cfg` at `0x3D0000`
   (inside what `min_spiffs` gives to SPIFFS) restores the per-board config partition.
   Compiled and decoded on `esp32`, `esp32s3` and a menu-less board.
5. **The safety posture survives on stock Arduino**. The core's prebuilt bootloader builds with `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`, anti-rollback unset, Secure Boot
   and flash encryption off — the exact posture `design/partitions.md` §2 requires, on both
   `esp32` and `esp32s3`. `esp_https_ota.h`, `esp_ota_ops.h`, `nvs.h` and `mqtt_client.h`
   all ship in the core's includes. Thus, the agent's C compiles in a sketch unmodified.
6. **(b) is cheap in flash and expensive everywhere else**. Measured against a 268588 B
   baseline sketch: an NVS config read costs **+7460 B** flash, a `ff_cfg` partition read
   **+324 B**. Neither matters against a 1920 KB slot, so flash cost decides nothing.
   Provisioning does: the upload writes only those four offsets, so **nothing can put
   credentials into NVS before first boot** — (b) needs a serial handshake or Improv. This is new surface rather than a config location. Worse, *Erase All Flash Before Sketch
   Upload* is a one-click IDE menu (`-e`) that wipes NVS with the device credential in it,
   and re-enrolling needs a token that was single-use.

**Two consequences that are not optional.**

- **PROPOSED (not applied — `spec/` has protection during `/implement`):** the Arduino layout
  is a **new layout id**, `ab-4m-arduino-v1`, added to `spec/device-protocol.md` and to
  `firmware/manifest.py::SUPPORTED_LAYOUTS` with `ota_slot_size` **1966080** — the same
  number as `ab-4m-v1`, a different map. Per `design/partitions.md` §6 a new layout is a new
  id, never an edit to an existing row. Two boards in one fleet will carry different maps
  and the server must support both.
- **The IDE's size guard reads the menu, not the table**. A build carrying a sketch-local
  table with 1966080 B slots still reported *"Maximum is 1310720 bytes"*. It under-reports
  here (it refuses sketches that would have fit). But it can over-report whenever the
  selected menu scheme is roomier than the shipped csv. `R3-fw-5` cannot use it as the
  layout check.

Filed 2026-09-22 (R3), answered 2026-09-22 (`R3-fw-1`).
