# Thin OTA Library

The four-verb contract (`stage → apply → confirm → rollback`) as something a maker embeds
in **their own** firmware, rather than a prebuilt agent they flash and watch. ESP-IDF
component, Arduino library, a worked example, and the project's first written CUJ.

Completed-work archive for this feature area (`docs/features/`). This holds the plan
**substance**, not links — `.claude/plans/*` are local and gitignored, so their reasoning
must be captured HERE (and decisions logged in `DECISIONS.md`). When `/implement`
finishes a task, it appends a completed entry below.

**Supports CUJs:** [`spec/cujs.md`](../../spec/cujs.md) → **CUJ-1**, "A sketch on the desk
becomes a board in the field that fixes itself" — this release *is* that journey.

---

## Completed Work

### Where a library user's config lives (R3-fw-1) — **LANDED 2026-09-22**

**What shipped.** A measurement and a decision, no firmware. The open question filed with
the release — `ff_cfg` is a flash partition the agent can rely on and an Arduino sketch
cannot — is answered **(a): a packaged partition table, shipped as a sketch-local
`partitions.csv`, under a new layout id `ab-4m-arduino-v1`.** Config stays in a flashable
`ff_cfg`; NVS is not where it lives. Written up in `spec/open-questions.md` →
*ota-library* (the section is now ANSWERED), reasoned in
[`design/decisions/arduino-gets-its-own-layout-id.md`](../../design/decisions/arduino-gets-its-own-layout-id.md),
reproducible from
[`docs/runbooks/arduino-partition-measurement.md`](../runbooks/arduino-partition-measurement.md).

**Key approach — compile it, then decode what the build actually produced.** The question
had been open since the release was written because it could only be answered by running
the toolchain, and the honest failure mode of a spike like this is a well-argued guess.
So: `arduino-cli` 1.5.2 + `esp32:esp32@3.3.12` into a scratch tree, real compiles for
`esp32`, `esp32s3` and `esp32doit-devkit-v1`, every table read back out of the built
`partitions.bin` with the core's own `gen_esp32part.py` rather than from the csv that was
its input — and `flash_args` read alongside it, which is where the decisive finding was.

**The finding that chose the answer.** A sketch-local `partitions.csv` *does* override
everything (`platform.txt` prebuild hooks: `build.partitions` < variant < sketch folder),
so shipping `ab-4m-v1` looked like pure packaging. It is not: the Arduino upload and merge
recipes hardcode `0xe000` for `boot_app0.bin` and `0x10000` for the app regardless of the
table being flashed. Under `ab-4m-v1` those land in the tail of `nvs` and in the second
half of `otadata`, with the app running on over `phy_init`, `ff_cfg` and the 64 KB
alignment gap — `ota_0` starts at `0x20000`. The build is green, the partition binary is a
correct `ab-4m-v1`, and the board never boots its sketch. Stock `min_spiffs` already has
two slots of exactly **1966080** B at the offsets the recipe writes to, so
`ab-4m-arduino-v1` is that map plus a 4 KB `ff_cfg` at `0x3D0000`: same `ota_slot_size` as
`ab-4m-v1`, different offsets, new id per `design/partitions.md` §6.

**Why not NVS.** Flash cost turned out to decide nothing — +7460 B for an NVS read vs
+324 B for a partition read, against a 1920 KB slot. Provisioning decides it: the upload
writes four offsets and nothing else, so no credential can reach NVS before first boot,
and *Erase All Flash Before Sketch Upload* is a one-click menu that wipes it along with
the device credential — after which re-enrolling needs an already-burnt single-use token.

**T2 acceptance evidence** — *"the question in `open-questions.md` is answered with
evidence, not opinion, and the answer names which of (a)/(b) the release implements."*
The section names (a) in its first line and carries six numbered measurements, each a
decoded table, an offset list or a byte count from a real build:

| Measured | Result |
|---|---|
| Stock default table, `esp32` + `esp32s3` | `app0`/`app1` **1310720** B @ `0x10000`/`0x150000`, `nvs` 20K, no `ff_cfg`; `0x12000` is inside `app0` |
| All 49 stock csvs | 29 have two equal OTA slots; `min_spiffs` and `rainmaker` are **1966080** — our number |
| Sketch-local override | wins over default menu, over explicit `PartitionScheme=min_spiffs`, and on a board with no menu; survives `esp32`→`esp32s3` |
| `ab-4m-v1` as a sketch table | builds exactly; `flash_args` writes `0xe000`/`0x10000` anyway → unbootable |
| `ab-4m-arduino-v1` candidate | compiled and decoded on three boards; `flash_args` matches the table |
| Board coverage | **52 of 409** boards expose no `PartitionScheme` menu (35 hardwired `default`), incl. `esp32doit-devkit-v1` |
| Bootloader posture | `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`, anti-rollback / Secure Boot / flash-enc off, both targets |
| Config read cost | baseline 268588 B → NVS **+7460 B**, `ff_cfg` partition **+324 B** |

Scratch toolchain (~7.8 GB installed, pruned to 2.1 GB) was deleted afterwards; the box
was left at the free space it started with. Nothing was installed into `~/.arduino15`.

**Unblocks the release.** `R3-fw-3` now knows what it ships and `R3-fw-5` knows it cannot
lean on the IDE's size guard — that line reads the board menu's `upload.maximum_size`, not
the table, and reported `Maximum is 1310720 bytes` for a build whose slots were 1966080.
One PROPOSAL left for `spec/`: `ab-4m-arduino-v1` needs adding to `device-protocol.md` and
to `firmware/manifest.py::SUPPORTED_LAYOUTS`, which is a one-entry dict today.

### The project's first written CUJ (R3-spec-1) — **LANDED 2026-09-22**

**What shipped.** [`spec/cujs.md`](../../spec/cujs.md), and with it the end of an
open question filed 2026-09-11. The file carries one journey — **CUJ-1**, Alex the solo
maker (`docs/personas/PERSONAS.md` §1) taking a working sketch on a DevKit through
library → one USB flash → the fleet list → an OTA of a changed build → a bad build that
recovers itself. Six steps, written as what Alex does rather than what the system does:
*"Later they deploy a build that is broken. The board recovers on its own, comes back on
the version that worked, and says so. Alex does not get up."*

Two standards that had acceptance criteria hanging in air now reference it.
*Unaided onboarding* points at **step 3** (the one flash that turns a board on the desk
into a board in the fleet list) — its criteria are that step's parts. *Embeddable OTA*
points at the whole journey. Both are additive `Supported By` blocks; no criterion was
reworded. The stale "There are no written CUJs" paragraph is gone from
`spec/open-questions.md`.

**Key approach — the Driver is segmented, and that is the one real design decision here.**
CUJ-1 crosses four releases (R0 enroll · R1 OTA transport · R2 auto-rollback · R3 the
library), so no single harness can play it until `R3-test-1` exists. The template's single
`Driver:` field would therefore have been dead on arrival, and fleetforge's T3 gate — which
`/replan` runs at sprint close — would have stayed red until R3 landed, blocking sprint
transitions that have nothing to do with this release. Instead each segment names its own
harness and declares whether it is gradeable:

| Steps | Segment | Harness | Gradeable |
|---|---|---|---|
| 1–2 | Sketch compiles with the library | the worked example's build (`R3-fw-3`/`R3-fw-4`) | not yet |
| 3 | One flash → on the fleet | `just agent-qemu esp32`, `just agent-qemu-smoke esp32`, `pytest tests/test_enroll.py` | yes, agent path |
| 5 | OTA a changed build | `just sim-fleet 1 --capabilities ota` + `POST /v1/devices/{id}/deploy`; on-device per `docs/runbooks/agent-qemu.md` | yes, agent path |
| 6 | A bad build recovers itself | `R3-test-1`'s QEMU rollback run | not yet (R2/R3) |

A segment with no harness is **not graded and is not a pass** — the distinction matters,
because the failure mode of a partial CUJ is quietly scoring the easy half and calling it
green.

The Judge is three-part per the template: deterministic must-pass (device online after
step 3; reported `fw_version` equals the uploaded build's after step 5; `rolled-back` and
the prior version after step 6; a duplicated command downloads once), hard-fail traps that
cap the score at 0 (any UART log read, any physical retrieval, any token/PSK/broker
credential in a log or URL, a layout-mismatched build flashed rather than refused, a
milestone shown as reached while no longer true), and an LLM rubric graded by `jeep`
against the Success Criteria. Success Criteria cite numbers already committed in
`prd.md` → *Requirements & targets* (5 min healthy deploy, 2 s dashboard reflection,
Fleet safety 100%) rather than inventing new ones.

**T1.** `just test` — ruff, `ruff format --check`, mypy and **929 tests** green.

**T2 acceptance evidence.**
* `spec/cujs.md` exists and carries the sketch/DevKit CUJ — read back in full.
* `grep -n "cujs.md" spec/standards.md` → line 23 (*Unaided onboarding*) and line 134
  (*Embeddable OTA*), both `Supported By` blocks.
* `grep -c "no written CUJs" spec/open-questions.md` → **0**; the section's other three
  paragraphs intact.
* **Every harness the Driver claims exists today actually resolves** — `just --show` for
  `agent-qemu`, `agent-qemu-smoke` and `sim-fleet` all OK; `pytest --collect-only
  tests/test_enroll.py` → **29 tests collected**. A Driver citing a recipe that does not
  exist is the specific failure this check was added to catch.
* The subjective criterion ("says what a person does") was confirmed by the user, which
  also served as the mandatory `CRITICAL.md` review for the `spec/` edits.

**Spec proposal (recorded, not applied).** CUJ-1 reaches the fleet list through the
*library*, so the flasher-page path R0 actually built is only one of its steps. A second
CUJ for the prebuilt-agent path would be fully playable today and would give T3 something
to grade before R3 lands. Filed in `spec/open-questions.md`; out of scope here because the
task scoped itself to the first CUJ.

**Gotcha for whoever writes CUJ-2.** `spec/` is `CRITICAL.md`-protected and `/implement`
does not edit it — a `spec`-category task is the sanctioned route, and it still carries the
escalation (strongest model, mandatory review before commit).

## In Progress

_Tracked in `TODO.md` (live status lives there, not here)._

## Planned Work

### Thin OTA library — Arduino, ESP-IDF component, PlatformIO (Priority: P1)

- **Problem:** A hobbyist's unit of work is a sketch, not a Fleetforge agent. Today the
  only firmware that speaks the protocol is the prebuilt agent, which connects,
  heartbeats and blinks — it does not water the plants, drive the frame, or fly the quad.
  So the product can only update a device that does nothing its owner cares about. Until
  the four verbs are a library that drops into *their* firmware, Fleetforge is a very
  good demo of itself.
- **Status:** Planned
- **Target:** R3
- **Added:** 2026-09-22
- **Source:** [`docs/HOBBYIST.md`](../HOBBYIST.md) §4.1 — ranked the #1 unlock for the
  primary persona. Named in [`prd.md`](../../spec/prd.md) → *Scope* as half the
  device-side product surface ("(b) a thin OTA library (ESP-IDF/Arduino) to embed in
  custom firmware"), with no feature file, release or task until now.

**Why R3 and not earlier.** A library is a multiplier on however safe deploy currently
is. Shipping the contract into other people's `setup()`/`loop()` before auto-rollback
exists would spread the unsafe path across custom firmware on boards nobody can reach —
the exact failure the product exists to prevent. Recorded in
[`design/decisions/ota-library-ships-after-safe-deploy.md`](../../design/decisions/ota-library-ships-after-safe-deploy.md).

**The release opens with a spike, not with extraction.** Where a library user's config
lives is genuinely undecided, and it sets the scope of everything after it. The agent
keeps broker URL, Wi-Fi credentials and the enrollment token in a dedicated `ff_cfg`
flash partition written by the browser flasher
([`design/partitions.md`](../../design/partitions.md) §3). A hobbyist who drops the
library into a sketch and flashes from the Arduino IDE has no such partition, and
overwriting a custom partition table is the classic Arduino-IDE failure. Two candidates:

- **(a) Packaged partition table + board definition**, made mandatory. Mostly packaging,
  but the library then only works for users who adopt `ab-4m-v1` exactly, and a flash-time
  immutable set wrong is unrecoverable.
- **(b) NVS-backed config path**, so the library runs on a stock Arduino partition scheme.
  Works for far more users, but is a real change to `ff_cfg` and the enrollment flow, and
  touches a `CRITICAL.md` path.

**Answered 2026-09-22 by `R3-fw-1`: (a), with a correction.** The packaged table cannot be
`ab-4m-v1` — the Arduino upload recipe hardcodes offsets that layout does not use — so the
library ships `ab-4m-arduino-v1`, a second layout id with the same `ota_slot_size`, as a
sketch-local `partitions.csv`. Config stays in a flashable `ff_cfg`. See the Completed Work
entry above and
[`design/decisions/arduino-gets-its-own-layout-id.md`](../../design/decisions/arduino-gets-its-own-layout-id.md).

**What the release contains**

1. **ESP-IDF component.** The agent already separates the protocol from the demo app:
   `ff_ota`, `ff_mqtt`, `ff_enroll`, `ff_cfg`, `ff_store`, `ff_identity`, `ff_net`,
   `ff_time` are eight modules with headers under `agent/main/`. Moving them to a
   component with an `idf_component.yml` is largely mechanical; the design work is
   deciding what the *public* surface is, because whatever ships becomes a contract that
   is additive-only from then on.
2. **Arduino library** wrapping the same C. This is where the persona actually lives
   ([`docs/personas/PERSONAS.md`](../personas/PERSONAS.md) §1 — "writes firmware in
   Arduino IDE or PlatformIO, does not use ESP-IDF directly"). If adopting Fleetforge
   means migrating a project to ESP-IDF, it will not be adopted.
3. **A worked example**, small enough to read in one screen: enroll → heartbeat → handle
   `stage` → report the version that booted. The PRD's Morse-code blinker is the
   documented sample, so the example and the marketing claim are the same artifact.
4. **The first CUJ.** *Unaided onboarding* in [`spec/standards.md`](../../spec/standards.md)
   has acceptance criteria hanging in air because `spec/cujs.md` does not exist. "I have a
   sketch and a DevKit on the desk" is the journey this release is about, and writing it
   gives the standard something to hang from.

**Secondary benefit.** Extraction forces the protocol to stay library-shaped — small,
additive, no implicit dashboard coupling — which is what the device-facing thin waist
claimed to be but has never been tested against a second consumer.

**Depends on board profiles.** R3 is the release that produces the second and third real
partition layouts, and the first user who brings a `partitions.csv` we have never seen. A
code-resident `SUPPORTED_LAYOUTS` cannot serve that, so step 2 of
[board-profiles.md](board-profiles.md) — the profile table with user-defined entries — is
gated on this release and should be scheduled with it.

**Out of scope for R3:** Improv reprovisioning (HOBBYIST §4.3, unslotted), PlatformIO
registry publication beyond a working `platformio.ini` recipe, and any application-config
channel — `dn/cfg` stays agent-only by design.
