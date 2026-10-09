# Thin OTA Library

The four-verb contract (`stage → apply → confirm → rollback`) as something a maker embeds
in **their own** firmware, rather than a prebuilt agent they flash and watch. ESP-IDF
component, Arduino library, a worked example, and the project's first written CUJ.

Completed-work archive for this feature area (`docs/features/`). This holds the plan
**substance**, not links. `.claude/plans/*` are local and gitignored, so their reasoning
must live HERE (and decisions logged in `DECISIONS.md`). When `/implement`
finishes a task, it appends a completed entry below.

**Supports CUJs:** [`spec/cujs.md`](../../spec/cujs.md) → **CUJ-1**, "A sketch on the desk
becomes a board in the field that fixes itself" — this release *is* that journey.

---

## Completed Work

### R3-spec-3 (2026-10-08): library marker encoding decided; spec not applied

Decided: the library marker is a 64-byte `const` struct in the library's shared C core
(magic `14a948d18f12cfdd46464f54414c4942`, then `format`, then `lib_version`), found by
scanning the whole uploaded image, not a field of `esp_app_desc_t`, not
`.rodata_custom_desc`, not a linker section and not an ELF note. It is kept only by an
`extern` read from library code, so `--gc-sections` drops it from a sketch that has the
library installed but never calls it; it is never force-kept. Boards announce the running
image's marker as `lib_marker` (additive, `proto` 1, `null` never warns); the pre-check
gates on the image being sent, as the gating warning `no_library_marker` (`R3-be-1`). The
paste-ready text is in *Planned Work → Library marker proposal*. Application order: Patch A
((b) new `## Library marker` section, (c) the `lib_marker` prose, (d) the `flows.md` line
144 deletion, (e) the `open-questions.md` candidate (1) rewrite) any time after the owner
accepts; Patch B ((a), `"lib_marker": 1` in the announce example) only in `R3-fw-6`'s
commit. Blocked on the owner: `R3-fw-6`, `R3-be-1`. Side finding for `R3-fw-3`: an Arduino
build's `esp_app_desc_t.version` is the core's IDF string, so its `fw_version` is wrong.
Nothing is built and `spec/` is untouched.

**T2 evidence.** Reference reader (the code in the proposal) as a scratch script,
`python3 -I scan.py --selftest`: exit 0 (a struct at an odd offset inside a copy of
`agent/dist/esp32/app.bin` is found with the right `lib_version`; truncated, `format = 0`,
empty, non-printable and unterminated `lib_version` are all "no marker"; an invalid first
occurrence before a valid one finds the second). Then the scan:

| Binary | Result |
|---|---|
| `agent/dist/{esp32,esp32c3,esp32c6,esp32s3}/app.bin` (0.4.7, `project_name='fleetforge-agent'`) | `no marker` ×4 |
| `tests/fixtures/firmware/esp32{,s3}.app.head.bin` (0.4.5) | `no marker` ×2 |
| `tests/fixtures/firmware/esp32{,s3}.merged.head.bin` | `no marker` ×2 |
| Arduino core 2.0.17, `call` (`.pio/build/call/firmware.bin`) | `marker format=1 lib_version=0.0.0-spike @0x148` |
| Arduino core 2.0.17, `nocall` | `no marker` |
| ESP-IDF 6.0.1 component, `call` | `marker format=1 lib_version=0.0.0-spike @0x5a74` |
| ESP-IDF 6.0.1 component, `nocall` | `no marker` |

PlatformIO 6.1.19, `espressif32` 7.0.1, `esp32dev`. The Arduino builds' descriptor:
`version='esp-idf: v4.4.7 38eeba213a'`, `project_name='arduino-lib-builder'`. The first
Arduino build installed `framework-arduinoespressif32` (769 MB) and `toolchain-xtensa-esp32`
(396 MB) under `~/.platformio/packages`; both are kept for `R3-fw-3`/`R3-fw-7`. The scratch
projects were deleted. The patches were dry-run on a copy of the repo: Patch A applies at
its anchors and `test_ff_cfg.py`, `test_agent_partitions.py`,
`test_agent_board_measurements.py`, `test_agent_known_networks.py` and
`test_deploy_precheck.py` stay green; Patch A + B turns exactly
`TestAnnounceMatchesTheSpec::test_the_firmware_builds_exactly_the_spec_keys` red, which is
why B waits for `R3-fw-6`.

### Where a library user's config lives (R3-fw-1) — **LANDED 2026-09-22**

**What shipped**. A measurement and a decision, no firmware. The open question filed with
the release (`ff_cfg` is a flash partition the agent can rely on and an Arduino sketch
cannot) is answered **(a): a packaged partition table, shipped as a sketch-local
`partitions.csv`, under a new layout id `ab-4m-arduino-v1`**. Config stays in a flashable
`ff_cfg`. NVS is not where it lives. Measured and reasoned in
[`design/decisions/arduino-gets-its-own-layout-id.md`](../../design/decisions/arduino-gets-its-own-layout-id.md),
reproducible from
[`docs/runbooks/arduino-partition-measurement.md`](../runbooks/arduino-partition-measurement.md).

**Key approach — compile it, then decode what the build actually produced**. The question
was open since the release was written. This is because it could only be answered by running
the toolchain, and the honest failure mode of a spike like this is a well-argued guess.
So: `arduino-cli` 1.5.2 + `esp32:esp32@3.3.12` into a scratch tree, real compiles for
`esp32`, `esp32s3` and `esp32doit-devkit-v1`, every table read back out of the built
`partitions.bin` with the core's own `gen_esp32part.py` rather than from the csv that was
its input — and `flash_args` read alongside it. This is where the decisive finding was.

**The finding that chose the answer**. A sketch-local `partitions.csv` *does* override
everything (`platform.txt` prebuild hooks: `build.partitions` < variant < sketch folder),
so shipping `ab-4m-v1` looked like pure packaging. It is not: the Arduino upload and merge
recipes hardcode `0xe000` for `boot_app0.bin` and `0x10000` for the app regardless of the target flash table. Under `ab-4m-v1` those land in the tail of `nvs` and in the second
half of `otadata`, with the app running on over `phy_init`, `ff_cfg` and the 64 KB
alignment gap — `ota_0` starts at `0x20000`. The build is green, the partition binary is a
correct `ab-4m-v1`. The board never boots its sketch. Stock `min_spiffs` already has
two slots of exactly **1966080** B at the offsets the recipe writes to, so
`ab-4m-arduino-v1` is that map plus a 4 KB `ff_cfg` at `0x3D0000`: same `ota_slot_size` as
`ab-4m-v1`, different offsets, new id per `design/partitions.md` §6.

**Why not NVS**. Flash cost turned out to decide nothing — +7460 B for an NVS read versus
+324 B for a partition read, against a 1920 KB slot. Provisioning decides it: the upload
writes four offsets and nothing else. Thus, no credential can reach NVS before first boot,
and *Erase All Flash Before Sketch Upload* is a one-click menu that wipes it along with
the device credential — after which re-enrolling needs an already-burnt single-use token.

**T2 acceptance evidence** — *"the question in `open-questions.md` is answered with
evidence, not opinion, and the answer names which of (a)/(b) the release implements."*
The section names (a) in its first line and carries six numbered measurements, each a
decoded table, an offset list or a byte count from a real build:

| Measured | Result |
|---|---|
| Stock default table, `esp32` + `esp32s3` | `app0`/`app1` **1310720** B @ `0x10000`/`0x150000`, `nvs` 20K, no `ff_cfg`. `0x12000` is inside `app0` |
| All 49 stock csvs | 29 have two equal OTA slots. `min_spiffs` and `rainmaker` are **1966080** — our number |
| Sketch-local override | wins over default menu, over explicit `PartitionScheme=min_spiffs`, and on a board with no menu. Survives `esp32`→`esp32s3` |
| `ab-4m-v1` as a sketch table | builds exactly. `flash_args` writes `0xe000`/`0x10000` anyway → unbootable |
| `ab-4m-arduino-v1` candidate | compiled and decoded on three boards. `flash_args` matches the table |
| Board coverage | **52 of 409** boards expose no `PartitionScheme` menu (35 hardwired `default`), incl. `esp32doit-devkit-v1` |
| Bootloader posture | `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`, anti-rollback / Secure Boot / flash-enc off, both targets |
| Config read cost | baseline 268588 B → NVS **+7460 B**, `ff_cfg` partition **+324 B** |

Scratch toolchain (~7.8 GB installed, pruned to 2.1 GB) disappeared afterwards. The box
was left at the free space it started with. Nothing was installed into `~/.arduino15`.

**Unblocks the release**. `R3-fw-3` now knows what it ships and `R3-fw-5` knows it cannot
lean on the IDE's size guard. That line reads the board menu's `upload.maximum_size`, not
the table, and reported `Maximum is 1310720 bytes` for a build whose slots were 1966080.
One PROPOSAL left for `spec/`. `ab-4m-arduino-v1` needs adding to `device-protocol.md` and
to `firmware/manifest.py::SUPPORTED_LAYOUTS`. This is a one-entry dict today. *(Applied 2026-10-02: both are in.)*

### The project's first written CUJ (R3-spec-1) — **LANDED 2026-09-22**

**What shipped**. [`spec/cujs.md`](../../spec/cujs.md), and with it the end of an
open question filed 2026-09-11. The file carries one journey — **CUJ-1**, Alex the solo
maker (`docs/personas/PERSONAS.md` §1) taking a working sketch on a DevKit through
library → one USB flash → the fleet list → an OTA of a changed build → a bad build that
recovers itself. Six steps, written as what Alex does rather than what the system does:
*"Later they deploy a build that is broken. The board recovers on its own, comes back on
the version that worked, and says so. Alex does not get up."*

Two standards that had acceptance criteria hanging in air now reference it.
*Unaided onboarding* points at **step 3** (the one flash that turns a board on the desk
into a board in the fleet list) — its criteria are that step's parts. *Embeddable OTA*
points at the whole journey. Both are additive `Supported By` blocks. We did not reword any criterion. The stale "There are no written CUJs" paragraph is gone from
`spec/open-questions.md`.

**Key approach — the Driver uses segments. That is the one real design decision here**.
CUJ-1 crosses four releases (R0 enroll · R1 OTA transport · R2 auto-rollback · R3 the
library). Thus, no single harness can play it until `R3-test-1` exists. The template's single
`Driver:` field would thus were dead on arrival, and fleetforge's T3 gate (which
`/replan` runs at sprint close) would stayed red until R3 landed, blocking sprint
transitions that have nothing to do with this release. Instead each segment names its own
harness and declares whether it is gradeable:

| Steps | Segment | Harness | Gradeable |
|---|---|---|---|
| 1–2 | Sketch compiles with the library | the worked example's build (`R3-fw-3`/`R3-fw-4`) | not yet |
| 3 | One flash → on the fleet | `just agent-qemu esp32`, `just agent-qemu-smoke esp32`, `pytest tests/test_enroll.py` | yes, agent path |
| 5 | OTA a changed build | `just sim-fleet 1 --capabilities ota` + `POST /v1/devices/{id}/deploy`. On-device per `docs/runbooks/agent-qemu.md` | yes, agent path |
| 6 | A bad build recovers itself | `R3-test-1`'s QEMU rollback run | not yet (R2/R3) |

A segment with no harness is **not graded and is not a pass** — the distinction matters,
because the failure mode of a partial CUJ is quietly scoring the easy half and calling it
green.

The Judge is three-part per the template: deterministic must-pass (device online after
step 3, reported `fw_version` equals the uploaded build's after step 5, `rolled-back` and
the prior version after step 6, a duplicated command downloads once), hard-fail traps that
cap the score at 0 (any UART log read, any physical retrieval, any token/PSK/broker
credential in a log or URL, a layout-mismatched build flashed rather than refused, a
milestone shown as reached while no longer true), and an LLM rubric graded by `jeep`
against the Success Criteria. Success Criteria cite numbers already committed in
`prd.md` → *Requirements & targets* (5 min healthy deploy, 2 s dashboard reflection,
Fleet safety 100%) rather than inventing new ones.

**T1**. `just test` — ruff, `ruff format --check`, mypy and **929 tests** green.

**T2 acceptance evidence.**
* `spec/cujs.md` exists and carries the sketch/DevKit CUJ — read back in full.
* `grep -n "cujs.md" spec/standards.md` → line 23 (*Unaided onboarding*) and line 134
  (*Embeddable OTA*), both `Supported By` blocks.
* `grep -c "no written CUJs" spec/open-questions.md` → **0**. The section's other three
  paragraphs intact.
* **Every harness the Driver claims exists today actually resolves** — `just --show` for
  `agent-qemu`, `agent-qemu-smoke` and `sim-fleet` all OK. `pytest --collect-only
  tests/test_enroll.py` → **29 tests collected**. A Driver citing a recipe that does not
  exist is the specific failure this check went in to catch.
* The subjective criterion ("says what a person does") was confirmed by the user, which
  also served as the mandatory `CRITICAL.md` review for the `spec/` edits.

**Spec proposal (recorded, not applied)**. CUJ-1 reaches the fleet list through the
*library*, so the flasher-page path R0 actually built is only one of its steps. A second
CUJ for the prebuilt-agent path would be fully playable today and would give T3 something
to grade before R3 lands. Filed in `spec/open-questions.md`. Out of scope here because the
task scoped itself to the first CUJ.

**Gotcha for whoever writes CUJ-2**. `spec/` is `CRITICAL.md`-protected and `/implement`
does not edit it. A `spec`-category task is the sanctioned route. It still carries the
escalation (strongest model, mandatory review before commit).

## In Progress

_Tracked in `TODO.md` (live status lives there, not here)._

## Planned Work

### Thin OTA library — Arduino, ESP-IDF component, PlatformIO (Priority: P1)

- **Problem:** A hobbyist's unit of work is a sketch, not a Fleetforge agent. Today the
  only firmware that speaks the protocol is the prebuilt agent, which connects,
  heartbeats and blinks. It does not water the plants, drive the frame, or fly the quad.
  So the product can only update a device that does nothing its owner cares about. Until
  the four verbs are a library that drops into *their* firmware, Fleetforge is a very
  good demo of itself.
- **Target:** R3
- **Added:** 2026-09-22
- **Source:** [`docs/HOBBYIST.md`](../HOBBYIST.md) §4.1 — ranked the #1 unlock for the
  primary persona. Named in [`prd.md`](../../spec/prd.md) → *Scope* as half the
  device-side product surface ("(b) a thin OTA library (ESP-IDF/Arduino) to embed in
  custom firmware"), with no feature file, release or task until now.

**Why R3 and not earlier**. A library is a multiplier on but safe deploy currently
is. Shipping the contract into other people's `setup()`/`loop()` before auto-rollback
exists would spread the unsafe path across custom firmware on boards nobody can reach —
the exact failure the product exists to prevent. Recorded in
[`design/decisions/ota-library-ships-after-safe-deploy.md`](../../design/decisions/ota-library-ships-after-safe-deploy.md).

**The release opens with a spike, not with extraction**. Where a library user's config
lives is genuinely undecided. It sets the scope of everything after it. The agent
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

**Answered 2026-09-22 by `R3-fw-1`: (a), with a correction**. The packaged table cannot be
`ab-4m-v1` (the Arduino upload recipe hardcodes offsets that layout does not use) so the
library ships `ab-4m-arduino-v1`, a second layout id with the same `ota_slot_size`, as a
sketch-local `partitions.csv`. Config stays in a flashable `ff_cfg`. See the Completed Work
entry above and
[`design/decisions/arduino-gets-its-own-layout-id.md`](../../design/decisions/arduino-gets-its-own-layout-id.md).

**What the release contains**

1. **ESP-IDF component**. The agent already separates the protocol from the demo app:
   `ff_ota`, `ff_mqtt`, `ff_enroll`, `ff_cfg`, `ff_store`, `ff_identity`, `ff_net`,
   `ff_time` are eight modules with headers under `agent/main/`. Moving them to a
   component with an `idf_component.yml` is largely mechanical. The design work is
   deciding what the *public* surface is, because whatever ships becomes a contract that
   is additive-only from then on.
2. **Arduino library** wrapping the same C. This is where the persona actually lives
   ([`docs/personas/PERSONAS.md`](../personas/PERSONAS.md) §1 — "writes firmware in
   Arduino IDE or PlatformIO, does not use ESP-IDF directly"). If adopting Fleetforge
   means migrating a project to ESP-IDF, it will not be adopted.
3. **A worked example**, small enough to read in one screen: enroll → heartbeat → handle
   `stage` → report the version that booted. The PRD's Morse-code blinker is the
   documented sample. Thus, the example and the marketing claim are the same artifact.
4. **The first CUJ.** *Unaided onboarding* in [`spec/standards.md`](../../spec/standards.md)
   has acceptance criteria hanging in air. This is because `spec/cujs.md` does not exist. "I have a
   sketch and a DevKit on the desk" is the journey this release is about, and writing it
   gives the standard something to hang from.

**Secondary benefit**. Extraction forces the protocol to stay library-shaped (small,
additive, no implicit dashboard coupling) which is what the device-facing thin waist
claimed to be but was never tested against a second consumer.

**Depends on board profiles**. R3 is the release that produces the second and third real
partition layouts, and the first user who brings a `partitions.csv` we never saw. A
code-resident `SUPPORTED_LAYOUTS` cannot serve that, so step 2 of
[board-profiles.md](board-profiles.md) (the profile table with user-defined entries) depends on this release and must be scheduled with it.

**Out of scope for R3:** Improv reprovisioning (HOBBYIST §4.3, unslotted), PlatformIO
registry publication beyond a working `platformio.ini` recipe, and any application-config
channel — `dn/cfg` stays agent-only by design.

#### Library marker proposal (R3-spec-3, 2026-10-08) — PROPOSED, not applied

`spec/` is protected during `/implement`, so this is the decision written as paste-ready
text for a later `spec:` commit (the route R2-spec-1 and R2b-spec-2 took). **Nothing here is
built.** No agent, server, schema, migration, simulator or test change has been made. The
follow-ups (`R3-fw-6`, `R3-be-1`) are blocked in `TODO.md` until the owner accepts and
applies Patch A. The decision is logged in `DECISIONS.md` (2026-10-08, R3-spec-3), as
proposed only.

**The question.** `spec/flows.md` Flow 2 step 2 warns, with an explicit override, on "a
binary without the Fleetforge OTA library marker" from R3, and `spec/open-questions.md` →
*An OTA image that does not contain the agent* explains why: an image without the library
arms no confirm timer, runs unconfirmed and offline, and no remote action reaches it. The
marker must be readable by the server from the uploaded `.bin` at upload, and must survive
both ESP-IDF and Arduino builds.

**Decided: a 64-byte constant, found by scanning the whole image** (candidate 3 of the
task). It is one `const` struct in the library's shared C core, the part `R3-fw-2` extracts
into the ESP-IDF component and `R3-fw-3` wraps for Arduino, defined in its own translation
unit (planned name `ff_marker.c`). It is not in `esp_app_desc_t` and not at a fixed offset.

Byte layout. Every field is bytes or chars, so byte order never matters. The struct is
4-byte aligned.

| Offset | Size | Field | Value |
|---|---|---|---|
| 0 | 16 | `magic` | `14 a9 48 d1 8f 12 cf dd 46 46 4f 54 41 4c 49 42`: 8 random bytes, then ASCII `FFOTALIB`. As one hex string: `14a948d18f12cfdd46464f54414c4942` |
| 16 | 1 | `format` | `1` |
| 17 | 3 | reserved | `0` |
| 20 | 32 | `lib_version` | the library's version: printable ASCII (`0x20`..`0x7e`), NUL-terminated, non-empty |
| 52 | 12 | reserved | `0` |

The magic was generated once (`secrets.token_hex(8)`, then `FFOTALIB`) and is frozen: it
is never regenerated. The random half means a sketch that prints "FFOTALIB" cannot match by
accident; the ASCII half lets a person find the marker with `grep -a FFOTALIB` or `strings`.

**Reader rule (the server, `R3-be-1`).** Scan the whole image for `magic`. Take each
occurrence in order and accept the first that has at least 64 bytes from the start of the
magic to the end of the image, `format >= 1`, and a valid `lib_version` C string. Read
fields beyond `lib_version` only when `format` says they exist. No occurrence, or no valid
one, is "no marker". A malformed marker is "no marker", never an error (the "malformed is
null, never a lost announce" rule, applied to an image). Cost: one `bytes.find` loop over at
most 1966080 bytes, once per upload. The reference reader the spike used, in full:

```python
MAGIC = bytes.fromhex("14a948d18f12cfdd46464f54414c4942")

def find_marker(data: bytes) -> dict | None:
    pos = data.find(MAGIC)
    while pos != -1:
        if len(data) - pos >= 64 and data[pos + 16] >= 1:
            raw = data[pos + 20 : pos + 52]
            end = raw.find(b"\0")
            if end > 0 and all(0x20 <= b <= 0x7E for b in raw[:end]):
                return {"offset": pos, "format": data[pos + 16],
                        "lib_version": raw[:end].decode("ascii")}
        pos = data.find(MAGIC, pos + 1)
    return None
```

**Writer rules (the library, `R3-fw-6`). Mechanism, so it lives here and not in the spec.**

- Defined once, in `ff_marker.c`, as
  `const ff_lib_marker_t ff_lib_marker __attribute__((aligned(4))) = {...};` with a
  `_Static_assert(sizeof(ff_lib_marker_t) == 64)`.
- **It is kept alive only because library code reads it**, through an `extern` from another
  translation unit: the announce builder (`ff_identity.c::announce_object`) and/or the confirm
  path. Both toolchains compile with `-fdata-sections` and link with `--gc-sections`, so the
  marker's own `.rodata.ff_lib_marker` section survives exactly when that code is linked.
  The spike's `nocall` builds below are the proof: library installed and compiled, never
  called, no marker.
- **Never** force-keep it with `-u`, `KEEP()` or `__attribute__((used))`/`retain`. A
  force-kept marker would mark a sketch that merely has the library installed, which is the
  case the warning exists for.
- **Let the address escape**, do not only read a scalar. A read of `ff_lib_marker.format`
  alone can be constant-folded under LTO and the object dropped. Passing
  `ff_lib_marker.lib_version` as a string (it is the announce's `agent_version` in a library
  build, see the version note) keeps the object, and the linker keeps or drops its section
  as a unit. Unmeasured under LTO: neither spike build uses it, and `R3-fw-6` may check it.
- Growth is additive: a new field takes reserved bytes and a higher `format`. Existing
  fields never move or change type.

**Why the scan survives every toolchain.** An ESP app `.bin` holds the DROM (`.rodata`)
segment bytes verbatim, uncompressed and unencrypted: flash encryption happens on the device,
and a Secure Boot signature is appended, not interleaved. A `const` object is contiguous
inside one segment, so segment headers never split it. The approach depends only on "the
object is linked", which holds the same way on ESP-IDF 4.4 through 6.0, Arduino core 2.x and
3.x, and PlatformIO. In the spike it landed at `0x5a74` (ESP-IDF) and `0x148` (Arduino):
not a fixed offset, and the reader must not assume one.

**What it proves, and what it does not.** It proves the library's code, including its
confirm timer, is linked into the image. It does **not** prove (1) that the firmware starts
the library (a sketch that calls it only on a path that never runs still carries it), (2) that its flash layout is right (`partition_layout` and the
partition-table fingerprint check that), or (3) that its own logic works (R5's custom
self-test). Three named gaps.

**On the wire (`R3-fw-6`).** One additive, flat `up/announce` field, `"lib_marker": 1`: the
`format` of the marker in the **running** image. Absent on any image without one, which
includes every agent up to 0.4.7, so every board in the field today. The server never
refuses or warns on a device for this field (`null`/absent never warns). The pre-check
gates on the marker of the **image being sent** (the upload verdict), not on the board's
announce: the board's field says what it runs now, the warning is about what it will run
next. Additive under *Evolution rules* rule 2; `proto` stays `1`; flat, so CBOR stays a
drop-in. Malformed (not an integer, a boolean, or outside `1..255`) is stored as null and
logged, never refused, on both the ingestor and `/v1/enroll`. Size: `,"lib_marker":1` adds
15 B to an announce of about 590 B; esp-mqtt's buffer is 1024 B.

**The stock agent carries the marker too**, from the first agent built on the `R3-fw-2`
component: it links the same code, and the rule is simply "has the confirm path, has the
marker". So `R3-fw-6`'s acceptance "the stock agent and every pre-R3 board announce none"
reads as "agents ≤ 0.4.7 (pre-R3) announce none" (clarified in `TODO.md`). `R3-be-1`'s "not
for the stock agent's own bundles" stands: agent bundles are published through
`firmware/publish.py`, never through `POST /v1/artifact`, so they are never checked, and the
0.4.x bundles carry no marker.

**Pre-check (`R3-be-1`), named here so it is not re-decided.** Code `no_library_marker`, a
gating warning (`needs_override: true`), the second gating code after `rollback_incapable`:
`/deploy` answers 409 unless the body has `override: ["no_library_marker"]`. Its sentence
lives in `deploy_precheck.py`. The verdict must be computed at upload and stored, because a
deploy never reads bytes (R1-be-3; the R2b-be-3 entry notes that "a verdict per artifact
would need a migration"). Storage is `R3-be-1`'s to decide, with two constraints:
`artifacts.provenance` is producer-copied identity ("copied, never recomputed",
`db/models.py`), a poor home for a server-computed verdict; and a new column is an alembic
migration, which is CRITICAL. **Artifacts uploaded before `R3-be-1` have no verdict and
count as unknown: they never warn** (the "null never warns" rule; a named gap).

**Consumers each dependent touches.**

- `R3-fw-6`: `ff_marker.c` (+ its header) in the `R3-fw-2` component, and
  `ff_identity.c::announce_object` (adds `lib_marker` and is the extern reader); Patch B in
  the same commit.
- `R3-be-1`: `api/routers/artifacts.py` (the scan next to the `detect_merged(data)` call,
  about line 261), the stored verdict, `deploy_precheck.py` (`NO_LIBRARY_MARKER`,
  `GATING_CODES`), `api/schemas.py::OverrideCode` (the `Literal` behind
  `DeployRequest.override`, kept equal to `GATING_CODES` by `tests/test_deploy_precheck.py`),
  and, for the board's field, `ingestor/protocol.py::AnnouncePayload`, `ingestor/store.py`,
  `api/schemas.py::EnrollRequest` (storing it is optional; nothing gates on it).

**Rejected.**

1. **A string in `esp_app_desc_t` (`project_name` or `version`).** The maker owns both:
   `project_name` is their CMake project, and `version` is already the build label that
   becomes `fw_version` and pre-fills the upload form (`frontend/src/appImage.ts`). Hijacking
   either breaks something the user sees. And in an Arduino build the descriptor is compiled
   into the core's precompiled libraries, so a library cannot write it at all: the spike's
   Arduino builds (core 2.0.17) carry `version='esp-idf: v4.4.7 38eeba213a'`,
   `project_name='arduino-lib-builder'`. There is no free field.
2. **`.rodata_custom_desc` (`esp_custom_app_desc_t`, fixed at `0x120`, right after
   `esp_app_desc_t`).** Attractive because the 256-byte head would hold it. But it is one
   slot per application, which the maker may use; it depends on every core's linker script
   placing that section (the PlatformIO ESP-IDF copy does, `sections.ld.in`; every Arduino
   core and platform version would have to be re-verified forever); and the ESP-IDF docs keep
   it alive with `-u custom_app_desc`, which an Arduino library cannot add.
3. **A dedicated output section the library links in.** Needs a linker-script change:
   impossible from an Arduino library and fragile across cores.
4. **An ELF symbol or note.** The server receives the `.bin`, which has no symbols.

The scan needs no linker cooperation at all.

**Version note for `R3-fw-2` / `R3-fw-6` (not spec text).** `lib_version` is the
component's version string; `R3-fw-2` picks its single source. `ff_identity.c` already says
that from R1 "the agent is a component inside a user firmware and only `fw_version` moves",
so in a library build `agent_version` is the same constant as the marker's `lib_version`,
and `fw_version` stays `esp_app_desc_t.version`. **Side finding for `R3-fw-3`/`R3-fw-4`**:
in an Arduino build `esp_app_desc_t.version` is the core's IDF string (above), so
`fw_version` and the upload form's version pre-fill are wrong for every Arduino build until
the library supplies the maker's version another way. Not fixed here.

**Spike results (2026-10-08).** PlatformIO 6.1.19, platform `espressif32` 7.0.1, board
`esp32dev`. A three-file mini library (`fflib.h`, `ff_marker.c` with the exact bytes above
and `lib_version "0.0.0-spike"`, `fflib.c` whose `fflib_begin()` prints `format` and
`lib_version` through an `extern`) and an app that calls `fflib_begin()` only when
`FF_CALL=1`. Two envs per toolchain, `call` and `nocall`. Scanned with the reference reader.

| Build | Scan result | `ff_lib_marker` in the ELF | `esp_app_desc_t` |
|---|---|---|---|
| Arduino core 2.0.17 (IDF 4.4.7), `call` | `marker format=1 lib_version=0.0.0-spike @0x148` | `3f400148 D` | `version='esp-idf: v4.4.7 38eeba213a'`, `project_name='arduino-lib-builder'` |
| Arduino core 2.0.17 (IDF 4.4.7), `nocall` | `no marker` | absent | same |
| ESP-IDF 6.0.1 (component), `call` | `marker format=1 lib_version=0.0.0-spike @0x5a74` | `3f405a74 D` | `version='1'`, `project_name='spike'` |
| ESP-IDF 6.0.1 (component), `nocall` | `no marker` | absent | same |

False positives: none. The reader on every real binary in the repo: `agent/dist/{esp32,
esp32c3,esp32c6,esp32s3}/app.bin` (agent 0.4.7) and `tests/fixtures/firmware/*.bin` (two
0.4.5 app heads, two merged heads): all `no marker`, and none contains `FFOTALIB`. Arduino
core 3.x was not built (2.0.17 is the platform's default); the scan does not depend on the
core, and `R3-fw-3`'s builds re-prove it.

**Paste-ready spec text.** Everything below is for `spec/device-protocol.md`,
`spec/flows.md` and `spec/open-questions.md`, in two patches. **Patch A** is (b), (c), (d)
and (e): apply any time after the owner accepts. **Patch B** is (a): apply **only in
`R3-fw-6`'s commit**, never before, because
`tests/test_ff_cfg.py::TestAnnounceMatchesTheSpec::test_the_firmware_builds_exactly_the_spec_keys`
requires `ff_identity.c` to emit every key in the `up/announce` example, so Patch B alone
turns `just test` red (R2b-spec-2's A4, same reason). Patch A on its own leaves `just test`
green: no test reads the text it adds.

*(a) Patch B. `spec/device-protocol.md` → `up/announce` JSON example.* Anchor: the line
`  "capabilities": ["ota", "selftest", "identify"]` inside the ```` ```json ```` block under
`### \`up/announce\` — identity`. Replace that one line with these two; every other line stays
byte-for-byte (`tests/test_agent_partitions.py` matches `"ota_slot_size": 1966080` and
`"partition_layout": "ab-4m-v1"` as text). The edit is to the last line only, so it composes
with R2b-spec-2's still-owed Patch B (three keys after `ota_slot_size`) in either order.

```json
  "capabilities": ["ota", "selftest", "identify"],
  "lib_marker": 1
```

*(b) Patch A. `spec/device-protocol.md`, a new section.* Anchor: insert immediately before
the line `## Evolution rules`, after the paragraph of `## Hierarchy (V3)` that ends "keeping
the namespace flat and reserving the field.", with one blank line on each side:

```markdown
## Library marker

An image built with the Fleetforge OTA library (the ESP-IDF component or the Arduino
library, and so the prebuilt agent built from it) contains one 64-byte constant, the
library marker. The server looks for it in an uploaded image. It is not in `esp_app_desc_t`
and not at a fixed offset. Every field is bytes or chars, so byte order does not matter.

| Offset | Size | Field | Value |
|---|---|---|---|
| 0 | 16 | `magic` | `14 a9 48 d1 8f 12 cf dd 46 46 4f 54 41 4c 49 42` (hex `14a948d18f12cfdd46464f54414c4942`; the last 8 bytes are ASCII `FFOTALIB`) |
| 16 | 1 | `format` | `1` |
| 17 | 3 | reserved | `0` |
| 20 | 32 | `lib_version` | the library's version: printable ASCII, NUL-terminated, non-empty |
| 52 | 12 | reserved | `0` |

**Reading it.** Scan the whole image for `magic`. Take each occurrence in order and accept
the first that has at least 64 bytes from the start of the magic to the end of the image,
`format` of at least `1`, and a `lib_version` that is a non-empty string of printable ASCII
(`0x20`-`0x7e`) ended by a NUL within its 32 bytes. Read fields beyond `lib_version` only
when `format` says they exist. No occurrence, or no valid one, means the image has no
marker; a malformed marker is no marker, never an error.

**Writing it.** The marker is defined once, in the library, and is present in an image
exactly when the library's code is linked into it; installing the library without using it
does not mark an image. It grows only additively: a new field takes reserved bytes and a
higher `format`, and existing fields never move or change type.

**What it proves.** That the library's code, including its confirm timer, is linked into
the image. Not that the firmware starts the library, not that its flash layout is right
(`partition_layout` and `partition_table_sha256` check that), and not that its own logic
works.

An image without the marker gets a warning with an explicit override at pre-check
([flows.md](flows.md) Flow 2, step 2), never a refusal.
```

*(c) Patch A. `spec/device-protocol.md` → `up/announce` prose.* Anchor: insert as a new
paragraph after the paragraph that begins "`ssid` and `known_networks` say which network the
board is on" and ends "The passphrase and the other networks' SSIDs are never sent.", and
before `#### Partition layouts`, with one blank line on each side:

```markdown
`lib_marker` is the `format` of the library marker (*Library marker* below) in the image
the board is running, an integer. It is absent when that image has no marker, which
includes every agent built before the library. It is additive under *Evolution rules*,
rule 2, and `proto` stays `1`. The server never refuses or warns on a device for it: the
pre-check reads the marker from the image being sent, not from the board. The enroll body
carries it like every other announce field, and the server stores a malformed value as
null rather than refusing the request.
```

*(d) Patch A. `spec/flows.md` line 144* (Flow 2, *Decisions (operator view)*). Anchor: the
line that begins `- **Open, recorded not decided:**`. Delete ", and how the Fleetforge
library marker is encoded in the app binary header (R3)", so the line reads:

```markdown
- **Open, recorded not decided:** how a sleepy battery node avoids a false rollback from the confirm timer (beyond surfacing its sleepy wake window at pre-check).
```

"Header" was wrong as well as open: the marker is not in the image header. Line 107 ("from
R3, a binary without the Fleetforge OTA library marker …") and line 140 stay as they are.

*(e) Patch A. `spec/open-questions.md` → *An OTA image that does not contain the agent*.*
Anchor: the paragraph that begins `**An OTA image that does not contain the agent.**`
(lines 89-103 today). Replace the whole paragraph with the one below. The first two
sentences, candidate (2) and the closing R5 sentence are unchanged. The third sentence drops
", but cannot tell whether the agent is linked in" (the marker is how it tells), "Two
candidates, not decided." loses "not decided", candidate (1) is rewritten as decided, and
candidate (2) is marked open:

```markdown
**An OTA image that does not contain the agent.** The confirm timer lives in the agent
(`ff_mqtt.c::confirm_timeout_cb`), so an image without it arms no timer. The bootloader
holds it in `PENDING_VERIFY`, and it is rolled back only if the board resets; otherwise it
runs unconfirmed and offline, the deploy stays at `rebooting`, and no remote action
reaches it. The pre-check refuses a merged binary and a wrong layout or slot size. Two
candidates. (1) Decided for detection (R3-spec-3): the library marker read from the image
at upload ([device-protocol.md](device-protocol.md) → *Library marker*), shown at
pre-check as the gating warning `no_library_marker`. It catches the plain-sketch mistake,
not a library that is linked but never started, and not broken logic. (2) Open: a watchdog
the bootloader arms before it enters a `PENDING_VERIFY` image and only the agent's confirm
path disarms, so a silent image resets and rolls back. It needs a custom bootloader (a
one-time USB flash, so not OTA-able), applies only where we own the bootloader (not
Arduino's), and it is unverified whether IDF's startup disables that watchdog before the
app runs. Neither covers an image that has the agent and confirms but whose own logic is
wrong; that is the R5 custom self-test.
```

### R3 task list — moves into `TODO.md` when R3 opens

Written 2026-09-22 alongside `R3-spec-1`, and moved here from `TODO.md` on 2026-10-01
(`S0-ops-1`). `TODO.md` carries Sprint 0 plus the *active* release only, and R3 sits behind
R2 by decision (`design/decisions/ota-library-ships-after-safe-deploy.md`). Nothing here
must be picked up before R2 lands. `R3-spec-1` and `R3-fw-1` are done — see *Completed
Work* above. Release contents: [releases.md](../releases.md) → R3. Journey:
[`spec/cujs.md`](../../spec/cujs.md) → *CUJ-1*.

#### Firmware

- **R3-fw-2**: Extract the protocol into an ESP-IDF component (P1, 2d)
      `agent/main/` already separates protocol from demo app: `ff_ota`, `ff_mqtt`,
      `ff_enroll`, `ff_cfg`, `ff_store`, `ff_identity`, `ff_net`, `ff_time`. Move them to
      a component with an `idf_component.yml`. The agent becomes its first consumer and
      must keep passing `just agent-verify` and the QEMU E2E unchanged.
      The real work is deciding the **public** surface — whatever ships is additive-only
      from then on, exactly like the wire protocol. Keep it to the four verbs, enroll,
      announce/heartbeat, and a version accessor.
      Acceptance: the agent builds from the component with no behavior change, the QEMU
      run in `docs/runbooks/agent-qemu.md` still passes. The component's public
      headers are a strict subset of what `agent_main.c` uses.

- **R3-fw-3**: Arduino library wrapping the same C (P1, 2d)
      **Flow 3 requirement (2026-10-04):** the library must read the same known-networks list the
      agent reads (`R2b-fw-1`), and later carry the Improv handler. Otherwise an OTA to the
      maker's own firmware strands the board on its current network.
      Depends on `R3-fw-2`. The persona writes Arduino or PlatformIO and does not use
      ESP-IDF (`docs/personas/PERSONAS.md` §1). If adopting Fleetforge means porting their
      project, they will not adopt it.
      Ships what `R3-fw-1` chose: layout **`ab-4m-arduino-v1`** as a sketch-local
      `partitions.csv`, config still in a flashable `ff_cfg`
      (`design/decisions/arduino-gets-its-own-layout-id.md`). Two measured constraints:
      the table must travel with the **example**. This is because the prebuild hook only reads the
      sketch folder and never a library directory. And the new layout id is already in
      `device-protocol.md` → *Partition layouts* and `SUPPORTED_LAYOUTS` (2026-10-02). The safety posture needs no custom bootloader (the stock core is already
      `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`) but it is still not optional. A
      configuration that cannot roll back must fail at build or enroll, not warn.
      Acceptance: a stock Arduino IDE install plus this library compiles the example for
      esp32 and esp32s3, and a board flashed from it enrolls.

- **R3-fw-4**: The worked example — enroll → heartbeat → stage → report version (P1, 1d)
      Small enough to read in one screen. The PRD's Morse-code blinker is the documented
      sample. Thus, the example and the product claim are the same artifact. The blinker changes its message between two builds. This makes "the OTA worked" visible from
      across the room rather than only in the dashboard.
      Acceptance: builds unmodified from a clean checkout on both ESP-IDF and Arduino. The README quickstart is exactly the steps a reader follows.

- **R3-fw-5**: Reject a wrong flash layout loudly (P1, 1d)
      A build that does not reproduce a supported layout exactly must announce a different
      `partition_layout`. The server accepts one today
      (`firmware/manifest.py::SUPPORTED_LAYOUTS`) and two once `ab-4m-arduino-v1` lands. Thus,
      the deploy triggers a rejection — but today the message does not tell a library user what to
      fix. **Do not use the IDE's `Maximum is N bytes` line as the check**: `R3-fw-1`
      measured it reading the board menu's `upload.maximum_size` rather than the built
      table, reporting 1310720 for a build whose slots were 1966080.
      Acceptance: a deliberately mismatched layout triggers a refusal at deploy time with a
      message naming the expected layout and slot size. No board is ever flashed into a
      state where the library is running without a rollback-capable bootloader.

#### Test

- **R3-test-1**: E2E in QEMU — example firmware enrolls, updates, rolls back (P1, 1d)
      The library's claim is the same as the agent's. Thus, it gets the same proof.
      `docs/runbooks/agent-qemu.md` boots the real bundle against the dev stack. The
      example must run that path rather than a stubbed one.
      Three runs: a clean enroll, an OTA to a second build whose visible behavior
      differs, and a deliberately broken build that rolls back unaided and reports
      `rolled-back`.
      Acceptance: all three pass with no board, and the rollback run fails the test if the
      device reports `confirmed`.
