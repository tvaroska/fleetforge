# Fleetforge — Open Questions

Material things that are undecided. Recorded rather than guessed. Answering one means
moving it into `spec/` proper and deleting it here.

---

## enrollment — unaided onboarding

**How much of the diagnosis belongs to firmware rather than the panel?** The panel
classifies text the board happens to print, which is a parser chasing log strings — it
broke on 2026-09-11 precisely because `E BOD:` does not match the ESP-IDF log format.
An alternative is for the agent to report structured faults it already knows about
(`esp_reset_reason()` at boot is one line of C, and would make "this board is in a
reset loop" a fact rather than an inference). Not resolved because it trades a fragile
parser for a firmware round-trip on every new fault, and firmware updates are the thing
that is hardest to ship to a board that will not come online.

**What is the diagnostic bundle's format, and is it ever transmitted?** Copy-to-
clipboard as text is assumed for now. A structured format that the server could accept
would make fleet-wide onboarding failure rates measurable, but it turns a local
debugging aid into an API and a data-retention question. Deliberately deferred.

**Does the repeat path deserve its own design?** Onboarding board #2..#N is currently
identical to board #1 — re-enter Wi-Fi, re-detect chip, re-flash. Remembered profiles
and batch flashing are listed under Post-v1, but if the first fleet is realistically
more than a handful of boards, that is a v1 concern rather than a later one.

**Proposed: a second CUJ for the prebuilt-agent path.** `cujs.md` → *CUJ-1* is the
maker's journey and reaches the fleet list through the **library**, so the flasher-page
path that R0 actually built is only covered as one of its steps. The agent path is a
journey in its own right — "I want to see this thing work before I commit to it" — and
unlike CUJ-1 it is fully playable today, which would give T3 something to grade before
R3 lands. Not written here because `R3-spec-1` scoped itself to the first CUJ.
Filed 2026-09-22 (R3-spec-1).

---

## ota-deploy — one artifact size limit, written twice

**`prd.md`'s "Artifact size ≤ 1.9 MB" is the rounded form of `ota_slot_size`, and the
two should not read as independent caps.** `spec/device-protocol.md` fixes
`ota_slot_size` at **1966080** bytes, which is 1.875 MiB — "1.9 MB" to two significant
figures. They are one number. Read as two, they invite an implementation with two
thresholds a few kilobytes apart and a rejection nobody can explain from the message.

R1-be-1 implements the authoritative one only: `firmware/manifest.py::SUPPORTED_LAYOUTS`,
because that is the mapping tied to the partition table a board actually carries, and it
is already what agent-bundle validation uses — so an upload and a bundle cannot disagree
about how big a slot is.

Proposed: `prd.md` → *Requirements & targets* should either say "≤ the target layout's
`ota_slot_size` (1966080 B for `ab-4m-v1`)" or drop the line and cite
`device-protocol.md`. Not applied here — `spec/` is protected during `/implement`.
Filed 2026-09-16 (R1-be-1).

---

## ~~ota-library — where does a library user's config live?~~ ANSWERED 2026-09-22

**Answer: (a) — a packaged partition table, shipped as a sketch-local `partitions.csv`.
Not `ab-4m-v1`; a second layout id whose offsets obey the Arduino upload recipe.**
Config stays in a flashable `ff_cfg` partition; NVS is not where it lives.

Measured by `R3-fw-1` against `esp32:esp32@3.3.12` (`arduino-cli` 1.5.2, real compiles for
`esp32` and `esp32s3`, tables decoded from the built `partitions.bin`). Reproduce with
[`docs/runbooks/arduino-partition-measurement.md`](../docs/runbooks/arduino-partition-measurement.md).
Full reasoning: [`design/decisions/arduino-gets-its-own-layout-id.md`](../design/decisions/arduino-gets-its-own-layout-id.md).

**What was measured.**

1. **A stock build is not compatible, and not close.** Default scheme on both `esp32` and
   `esp32s3`: `app0`/`app1` **1310720** B at `0x10000`/`0x150000`, `nvs` 20K, no `ff_cfg`.
   `0x12000` — where `ff_cfg` lives on `ab-4m-v1` — is *inside* `app0`.
2. **A sketch-local `partitions.csv` does override everything, and it is the only
   mechanism that works on every board.** `platform.txt` prebuild hooks 1–3 copy in
   priority order `build.partitions` < variant < `{build.source.path}`, source last and
   winning. Observed winning over the default menu, over an explicitly selected
   `min_spiffs` menu value, and on `esp32doit-devkit-v1` — one of **52 of 409 boards that
   have no `PartitionScheme` menu at all** (35 of those hardwired to `default`), where the
   menu route does not exist. The same file survived a board change (`esp32` → `esp32s3`,
   same sketch folder, bootloader offset re-resolved `0x1000` → `0x0`).
3. **`ab-4m-v1` itself cannot be that file.** The upload and merge recipes hardcode four
   offsets — bootloader, `0x8000` table, **`0xe000` `boot_app0.bin`**, **`0x10000` app** —
   independently of the table being flashed. Under `ab-4m-v1` `0xe000` is the tail of
   `nvs` (`0x9000`–`0xF000`) and `0x10000` is the second half of `otadata`
   (`0xF000`–`0x11000`) — the app then runs on over `phy_init`, `ff_cfg` and the 64 KB
   alignment gap, because `ota_0` does not start until `0x20000`. The
   build **succeeds and emits an exact `ab-4m-v1` table**, then writes the app to an
   offset no partition claims. That board never boots its sketch, and no OTA can fix it.
4. **A compatible layout exists, compiles, and keeps the same `ota_slot_size`.** Stock
   `min_spiffs.csv` already carries two OTA slots of exactly **1966080** B — the number
   `device-protocol.md` fixes — at `0x10000`/`0x1F0000` with `otadata` at `0xe000`, which
   is precisely what the upload recipe expects. Adding a 4 KB `ff_cfg` at `0x3D0000`
   (inside what `min_spiffs` gives to SPIFFS) restores the per-board config partition.
   Compiled and decoded on `esp32`, `esp32s3` and a menu-less board.
5. **The safety posture survives on stock Arduino.** The core's prebuilt bootloader is
   built with `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`, anti-rollback unset, Secure Boot
   and flash encryption off — the exact posture `design/partitions.md` §2 requires, on both
   `esp32` and `esp32s3`. `esp_https_ota.h`, `esp_ota_ops.h`, `nvs.h` and `mqtt_client.h`
   all ship in the core's includes, so the agent's C compiles in a sketch unmodified.
6. **(b) is cheap in flash and expensive everywhere else.** Measured against a 268588 B
   baseline sketch: an NVS config read costs **+7460 B** flash, a `ff_cfg` partition read
   **+324 B**. Neither matters against a 1920 KB slot, so flash cost decides nothing.
   Provisioning does: the upload writes only those four offsets, so **nothing can put
   credentials into NVS before first boot** — (b) needs a serial handshake or Improv, which
   is new surface rather than a config location. Worse, *Erase All Flash Before Sketch
   Upload* is a one-click IDE menu (`-e`) that wipes NVS with the device credential in it,
   and re-enrolling needs a token that was single-use.

**Two consequences that are not optional.**

- **PROPOSED (not applied — `spec/` is protected during `/implement`):** the Arduino layout
  is a **new layout id**, `ab-4m-arduino-v1`, added to `spec/device-protocol.md` and to
  `firmware/manifest.py::SUPPORTED_LAYOUTS` with `ota_slot_size` **1966080** — the same
  number as `ab-4m-v1`, a different map. Per `design/partitions.md` §6 a new layout is a new
  id, never an edit to an existing row; two boards in one fleet will carry different maps
  and the server must support both.
- **The IDE's size guard reads the menu, not the table.** A build carrying a sketch-local
  table with 1966080 B slots still reported *"Maximum is 1310720 bytes"*. It under-reports
  here (it refuses sketches that would have fit), but it can over-report whenever the
  selected menu scheme is roomier than the shipped csv. `R3-fw-5` cannot use it as the
  layout check.

Filed 2026-09-22 (R3), answered 2026-09-22 (`R3-fw-1`).
