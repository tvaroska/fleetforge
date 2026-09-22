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

`R3-fw-1` answers this on metal-or-QEMU before the rest is estimated. The question is
recorded in [`spec/open-questions.md`](../../spec/open-questions.md).

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

**Out of scope for R3:** Improv reprovisioning (HOBBYIST §4.3, unslotted), PlatformIO
registry publication beyond a working `platformio.ini` recipe, and any application-config
channel — `dn/cfg` stays agent-only by design.
