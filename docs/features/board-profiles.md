# Board Profiles

How Fleetforge models "what kind of board is this" — the flash layout a device runs, how
the server learns it, and what it refuses when the two disagree. Covers `partition_layout`,
`ota_slot_size`, and the parameters neither of them carries today.

Completed-work archive for this feature area (`docs/features/`). This holds the plan
**substance**, not links — `.claude/plans/*` are local and gitignored, so their reasoning
must be captured HERE (and decisions logged in `DECISIONS.md`). When `/implement`
finishes a task, it appends a completed entry below.

---

## Completed Work

_None yet._

## In Progress

_Tracked in `TODO.md` (live status lives there, not here)._

## Planned Work

### Named profiles + measured attestation (Priority: P1)

- **Problem:** The server cannot tell a board that *is* `ab-4m-v1` from a board that merely
  *says* it is. `ota_slot_size` is measured on-device and honest
  (`agent/main/ff_identity.c:84` reads `running->size` from the live partition table), but
  `partition_layout` is the compiled-in constant `FF_PARTITION_LAYOUT` — a claim about the
  table, never a reading of it — and the announce schema accepts any string for it
  (`api/schemas.py:143`, free text, `max_length=64`). `SUPPORTED_LAYOUTS`
  (`firmware/manifest.py:37`) gates *bundles* at publish time and is never consulted for a
  device. So two physically different tables can both announce `ab-4m-v1` and pass every
  check `_check_compatible()` performs. Separately, nothing anywhere records flash chip
  size, slot count, or whether the device's bootloader supports rollback — and that last
  one is not recoverable by OTA (see *Why the bootloader field matters* below). There is
  also no list for a user to choose from: the catalog is a one-entry Python dict.
- **Status:** Planned
- **Target:** step 1 → R2 · step 2 → R3
- **Added:** 2026-09-23
- **Source:** 2026-09-23 competitive review of ElegantOTA (§8.3–8.4, in the orchestration
  repo at `products/docs/elegantota-review.md`, outside this repo) — a comparison of how
  ElegantOTA, ESPHome and LibreTiny model boards, plus the ESP-IDF 6.2 migration note on
  bootloader-dependent rollback. Findings reproduced below so this file stands alone.

**The shape.** A `partition_profiles` table seeded from a checked-in catalog, rows flagged
`builtin` (immutable) or `user`. The device announces **both** its claimed id and a
**measured fingerprint** read from the live partition table. The server compares them:

| Announce | Server behaviour |
|---|---|
| known id, fingerprint matches | normal — nothing changes from today |
| known id, fingerprint **differs** | **refuse to deploy**; flag *"device disagrees with its profile"* |
| unknown id, valid fingerprint | create a `detected` profile in pending state; operator names and adopts it |
| — | an operator may also define a profile up front via API/UI |

Artifacts keep carrying `partition_layout`, so the artifact model does not change. The
point is not to stop using the name; it is to **stop trusting it unverified**.

**Why this and not the two alternatives.** Both were costed and rejected:

- **Static catalog in code** — grow `SUPPORTED_LAYOUTS` into a richer frozen dict, user
  layouts arrive by PR. Cheapest and validation is trivially total, but "the user defines
  their own layout" then requires a release, which is incompatible with R3's premise: the
  maker brings their *own* firmware and often their own `partitions.csv`. Self-hosting at
  V2 would turn every custom layout into a fork.
- **Measured-only** — drop the name as a gate, validate raw geometry, demote
  `partition_layout` to a display hint. Genuinely the cleanest identity (a table
  fingerprint distinguishes two boards that both claim `ab-4m-v1`, which no name-based
  scheme can), and it generalises past ESP32. Rejected because `artifacts.partition_layout`
  is load-bearing in `_check_compatible()`: removing the name forces artifact↔device
  compatibility to be re-derived from raw geometry, which is a protected-`spec/` change and
  more churn than the problem justifies. The name is also what logs, the dashboard and
  support conversations actually want.

The hybrid keeps the name for humans and artifacts, and adds the measurement for the
machine. Validation stops being a lookup and becomes a **comparison of two independent
sources**, which is the only arrangement that catches the failure above.

**Step 1 — R2, small.** Do this much and stop:

1. Add the measured fingerprint to `up/announce` (`spec/device-protocol.md` change —
   propose, do not edit): slot count, each slot's offset and size, flash chip size,
   `rollback_capable`, and a `partition_table_sha256` over the decoded table.
2. Store it on `devices`; extend `IDENTITY_FIELDS` in `registry.py`.
3. `_check_compatible()` refuses on claim-vs-measurement mismatch, with the same
   name-what-did-not-match style as the three existing 409s.
4. Turn `SUPPORTED_LAYOUTS` into a profile dict carrying the new fields — **still in
   code**, still checked against `agent/partitions.csv` by
   `tests/test_agent_partitions.py`.

That buys the safety net and the validation for two columns: no new UI, no migrations
beyond the columns, no seeding, and no "who may define a profile" auth question.

**Step 2 — R3, and not before.** Move the catalog into the table, seed the builtins, allow
user-defined profiles via API/UI. The trigger is evidence, not the calendar: R3 produces
the second and third real layouts (`ab-4m-arduino-v1` already exists,
[`design/decisions/arduino-gets-its-own-layout-id.md`](../../design/decisions/arduino-gets-its-own-layout-id.md)),
and a maker with a stock `min_spiffs` table is the first user who cannot be served by a
code-resident dict. Building the table before that evidence is speculative schema.

**Why the bootloader field matters, and why it cannot wait for a redesign.** Rollback is
implemented jointly by the second-stage bootloader and the app, and **Fleetforge OTA
replaces the app, never the bootloader**. Per the ESP-IDF 6.2 migration notes, a device
whose bootloader predates rollback support never transitions
`ESP_OTA_IMG_NEW → ESP_OTA_IMG_PENDING_VERIFY`; the app detects this at startup and marks
itself valid to keep the OTA state consistent, *without* any rollback protection. Such a
board can never gain the safety net from us at any version, and it reports identically to a
protected one — image confirms, announce lands, dashboard green. The three failure modes
enumerated in [`TODO.md`](../../TODO.md) all assume the net is there: mode 1 (*image fails
to boot*) is delegated to the bootloader, and mode 2 (*boots, joins, never confirms*) is
the one we proved on metal. On such a board **both** silently degrade to mode 3's
no-automatic-recovery, and nothing distinguishes it. `esp_ota_get_state_partition()`
returning `ESP_OTA_IMG_NEW` where `PENDING_VERIFY` was expected is the detectable signal.

**Parameters to validate.** Note the pattern: the geometry checks that matter are already
written — they just run against the *bundle we build*, at build time, and have no
counterpart for a device announcing itself.

| Parameter | Bundle (build/publish) | Device (announce/deploy) |
|---|---|---|
| chip vs artifact target | — | ✅ `deploys.py` — the device's `platform_type`, never a request field |
| slot size ≥ artifact size | ✅ `bundledir.py:229` | ✅ `_check_compatible()` |
| layout name equality | ✅ `catalog.py:194` manifest vs index | ✅ `_check_compatible()` device vs artifact |
| `ota_slot_size` matches claimed layout | ✅ `bundledir.py:186`, `catalog.py:205` | ❌ — no per-layout expectation is consulted |
| no `factory` partition | ✅ `make_manifest.py:238` | ❌ — `agent_main.c:128` only *logs* it |
| ota_0 and ota_1 both present, equal size, contiguous | ✅ `make_manifest.py:243-253` | ❌ |
| `ff_cfg` present | ✅ `make_manifest.py::config_partition` | ❌ |
| flash chip size | — | ❌ new |
| bootloader rollback-capable | ✅ `verify_bundle.py` asserts the Kconfig | ❌ new — and not implied by the bundle's config, see below |
| partition-table sha256 vs profile | — | ❌ new — the whole point of the fingerprint |

Every ❌ in the right column becomes a ✅ by transporting the same rule to `up/announce`,
which is why step 1 is small: the predicates exist and are tested, they need a second
caller and a wire field to read.

**Open, to settle when step 1 is written:** the precedence rule when claim and measurement
disagree (refuse-and-flag is proposed above, but "adopt the measurement and warn" is
arguable for a re-flashed board), and whether a `detected` profile is per-fleet or global.
The choice of this design over the two alternatives gets a `DECISIONS.md` entry when step 1
lands, not before — it is a plan until something is built.

**Out of scope:** a board *name* database. All three neighbours converge on chip variant +
flash geometry as the only schema that carries weight — ESPHome's 298-entry esp32 board
list holds `{name, variant}` and is not consulted for partitioning at all. A Fleetforge
board list would be 300 rows of cosmetics wrapped around two fields we already have.
