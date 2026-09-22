# The OTA library ships after auto-rollback, as R3

**Date:** 2026-09-22 · **Area:** ota-library · **Status:** decided, unimplemented.
Release contents: [../../docs/releases.md](../../docs/releases.md) → R3. Requirements:
[../../spec/standards.md](../../spec/standards.md) → *ota-library*.

## Context

[`prd.md`](../../spec/prd.md) → *Scope* has always named two device-side deliverables:
"(a) a prebuilt agent to flash for instant wow, and (b) a thin OTA library
(ESP-IDF/Arduino) to embed in custom firmware." Only (a) was ever planned. (b) had no
feature file, no release, no task and no contract until this decision.

[`docs/HOBBYIST.md`](../../docs/HOBBYIST.md) ranked (b) the single largest unlock for the
primary persona, on the grounds that R1–R6 are all features *of the agent* — and the
agent connects, heartbeats and blinks. Every release therefore improves the updating of
a device that does nothing its owner cares about.

That review also proposed shipping the library *before* R2's auto-rollback, to get the
on-ramp built early.

## Decision

The library is a release, and it is **R3 — immediately after R2, not before it.** The v1
ladder shifts by one: health & telemetry R3→R4, custom self-test R4→R5, signed OTA
R5→R6; V2 becomes R7–R11.

## Why not before R2

A library is a **multiplier on however safe deploy currently is.** `TODO.md` already
warns that today's Deploy button is unsafe — there is no checksum verification, no
device-armed confirm and no auto-rollback until R2. Handing the four-verb contract to
makers in that state would put the unsafe path inside custom firmware, on boards chosen
precisely because they are hard to reach, which is the exact failure the product exists
to prevent. The blast radius of a premature library is not "one demo board reboots"; it
is other people's installations.

The inverse ordering costs little: R2 is the next release either way, and the extraction
work does not start sooner for being planned sooner.

**Consequence if the order is ever revisited:** the library's documentation must be gated
on rollback being live, and the release must say so. A library that ships with a
"rollback coming soon" note is the same mistake with a disclaimer.

## Why a renumber rather than a decimal or a tail slot

`R{N}-{category}-{number}` is the task-ID format, and `roadmap.md`, `releases.md` and
`TODO.md` all key off integer releases; `R2.5` would be the only non-integer in the
system. Appending the library after R6 was considered and rejected — it puts the primary
persona's on-ramp behind the entire v1 hardening ladder.

The renumber was affordable because **no R3+ task IDs exist yet**: R0 and R1 are the only
releases with tasks, so nothing archived had to be rewritten. The 23 forward-looking
release references in `src/`, `tests/` and `agent/sdkconfig.defaults` were swept in the
same change, so no comment refers to a number that moved. `DECISIONS.md` history was
**not** rewritten — it is append-only, and its entries are accurate statements about what
was true when written.

## Consequence

- The device protocol acquires a second consumer. This is the point: the device-facing
  thin waist has always claimed to be a small additive contract, and it has never been
  tested against anything but the agent. Whatever the library exposes becomes
  additive-only from the moment it ships.
- The release cannot be estimated until the config-storage question is answered — the
  agent's `ff_cfg` flash partition has no equivalent in an Arduino build. That question
  is in [`../../spec/open-questions.md`](../../spec/open-questions.md) and `R3-fw-1` is a
  spike, not an implementation task.
- `spec/cujs.md` gets written here. *Unaided onboarding* has had acceptance criteria
  hanging in air since 2026-09-11 for want of a journey to hang them from, and "I have a
  sketch and a DevKit on the desk" is that journey.
