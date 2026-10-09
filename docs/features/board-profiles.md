# Board Profiles

How Fleetforge models "what kind of board is this" — the flash layout a device runs, how
the server learns it, and what it refuses when the two disagree. Covers `partition_layout`,
`ota_slot_size`, and the parameters neither of them carries today.

Completed-work archive for this feature area (`docs/features/`). This holds the plan
**substance**, not links. `.claude/plans/*` are local and gitignored, so their reasoning
must live HERE (and decisions logged in `DECISIONS.md`). When `/implement`
finishes a task, it appends a completed entry below.

---

## Completed Work

### R3-fe-1 (2026-10-09): the dashboard names a detected profile

Frontend, e2e harness, docs and a `justfile` comment only: `src/`, `alembic/`, `spec/`,
`agent/`, `mosquitto/` and `.env*` untouched, no CRITICAL.md path. Decision: `DECISIONS.md`
2026-10-09 (R3-fe-1). Board-profiles step 2, the dashboard half.

As built:

- **A "Partition profiles" section** between the fleet table and the upload form
  (`PartitionProfiles.tsx`). The named table (Name, Kind, OTA slot, Boards, Fingerprint) lists
  every deployable profile. Under "Detected, not named yet", each pending map shows the board
  it was first seen on (its name when the fleet has one), the boards on it now, its measured
  slot and flash chip, the full fingerprint, and a form: `Name for this map` and `Name and
  adopt`. A `Name for this map` input is the only way to adopt; the form asks for an OTA slot
  in bytes only when the row has none measured. A name is final once adopted, and the form says
  so. Naming is `PATCH /v1/partition-profiles/{sha}` with `{layout_id}`; 409/422 sentences render
  verbatim, a bad name is refused client-side first (`profileName.ts`, mirrors
  `PROFILE_LAYOUT_ID_PATTERN` and the reserved `unknown` sentence).
- **The announce hint (D1).** `useFleet` returns `announceSeq`, bumped once per coalesced burst
  that held a `device.announce` frame and on every stream re-open after the first. `useProfiles`
  (`profiles.ts`, the `deploy.ts` idiom) reads on mount, on a hint change, and after an adopt,
  dropping out-of-order responses. No second EventSource, no new event type, no poll.
- **Upload options are adopted names only (D3).** `UploadBuild` takes `profiles` and offers
  `adoptedNames(profiles)`, defaulting to the one effective layout every board of the chosen
  chip shares (`effectiveLayout`: announced adopted id, else the adopted profile with the
  board's fingerprint), else `ab-4m-v1`. `d.partition_layout` is no longer read, so `unknown`
  is never an option. Before the list loads the option is `ab-4m-v1`.
- **The pre-check card** says `board <profile> (announces unknown)` when
  `device_partition_profile` differs from the announced id (`deployPrecheck.ts::fitLine`).
- **`api.ts`** mirrors `PartitionProfileSummary`/`Adopt`/`List`, `listPartitionProfiles`,
  `adoptPartitionProfile`, and the optional `DeviceSummary.flash_chip_size /
  partition_table_sha256 / rollback_capable` and `DeployPrecheck.device_partition_profile`.
- **E2E scenario 7, `adopt-detected-profile`** (`frontend/scripts/update-flow-e2e.mjs`), between
  `deploy-broken` and `traps`. A third simulated board (`--partition-layout unknown`, a per-run
  `--partition-sha`, `--flash-chip-size 4194304`) is started only once the page is loaded. The
  scenario checks: (1) fewer than 32 pending rows; (2) the board is online announcing `unknown`
  and the fingerprint; (3) the pending row appears with no reload and carries the device id,
  `1,966,080`, `4,194,304`, and the API agrees (`detected`, no name, detecting board); (4) the
  pre-check of the good build is refused, no Send, with "adopt it by naming it" and the
  fingerprint; (5) the upload form's layout options never include `unknown`; (6) naming it
  turns the row deployable, with the notice and the API (`layout_id`, `deployable`, `adopted_at`);
  (7) a build uploaded under that name is offered, the pre-check is deployable and names the
  profile (API: profile = the name, layout = `unknown`), Send is 202, and the card reaches `good`
  with `confirming` before `confirmed`. A failure before naming deletes its own pending row.
  `E2E_BREAK=adopt-detected-profile` flips (6)'s expectation.

T1: `npm test` 688 passed in 34 files (was 651 in 31): new `profiles.test.ts`,
`profileName.test.ts`, `PartitionProfiles.test.tsx`, and new cases in `fleet.test.tsx` (announce
bump per burst, heartbeat does not, first open skipped), `UploadBuild.test.tsx`,
`deployPrecheck.test.ts`, `api.test.ts`, `Dashboard.test.tsx` (announce frame causes a second
profile read). `npm run typecheck` and `npm run build` clean; `node --check` on the harness;
`uv run pytest` 1745 passed, `ruff check`, `mypy src/ scripts/` clean.

T2 (dev stack, `just update-e2e`, run `mv135kum`): 7/7 pass; adopted `cmd_id
b0a9d8ab107e4b01b3a41a1b11dc0e05`. `/tmp/ff-r2b-test-3/adopt-detected-profile.txt` shows, in
order, the pending row (per-run fingerprint, 1,966,080 slot, 4,194,304 flash, the detect board),
the refusal, the named row `uemv135kum-map`, and the deploy card `good`. The database row is
`detected|uemv135kum-map|t|8278288aa341`. Vacuity: `E2E_BREAK=adopt-detected-profile` exited 1,
`FAIL adopt-detected-profile: the row's data-deployable is true, wanted false`, 6/7 pass.
(An earlier draft of the break check waited for the pending row, which is present at the moment
of the click, so the run still passed; the check now waits for the settled row.)

Named gaps:

- No UI for `POST` (defining a profile up front from a fingerprint) and none for `DELETE`
  (forgetting a pending map). The API has both; the `curl`/runbook route stays.
- The pre-check card's refusal says "adopt it by naming it" but does not link to the section.
- The fleet table's columns are unchanged: a board on an adopted map still reads `unknown`
  in the data it announces; the profile name shows in the pre-check card and the Boards count.
- Every `just update-e2e` run leaves one adopted profile behind (permanent once an artifact
  names it, like the labels). `ruff format --check .` flags `docs/features/ota-library.md`
  (a markdown code block), which this task did not touch.
- The board-profiles spec paragraph proposed by R3-be-2 is still unapplied; no new `spec/`
  change is proposed here.

### R3-be-2 (2026-10-09): partition profiles table, detected and user-defined

CRITICAL (new Alembic migration `0007`). Decision: `DECISIONS.md` 2026-10-09 (R3-be-2).
`spec/`, `agent/`, `frontend/` and `mosquitto/` untouched. Board-profiles step 2.

As built:

- **Table `partition_profiles`** (migration `0007`, model `PartitionProfile`), **global**
  (D1), keyed by `partition_table_sha256` (PK, lowercase hex CHECK); `layout_id` is a
  nullable UNIQUE name (`SafeSegment` shape, never `unknown`); `origin` is
  `builtin | user | detected` (CHECK, never changes); `ota_slot_size`, `flash_chip_size`,
  `detected_device_id` (no FK), `created_at`, `adopted_at`. Adopted = named = deployable;
  only `detected` may be pending. CHECKs `adopted_is_named`, `only_detected_pending`,
  `adopted_has_slot`, `slot_positive`. The migration seeds the two builtins from retyped
  literals; `tests/test_partition_profiles.py` pins them equal to `BUILTIN_LAYOUTS`.
  **`downgrade()` drops operator profiles and adoptions.**
- **`SUPPORTED_LAYOUTS` → `BUILTIN_LAYOUTS`** (D5): the seed, the firmware's
  `FF_KNOWN_LAYOUTS`, the spec table, and what agent bundles validate against
  (`bundledir.py`, `catalog.py` stay on it: no DB session at bundle load). What the server
  supports at runtime is the table, read per request as a `LayoutCatalog`
  (`fleetforge/partition_profiles.py::load_layout_catalog`).
- **Gate (D7).** `deploy_precheck.refusals(..., catalog=)` (required keyword; the module
  stays pure). Effective layout = the announced id if adopted, else the adopted profile
  the fingerprint matches, else `unsupported_layout`. A pending fingerprint appends one
  sentence ("recorded as a detected profile … adopt it by naming it …") to the unchanged
  old sentence. The fingerprint check runs only when the announced id itself is adopted, so
  a builtin id on a foreign table is still `partition_table_mismatch`. The multi-layout
  fix names an operator profile with "the partitions.csv it was defined from" instead of
  echoing its id. `DeployPrecheck.device_partition_profile` (additive) is the effective id.
- **Detection (D6)** in the ingestor, after the announce UPDATE returned a live row:
  `note_detected` is one `INSERT … SELECT … WHERE NOT EXISTS (layout named) AND pending <
  32 ON CONFLICT DO NOTHING RETURNING`, in a SAVEPOINT (`except SQLAlchemyError`: warn,
  keep the announce). INFO on a new row; WARNING when the cap blocked one. `store.py`
  unchanged. Retained replays detect too.
- **Upload.** `POST /v1/artifact` loads the catalog before reading the body; the
  `partition_layout` must be an adopted profile and its slot is the ceiling. The 400 lists
  `sorted(catalog.layouts)`.
- **API** (admin-only, `api/routers/partition_profiles.py`):

  | Route | Answer |
  |---|---|
  | `GET /v1/partition-profiles` | 200 `{profiles: [...]}`: builtins, adopted by name, pending oldest first; each with `deployable` and live `device_ids` |
  | `POST /v1/partition-profiles` `{layout_id, partition_table_sha256, ota_slot_size}` | 201 user profile, deployable; 409 duplicate sha (pending: "adopt it by naming it") or name; 422 `unknown`, uppercase sha, slot ≤ 0 or > 4 MiB, extra keys |
  | `PATCH /v1/partition-profiles/{sha}` `{layout_id, ota_slot_size?}` | 200 adopts a pending row (measured slot wins); 409 builtin, already named, slot differs, name taken; 422 slot missing, measured slot > 4 MiB; 404 unknown/malformed sha |
  | `DELETE /v1/partition-profiles/{sha}` | 204 pending or image-less user/adopted row; 409 builtin or "N artifacts are labelled for <id>"; 404 |

- **No new SSE type**: the announce that creates a detected row already emits
  `device.announce`; the dashboard re-reads profiles on it (R3-fe-1).
- **Behaviour change, named:** an announce whose id the server does not know but whose
  fingerprint is an adopted profile now resolves to that profile (before, any unknown id
  was refused). An id-less board with a known fingerprint is layout-checked under that
  profile's id. Two `tests/test_deploy_precheck.py` cases were updated for this.

**Spec proposal (not applied; `spec/` is protected).** `spec/device-protocol.md` →
*Partition layouts*, a paragraph after the table:

> The table is the builtin set: every agent and library build knows these ids and their
> fingerprints. A server may also hold operator-defined profiles, keyed by
> `partition_table_sha256`. A board on such a map announces `unknown`, because its firmware
> knows only the builtin ids. The server resolves it by fingerprint, and refuses it until an
> operator has named the profile. An operator-defined profile never changes a builtin row,
> and a builtin id is never reused.

If R3-fw-5's Patch A (the reserved `unknown`) is not yet applied, this goes in with it.

**Release note.** Migration `0007`. The ingestor never fails a message on a `0006` schema
(detection is in a savepoint, `UndefinedTableError` is caught), but nothing is detected
until the api has migrated. As with be-5 and be-6, restart `fleetforge-ingestor` after the
api rollout; its retained replay then records every board already on an unknown map. A
downgrade drops operator profiles; artifacts labelled with a user layout then neither
upload nor deploy until the profile is re-created.

**T1.** `just test`: lint, mypy and 1680 tests green, including the new
`test_partition_profiles.py` and `test_api_partition_profiles.py`, and `test_schema.py`'s
upgrade → downgrade → upgrade plus `alembic check`.

**T2** (dev stack, api restarted first, then ingestor; run id `1791539097`):

| # | Observed |
|---|---|
| 0 | `alembic_version` `0007`; two builtin adopted rows (`ab-4m-arduino-v1`, `ab-4m-v1`, 1966080, spec shas). The ingestor's retained replay at once recorded X from an older dev board (`0a6341b1728c`); cleared with `DELETE` (204) so be2a would be the detector |
| A | GET: both builtins `deployable: true`, `device_ids` 6 and 7 dev boards |
| B | `just sim … --name be2a --partition-layout unknown --partition-sha X --flash-chip-size 4194304`: ingestor INFO "… recorded as a detected profile, pending adoption"; row `detected`, `layout_id` null, `deployable` false, slot 1966080, flash 4194304, `detected_device_id` `d212564bc71d` (be2a) |
| C | sim again + `docker compose restart ingestor`: still 1 row for X, no bookkeeping warnings |
| D | upload `be2-a-1791539097` (esp32c6, `ab-4m-v1`) 201; precheck `deployable: false`, `unsupported_layout`, old sentence + "This flash map is recorded as a detected profile …", `device_partition_profile: null` |
| E | POST /deploy 409 with the same sentence; 0 `deploy_events` rows |
| F | upload under `be2-map`: 400 "unknown partition_layout; this server understands: ab-4m-arduino-v1, ab-4m-v1" |
| G | PATCH X `{"layout_id":"be2-map"}`: 200, `deployable: true`, `origin: detected`, slot 1966080 kept |
| H | F again: 201 |
| I | precheck `be2-b-1791539097`: deployable, no refusals, `device_partition_profile: be2-map`, `device_partition_layout: unknown`; `be2-a-1791539097`: `layout_mismatch` "runs partition layout be2-map … built for ab-4m-v1" |
| J | sim online (`--capabilities ota`), POST /deploy 202; `requested → … → confirmed` |
| K | PATCH/DELETE builtin 409/409; PATCH X again 409 (name final); DELETE X 409 (1 artifact labelled for be2-map); builtin row unchanged |
| L | POST `be2-user` 201 deployable; repeat 409; `unknown` / uppercase sha / slot 5242880 → 422 ×3 |
| M | be2m announcing `ab-4m-v1` with a foreign sha: no profile row; precheck `partition_table_mismatch` |
| N | `just update-e2e`: 6/6 pass |

### R2b-fw-2 (2026-10-05): agent 0.4.7 announces the three measurements

CRITICAL (agent wire format, the confirm path). Decision: `DECISIONS.md` 2026-10-05
(R2b-fw-2). No server, simulator, migration or frontend change: R2b-be-6 already ingests
all three. `agent/partitions.csv` and `agent/sdkconfig.defaults*` untouched.

As built:

- **Measurements (`ff_identity.c`).** Taken once in `ff_identity_init()`, after the MAC,
  never fatal. `flash_chip_size` from `esp_flash_get_physical_size(esp_flash_default_chip)`,
  with no fallback to `esp_flash_get_size()` (the image header's claim); unreadable means
  the key is **omitted**. `partition_table_sha256` from `partition_fingerprint()`:
  `esp_partition_find(ESP_PARTITION_TYPE_ANY, ESP_PARTITION_SUBTYPE_ANY, NULL)`, entries
  on the default chip only, a static 32-entry array, insertion sort by address, decimal
  `type:subtype:offset:size\n` lines into incremental mbedTLS SHA-256, hex by hand. No
  label, no flags. One boot line: `board: flash chip … bytes (physical), partition table
  sha256 …, rollback_capable true|unknown`. `spi_flash` added to the component's REQUIRES.
- **`rollback_capable`.** `true` or null, never `false` (A3). Observed at the top of
  `ff_mqtt.c::classify_txn()` from `pending_verify()`, before the record is read: a
  deliberate superset of the `TXN_CONFIRMING` branch, so a board OTA'd by 0.3.1 (no
  record) learns it on its first 0.4.7 boot. Stored as `rb_cap` (u8 1) in the "ff"
  credential namespace (cleared by a token change, kept by an OTA), written once.
  `ff_store_save_rollback_capable(void)` takes no argument. The confirm/rollback functions
  are unchanged and NVS-free.
- **NEW-at-target.** `TXN_BOOTED_NEW`: the recorded slot runs in state `new` (the
  bootloader never armed rollback). Nothing on connect; at the announce ack, after the
  byte-identical TXN_CONFIRMING branch and behind `s_rollback_decided`,
  `accept_unverified_image()` marks valid only from NEW and `confirmed` is queued only on
  ESP_OK, with the detail `the bootloader never armed rollback for this image`. It used to
  be "stale transaction record … discarded", which left the deploy at `rebooting`. Not
  `failed` (the image is running; `fw_version` shows the target). **Not reachable in QEMU
  or on the bench S3** (our bootloader always arms rollback; forging otadata is
  forbidden): proven by `tests/test_agent_board_measurements.py` and the review. The bench
  run, and emitting `false`, belong to R2b-test-5.
- **Announce.** The keys follow `ota_slot_size` in spec order; enroll carries them too
  (same object; `rollback_capable` is null at enroll on a fresh board). 465 B in QEMU.
- **Budgets.** Raised to the measured bytes: esp32 1,029,440 (+2,848), esp32s3 1,009,488
  (+2,784), esp32c3 1,069,008 (+3,104), esp32c6 1,118,544 (+3,120).
- **Patch B owed.** `spec/` is untouched by design: Patch B is owed as the owner's `spec:`
  commit right after this one, never before (DECISIONS 2026-10-05 R2b-fw-2 amends A4).
  The three lines go after `"ota_slot_size": 1966080,` in the `up/announce` example:
  `"flash_chip_size": 4194304,`,
  `"partition_table_sha256": "1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed",`,
  `"rollback_capable": true,`. Proved safe in memory: the patched example still parses as
  JSON, all 16 keys are emitted by `ff_identity.c`, and the five keys from `ota_slot_size`
  to `capabilities` appear in spec order (`PATCH-B-SAFE 16`), so
  `test_the_firmware_builds_exactly_the_spec_keys` is green with and without it.

T2 evidence (QEMU esp32, dev stack on 8088, api recreated with
`FF_PUBLIC_BASE_URL=http://10.0.2.2:8088 FF_S3_PUBLIC_ENDPOINT_URL=http://10.0.2.2:9000`,
restored afterwards; trimmed):

```
1  fresh 0.4.7:  I ff-id: board: flash chip 4194304 bytes (physical), partition table sha256
                 1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed, rollback_capable unknown
   announce:     …"ota_slot_size":1966080,"flash_chip_size":4194304,"partition_table_sha256":"1fa6…59ed",
                 "rollback_capable":null,"capabilities":["ota"]}   (465 bytes, agent_version 0.4.7)
   /v1/devices:  {"fw_version":"0.4.7","ota_slot_size":1966080,"flash_chip_size":4194304,"partition_table_sha256":"1fa6…59ed","rollback_capable":null}
2  deploy 0.4.7-fw2b on_command -> staged; stop/start:
     ff-agent: running image: fw_version 0.4.7-fw2b, ota state pending_verify
     ff-id:    board: … rollback_capable unknown
     ff-id:    rollback_capable: true — this OTA-written image booted in pending_verify, so this board's bootloader rolls back (stored)
     ff-mqtt:  transaction 3f06…d441: confirming on ota_1
     ff-mqtt:  this image was written by OTA and is now CONFIRMED: …
     ff-txn:   transaction 3f06…d441 closed — record cleared
   status rows: …, staged, confirming, confirmed;  announce "fw_version":"0.4.7-fw2b" … "rollback_capable":true
   /v1/devices: {"fw_version":"0.4.7-fw2b",…,"rollback_capable":true}
3  stop/start:   ota state valid; board: … rollback_capable true; no transaction, no (stored) line;
                 announce still "rollback_capable":true
4  new token, agent-qemu-recfg, start:  "the ff_cfg enrollment token has changed (780a… -> b463…): erasing …";
                 board: … rollback_capable unknown; enroll 200; announce "rollback_capable":null, sha and size present;
                 /v1/devices rollback_capable null
```

QEMU's flash model answers the physical-size read (4194304), so the "key omitted" path
was not exercised live; it is pinned by the text tests.

Re-verified 2026-10-05 (attempt 2), identical (build B labelled `0.4.7-fw2c`; announce
465 B; status rows staged, confirming, confirmed).
Re-verified again in the mandatory review, identical: all four builds reproduced the budgets
byte for byte, and QEMU steps 1-4 passed with build B labelled `0.4.7-fw2r`.

### R2b-be-6 (2026-10-05): board measurements ingested and stored

Backend only, migration `0006`. Decision: `DECISIONS.md` 2026-10-05 (R2b-be-6). `spec/`
and `agent/` untouched: Patch A was already applied (8cb5335), and Patch B (the three keys
in the spec's `up/announce` example) goes in only with `R2b-fw-2`, because
`tests/test_ff_cfg.py` requires the agent to emit every key in that example.

As built:

- **Columns.** `devices.flash_chip_size BIGINT NULL` (the agent's `uint32_t` passes int4),
  `devices.partition_table_sha256 TEXT NULL`, `devices.rollback_capable BOOLEAN NULL`, right
  after `ota_slot_size`. No default, no CHECK. NULL is "unknown".
- **Normalisers.** `announce_fields.py` gained `normalize_flash_chip_size` (an `int`, not a
  `bool`, in `1..FLASH_CHIP_SIZE_MAX = 2^32 - 1`), `normalize_partition_table_sha256`
  (`fullmatch` of `[0-9a-f]{64}`, never lowercased) and `normalize_rollback_capable`
  (`isinstance(value, bool)` only). Nothing is coerced, and nothing raises. A malformed
  value becomes NULL plus one INFO line with the field, the reason and the device id,
  never the value. The flash ceiling is load-bearing: past int64, asyncpg raises inside
  the transaction and the announce is lost.
- **Announce.** `AnnouncePayload` has the three fields, each with a `mode="before"`
  validator. `store.py::_measurement_values` writes the triple on **every** announce, so a
  key that is absent clears the stored value (the second exception to "absent means no
  change", after `_network_values`). This is also the fail-open direction for R2b-be-7's
  gate: NULL never refuses and never warns.
- **Enroll.** `EnrollRequest` has the same three fields and validators, so a malformed
  value never yields a 422, before or after the burn. `IDENTITY_FIELDS` gained them, so a
  re-enrolment without them stores NULL. `api/routers/enroll.py` is unchanged.
- **Read model.** `DeviceSummary` exposes the three right after `ota_slot_size`, and
  `deploy` stays last. `frontend/src/api.ts` is unchanged: a later frontend task adds the
  mirror.
- **Simulator.** `DeviceIdentity` always announces the three, null when unset, between
  `ota_slot_size` and `capabilities`. New flags: `--flash-chip-size`, `--partition-sha`
  (sent verbatim, junk included) and `--rollback-capable {true,false}`. With no flags the
  sim announces null for all three, as every agent ≤ 0.4.6 does.

T2 evidence (dev stack on 8088; api restarted first and migrated 0005 -> 0006, then the
ingestor; trimmed):

```
0  alembic_version -> 0006; flash_chip_size|bigint  partition_table_sha256|text  rollback_capable|boolean
A  just sim --token … --name be6a --flash-chip-size 4194304 --partition-sha 1fa6…59ed --rollback-capable true
     -> {"flash_chip_size":4194304,"partition_table_sha256":"1fa6…59ed","rollback_capable":true}
B  just sim --name be6a --rollback-capable false      -> {"flash_chip_size":null,"partition_table_sha256":null,"rollback_capable":false}
C  just sim --name be6a                                -> all three null
D  curl enroll be6d00000001 (valid values stored), then mqtt-pub announce
   {"fw_version":"9.9.9","flash_chip_size":18446744073709551616,"partition_table_sha256":"1FA6…59ED","rollback_capable":"true"}
     -> {"fw_version":"9.9.9","flash_chip_size":null,"partition_table_sha256":null,"rollback_capable":null}
   ingestor: "device be6d00000001 announced an unusable flash_chip_size (out of range); stored as null"
             "… unusable partition_table_sha256 (not 64 lowercase hex characters) …"
             "… unusable rollback_capable (not a boolean) …"
             no DataError / OverflowError / Traceback; neither the number nor the uppercase sha in the log
E  curl POST /v1/enroll be6e00000001 with valid values -> 200, all three stored
F  curl POST /v1/enroll be6f00000001 "4MB" / "xyz" / 1  -> 200 (never 422), all three null,
   three "unusable … stored as null" lines in the api log, no values
G  .devices[0] | keys_unsorted -> […,"ota_slot_size","flash_chip_size","partition_table_sha256","rollback_capable","capabilities",…,"deploy"]
```

**Release note (deploy order).** As for R2b-be-5: the ingestor's ORM maps the new
columns, so a new-image ingestor on a `0005` schema fails every message, the retained
replay included, with `UndefinedColumnError`. `services/scripts/deploy.sh` recreates the
ingestor before `docker rollout fleetforge-api` migrates. Restart `fleetforge-ingestor`
after the api rollout.

### R2b-spec-2 (2026-10-04): R2-spec-1 amended; spec not applied

Decided: amend the R2-spec-1 proposal, then apply it (not as filed, not dropped). Three
amendments, now in *Planned Work → Step 1 wire proposal*: `rollback_capable: false` is a
gating pre-check warning `rollback_incapable` that `/deploy` enforces as a 409 unless the
body carries `override: ["rollback_incapable"]` (was: a 409 with no override); the chip-size
field is `flash_chip_size` (was `flash_size`, which the bundle manifest already uses for the
image header's claim); and the spec defines `false` by its meaning, leaving the `NEW`
mechanism here. Application order: Patch A ((b), (c), (d)) after the owner accepts, Patch B
((a)) only in `R2b-fw-2`'s commit. Follow-ups filed and blocked: `R2b-fw-2`, `R2b-be-6`,
`R2b-be-7`; `R2b-test-5` is hardware-gated. Nothing is built and `spec/` is untouched.

### R2-spec-1 (2026-10-03): step-1 wire proposal filed; spec not applied

Proposed three additive `up/announce` fields: `rollback_capable` (`true | false | null`,
measured from an OTA'd image's state, never from the build config), `partition_table_sha256`
(SHA-256 over decimal `type:subtype:offset:size` lines sorted by offset, labels and flags
excluded) and `flash_size` (physical chip size). The text is paste-ready in *Planned Work →
Step 1 wire proposal*, with worked fingerprints for `ab-4m-v1` and `ab-4m-arduino-v1`.
Nothing is built and `spec/` is untouched.

Findings that changed the plan: in ESP-IDF v5.5.5, `esp_bootloader_get_description()`
returns the app's own compiled-in descriptor, so it cannot key the persisted observation
(Arduino-path staleness is an accepted gap); bootloader / partition-table types are real
table entries, not runtime pseudo-entries; and `flash_size` omits itself rather than fall
back to the image header's size. Limit: the first OTA to a rollback-less board is
unprotected, because a board cannot measure its bootloader before it has OTA'd once.

## In Progress

_Tracked in `TODO.md` (live status lives there, not here)._

## Planned Work

### Named profiles + measured attestation (Priority: P1)

- **Problem:** The server cannot tell a board that *is* `ab-4m-v1` from a board that merely
  *says* it is. `ota_slot_size` measures on-device and honest
  (`agent/main/ff_identity.c:84` reads `running->size` from the live partition table). But `partition_layout` is the compiled-in constant `FF_PARTITION_LAYOUT` (a claim about the
  table, never a reading of it) and the announce schema accepts any string for it
  (`api/schemas.py:143`, free text, `max_length=64`). `SUPPORTED_LAYOUTS`
  (`firmware/manifest.py:37`) gates *bundles* at publish time and never takes effect for a
  device. So two physically different tables can both announce `ab-4m-v1` and pass every
  check `_check_compatible()` does. Separately, nothing anywhere records flash chip
  size, slot count, or whether the device's bootloader supports rollback. That last
  one is not recoverable by OTA (see *Why the bootloader field matters* below). There is
  also no list for a user to choose from. The catalog is a one-entry Python dict.
- **Target:** step 1 → R2 · step 2 → R3
- **Added:** 2026-09-23
- **Source:** 2026-09-23 competitive review of ElegantOTA (§8.3–8.4, in the orchestration
  repo at `products/docs/elegantota-review.md`, outside this repo) — a comparison of how
  ElegantOTA, ESPHome and LibreTiny model boards, plus the ESP-IDF 6.2 migration note on
  bootloader-dependent rollback. Findings reproduced below so this file stands alone.

**The shape**. A `partition_profiles` table seeded from a checked-in catalog, rows flagged
`builtin` (immutable) or `user`. The device announces **both** its claimed id and a
**measured fingerprint** read from the live partition table. The server compares them:

| Announce | Server behavior |
|---|---|
| known id, fingerprint matches | normal — nothing changes from today |
| known id, fingerprint **differs** | **refuse to deploy**. Flag *"device disagrees with its profile"* |
| unknown id, valid fingerprint | create a `detected` profile in pending state. Operator names and adopts it |
| — | an operator can also define a profile up front via API/UI |

Artifacts keep carrying `partition_layout`, so the artifact model does not change. The
point is not to stop using the name. It is to **stop trusting it unverified**.

**Why this and not the two alternatives**. Both were costed and rejected:

- **Static catalog in code** — grow `SUPPORTED_LAYOUTS` into a richer frozen dict, user
  layouts arrive by PR. Cheapest and validation is trivially total, but "the user defines
  their own layout" then requires a release. This is incompatible with R3's premise: the
  maker brings their *own* firmware and often their own `partitions.csv`. Self-hosting at
  V2 would turn every custom layout into a fork.
- **Measured-only** — drop the name as a gate, check raw geometry, demote
  `partition_layout` to a show hint. Genuinely the cleanest identity (a table
  fingerprint distinguishes two boards that both claim `ab-4m-v1`, which no name-based
  scheme can), and it generalises past ESP32. Rejected because `artifacts.partition_layout`
  is load-bearing in `_check_compatible()`: deleting the name forces artifact↔device
  compatibility to be re-derived from raw geometry. This is a protected-`spec/` change and
  more churn than the problem justifies. The name is also what logs, the dashboard and
  support conversations actually want.

The hybrid keeps the name for humans and artifacts, and adds the measurement for the
machine. Validation stops being a lookup and becomes a **comparison of two independent
sources**. This is the only arrangement that catches the failure above.

**Step 1 — R2, small**. Do this much and stop:

1. Add the measured fingerprint to `up/announce` (`spec/device-protocol.md` change —
   propose, do not edit): slot count, each slot's offset and size, flash chip size,
   `rollback_capable`, and a `partition_table_sha256` over the decoded table.
2. Store it on `devices`. Extend `IDENTITY_FIELDS` in `registry.py`.
3. `_check_compatible()` refuses on claim-versus-measurement mismatch, with the same
   name-what-did-not-match style as the three existing 409s.
4. Turn `SUPPORTED_LAYOUTS` into a profile dict carrying the new fields — **still in
   code**, still checked against `agent/partitions.csv` by
   `tests/test_agent_partitions.py`.

That buys the safety net and the validation for two columns: no new UI, no migrations
beyond the columns, no seeding, and no "who can define a profile" auth question.

#### Step 1 wire proposal (R2-spec-1, 2026-10-03) — PROPOSED, amended by R2b-spec-2 (2026-10-04), not applied

*Status 2026-10-05 (R2b-fw-2).* Patch A is applied (8cb5335). The server side is built
(R2b-be-6) and the agent emits all three fields from 0.4.7 (R2b-fw-2). Patch B (block (a),
the three keys in the `up/announce` example) is owed as the owner's `spec:` commit right after
R2b-fw-2's, never before it (DECISIONS 2026-10-05 R2b-fw-2, amending A4). The text below is the proposal as written.

`spec/` is protected during `/implement`, so this is the wire half of step 1 written as
paste-ready text for a later `spec:` commit (the route `43675b8` took). **Nothing here is
built.** No agent, server, schema, migration, simulator or test change has been made. The
follow-ups below are filed in `TODO.md` and blocked until the proposal is accepted.
The design choice is logged in `DECISIONS.md` (2026-10-03, R2-spec-1), as proposed only.

*Amended 2026-10-04 (R2b-spec-2).* A1: `rollback_capable: false` is a gating pre-check
warning (`rollback_incapable`) that `/deploy` enforces as a 409 unless the request lists it
in `override`, not a refusal with no override (`spec/flows.md` Flow 2 asks for a warning
with an explicit override). A2: the chip-size field is `flash_chip_size`, not `flash_size`.
A3: the spec text defines `false` by its meaning only; the `NEW`-at-target mechanism stays
in this file, and the agent does not emit `false` until `R2b-test-5` benches it. The text
below is already amended; see `DECISIONS.md` (2026-10-04, R2b-spec-2).

**What the announce carries today.** `agent/main/ff_identity.c::announce_object()` builds
the object in one place, and the enroll body is the same cJSON object plus `token`. So any
new announce field also arrives in `POST /v1/enroll` (`api/schemas.py::EnrollRequest`,
`extra="ignore"`), by construction. `partition_layout` is the compiled constant
`FF_PARTITION_LAYOUT`, a claim. `ota_slot_size` is measured (`running_slot_size()` reads
`running->size`). The server consumers of announce identity are
`ingestor/protocol.py::AnnouncePayload`, the field list in `ingestor/store.py`,
`registry.py::IDENTITY_FIELDS`, the `Device` columns, `api/routers/deploys.py::_check_compatible()`,
`firmware/manifest.py::SUPPORTED_LAYOUTS` (`dict[str, int]`, layout to slot size) and the
simulator (`simulator/device.py`).

**Three additive fields, all flat, all optional.** Evolution rules hold (additive only,
unknown fields ignored by both sides, nothing re-typed, `proto` stays `1`, flat for a CBOR
drop-in).

1. **`rollback_capable`: `true | false | null`. Measured, never claimed.**
   - `true`: this board has booted an OTA-written image in `ESP_OTA_IMG_PENDING_VERIFY`,
     so its bootloader performed `NEW → PENDING_VERIFY`. The agent sees this in
     `ff_mqtt.c::classify_txn()`, in the `TXN_CONFIRMING` branch.
   - `false`: an OTA-written image is running at the transaction record's `target_addr`
     in state `ESP_OTA_IMG_NEW`, so the bootloader did not transition it. Today
     `classify_txn()` treats that case as neither confirming nor valid. It falls through to
     "stale transaction record — discarded", and the deploy parks at `rebooting` with no
     outcome. That is exactly the moment `false` can be measured.
   - `null` (or absent): not yet observed. This is **every freshly flashed board until its
     first OTA**, because a serially flashed board boots with otadata UNDEFINED and never
     enters `PENDING_VERIFY`, and every agent older than the one that implements this. The
     server treats absent and `null` identically.
   - **Never derived from the app's compiled `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`.**
     That is a claim about a build, not a reading of the bootloader in flash. It is the
     same claim-vs-measurement mistake as `partition_layout`, and it is the point of
     `spec/open-questions.md` → *Bootloader attestation on the Arduino path*:
     `verify_bundle.py` asserts the Kconfig for the prebuilt agent bundle at build time
     only, and the Arduino core's bootloader was checked only against `esp32:esp32@3.3.12`
     (`design/decisions/arduino-gets-its-own-layout-id.md`).
   - **The limit, stated plainly: the first OTA to a board whose bootloader lacks rollback
     is unprotected, and the field only reports that afterwards.** A board cannot measure
     its bootloader before its first OTA. The only before-the-first-OTA attestation is a
     bootloader digest, which is deferred below.
   - Not `up/hb` `boot_ok`. `boot_ok` means "not `PENDING_VERIFY` right now", a per-boot
     state. `rollback_capable` is a property of the board.
   - **Unmeasured:** no board with a rollback-less bootloader has been run, so the
     `ESP_OTA_IMG_NEW` signal for `false` rests on the ESP-IDF 6.2 migration note and on
     the `esp_ota_get_state_partition()` contract, not on a bench result. The same note
     says such an app marks itself valid at startup, which could make the observable state
     `VALID`, not `NEW`, and that is indistinguishable from "confirmed, then reset before
     the PUBACK" (`TXN_ALREADY_CONFIRMED`). The implementation task must bench this on a
     board with a rollback-less bootloader before `false` goes on the wire. The spec
     defines `false` by meaning only (R2b-spec-2); this mechanism is the agent's, and
     `false` is not emitted until `R2b-test-5` benches it. (In IDF v5.5.5 an app built with
     `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE` writes `ESP_OTA_IMG_NEW` on set-boot and only
     the bootloader moves it to `PENDING_VERIFY`, per
     `app_update/esp_ota_ops.c::set_new_state_otadata`, so `NEW` at the target is plausible
     for our agent, but still unbenched.)
   - **Persistence.** Once observed, the agent stores it in its NVS namespace so it
     survives the reboots after the observing one. A re-flash through our flasher already
     erases it (`nvs_erase_all` on a token-fingerprint change, DECISIONS 2026-09-14
     S0-fw-4; the flasher erases `nvs`, DECISIONS 2026-09-13).
   - **Known gap: Arduino IDE upload.** It rewrites the bootloader and keeps NVS, so a
     stored `true` could outlive the bootloader that earned it, and a stored `false` could
     outlive the bad bootloader that earned it (remedy: erase flash, or re-flash through
     our flasher). The plan was to key the stored observation to
     `esp_bootloader_get_description()` (`idf_ver` + `date_time`). **Verified in IDF
     v5.5.5 headers and sources, and it does not work.** The function and the
     `esp_bootloader_desc_t` struct exist (`components/esp_bootloader_format`), but the
     header says "Intended for use by the bootloader", and in an app image it returns the
     weak `esp_bootloader_desc` compiled into the **app**. It describes the app's build, not
     the bootloader in flash. So the staleness is an **accepted, named gap**. The only
     sound invalidation key would be a digest of the bootloader region, which is
     `bootloader_sha256` (deferred below).

2. **`partition_table_sha256`: 64 lowercase hex characters, over a canonical serialization
   of the decoded table.**
   - **Entries.** Every entry the partition table itself contains: any type, any subtype.
     That is app `0x00` and data `0x01` and any custom type `0x40`-`0xFE`. Excluded:
     partitions on an external flash chip (`esp_partition_register_external`), and the
     table's trailing MD5 row, which is not an entry. **Verified in IDF v5.5.5**
     (`esp_partition/partition.c::load_partitions`): every entry up to the MD5 row is
     registered, with no filtering, so `esp_partition_find` returns all of them. The
     bootloader / partition-table types `0x02`/`0x03` are not runtime pseudo-entries (the
     planning notes assumed they were). They are ordinary table entries that
     `gen_esp32part.py` accepts when a CSV declares them (bootloader OTA). Neither layout
     below declares any, so they are included by the same rule and need no special case.
   - **Line form.** One line per entry, `{type}:{subtype}:{offset}:{size}\n`, all four in
     **decimal**, sorted by offset ascending, UTF-8, no header, a trailing newline on the
     last line. The hash is SHA-256 of those bytes. The device-side list is in table order,
     so it must sort.
   - **Labels are excluded.** Two tables that behave identically fingerprint identically.
     The agent finds `ff_cfg` by subtype, never by label.
   - **Flags are excluded.** The runtime `esp_partition_t.encrypted` is not the table's
     flag: IDF forces it on for app, OTA-data and NVS-keys partitions under flash
     encryption (`is_partition_encrypted`), which v1 forbids anyway, so device-side and
     CSV-side values would diverge. `readonly` is a known omission.
   - **Why decoded and not the raw `0x8000` sector.** The server and the tests can
     recompute it from a checked-in `partitions.csv` or a profile row with no ESP-IDF
     tooling, and it does not depend on `gen_esp32part`'s MD5 row or padding.
   - **Device side.** Iterate `esp_partition_find(ESP_PARTITION_TYPE_ANY,
     ESP_PARTITION_SUBTYPE_ANY, NULL)`, keep entries whose `flash_chip` is
     `esp_flash_default_chip`, sort by `address`, and hash with mbedTLS SHA-256 (already
     linked for TLS).

   Worked values. The serialization is shown so a reader can check the hash by eye.

   ```
   ab-4m-v1  (agent/partitions.csv)
   1:2:36864:24576
   1:0:61440:8192
   1:1:69632:4096
   1:64:73728:4096
   0:16:131072:1966080
   0:17:2097152:1966080
   sha256 = 1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed

   ab-4m-arduino-v1  (the table in design/decisions/arduino-gets-its-own-layout-id.md)
   1:2:36864:20480
   1:0:57344:8192
   0:16:65536:1966080
   0:17:2031616:1966080
   1:64:3997696:4096
   1:3:4128768:65536
   sha256 = 05528998ae17fb6a7a5741443f9a7a4720c766f370fefc30814cbc3e391c1fc4
   ```

   The `ab-4m-v1` value is computed from the checked-in `agent/partitions.csv`. No
   `partitions.csv` for `ab-4m-arduino-v1` is checked in yet (R3 ships it), so that value
   is computed from the ADR's table (nvs `0x9000`/`0x5000`, otadata `0xe000`/`0x2000`,
   ota_0 `0x10000`, ota_1 `0x1F0000`, both `0x1E0000`, ff_cfg `0x3D0000`/`0x1000`, coredump
   `0x3F0000`/`0x10000`) and must be re-derived from the real CSV when it lands.

   Reproduction recipe for `ab-4m-v1` (a recipe, not a committed script; for another CSV,
   extend `T`/`S` for the types and subtypes it uses):

   ```bash
   cd fleetforge && python3 - <<'EOF'
   import hashlib
   T={"app":0,"data":1}
   S={"app":{"factory":0,"test":0x20,**{f"ota_{i}":0x10+i for i in range(16)}},
      "data":{"ota":0,"phy":1,"nvs":2,"coredump":3,"nvs_keys":4,"efuse":5,"undefined":6,
              "esphttpd":0x80,"fat":0x81,"spiffs":0x82,"littlefs":0x83}}
   rows=[]
   for line in open("agent/partitions.csv"):
       line=line.split("#")[0].strip()
       if not line: continue
       f=[x.strip() for x in line.split(",")]
       rows.append((T[f[1]], int(f[2],0) if f[2].startswith("0x") else S[f[1]][f[2]], int(f[3],0), int(f[4],0)))
   c="".join(f"{t}:{s}:{o}:{z}\n" for t,s,o,z in sorted(rows,key=lambda r:r[2]))
   print(hashlib.sha256(c.encode()).hexdigest())
   EOF
   # -> 1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed
   ```

3. **`flash_chip_size`: integer bytes, the physical chip size detected at runtime.** Not the
   image header's configured size. Not `flash_size`: the bundle manifest already uses that
   name for the image header's configured size (`"4MB"`, `firmware/manifest.py`), the claim
   this field must not be (renamed by R2b-spec-2). `esp_flash_get_physical_size(esp_flash_default_chip,
   &size)` exists in v5.5.5 (`spi_flash/include/esp_flash.h`), and its header says
   `esp_flash_get_size()` returns "the size in the binary image header". **If the physical
   call fails, omit the field.** Do not fall back to `esp_flash_get_size()`: that value is
   the header's claim, which would put the claim-vs-measurement mistake back into the one
   field meant to be a measurement. (This corrects the planning note, which proposed the
   fallback.) The value: a layout that ends past the physical chip is a board that cannot
   work, and it is step 2's (R3) input for a `detected` profile.

**Size budget.** Today's announce is about 350 B. The three fields add about 142 B
(`"flash_chip_size":4194304,` 26 B; `"partition_table_sha256":"<64 hex>",` 93 B;
`"rollback_capable":true` 23 B), so about 490 B, under esp-mqtt's default 1024 B buffer
(`agent/main` sets no MQTT buffer size of its own). With R2b-spec-1's `ssid` and
`known_networks` as well, about 590 B.

**Enroll gotcha: store, never reject.** `EnrollRequest` is validated after the token is
read, and its docstring explains why a validation failure must never cost a token. A 400
there would burn a single-use token because an optional measurement was malformed. So on
`/v1/enroll` and in the ingestor, a malformed new field is stored as null and logged, never
refused. The deploy gate is where the fields take effect, and nowhere else. "Malformed"
means: `flash_chip_size` not a positive integer; `partition_table_sha256` not 64 lowercase hex
characters; `rollback_capable` not a boolean.

**Server semantics (policy, not protocol).** These settle the open question "the precedence
rule when claim and measurement disagree".

- **Refuse-and-flag, not adopt-and-warn, for the fingerprint in step 1.** Adopting the
  measurement needs a fingerprint-to-layout mapping, which is step 2's profile table. Step
  1 has only the name, so the honest move is a refusal, code `partition_table_mismatch`, a
  409 in the existing name-what-did-not-match style, for example: "this device announces
  partition layout ab-4m-v1 but its partition table fingerprint is X, not the Y that
  ab-4m-v1 has. The device disagrees with its profile." It is a partition-layout mismatch,
  which `spec/flows.md` refuses, so it is never overridable.
- **`rollback_capable: false` is a gating warning, code `rollback_incapable`** (amended by
  R2b-spec-2; the filed text made it a 409 with no override). The pre-check reports it as
  a warning naming that this board's bootloader cannot roll back. `POST
  /v1/devices/{id}/deploy` answers 409 with the same sentence unless the body overrides
  it: `DeployRequest.override: list[Literal["rollback_incapable"]] = []`. Per code, never a
  blanket `force` (the `DeployRequest` docstring's "no `force`" stays true); an unknown code
  is a 422; listing a code whose warning is not raised is a no-op; refusals can never be
  overridden. `PrecheckFinding` gains `needs_override: bool = False` so the card knows which
  warnings gate; `never_connected`, `offline` and `sleepy` stay non-gating. The R3
  library-marker warning will be the second gating code. `null` or absent never warns and
  is never refused ("an R0 board that announced neither is not refused for being old"),
  because that is every board before its first OTA and a warning on every board teaches
  the operator to ignore warnings; the dashboard may say "rollback unverified".
- `partition_table_sha256` absent: no fingerprint check (the status quo). Present for a
  layout with no known fingerprint: no check, logged.
- `SUPPORTED_LAYOUTS` becomes `{layout: {ota_slot_size, partition_table_sha256}}`, still in
  code (step 1 item 4). The `ab-4m-v1` hash is pinned against `agent/partitions.csv` by
  `tests/test_agent_partitions.py` with a retyped literal (that file's own convention).
- Whether a `detected` profile is per-fleet or global stays **open for step 2 (R3)**.
  Status (R3-be-2): settled, **global** (DECISIONS 2026-10-09 R3-be-2 D1).

**Considered and deferred.**

- **Per-slot geometry (`ota_slots` / a full partition list).** Step 1 above lists "slot
  count, each slot's offset and size". Deferred: the fingerprint already covers geometry
  for the step-1 comparison, and only step 2's "adopt an unknown table as a `detected`
  profile" needs the list. Adding it then is additive, the agent is OTA-updatable, and a
  nested list strains the flat / CBOR drop-in rule.
- **`bootloader_sha256`.** The only attestation available before the first OTA. The server
  knows the digest of the bootloader it ships in the agent bundle, and the Arduino core's
  bootloader per core version could be catalogued. It would also be the sound invalidation
  key for the persisted `rollback_capable`. Deferred because esptool rewrites the image
  header's flash mode, frequency and size bytes at write time (and the appended digest with
  them) unless `--flash_mode keep` and its siblings are used. The on-flash digest therefore
  need not equal the bundle file's sha256, and that needs a bench measurement before
  anything goes on the wire. The candidate R3 follow-up.
- **A compiled-in `rollback_capable` claim.** Rejected, see field 1.

**Paste-ready spec text.** Everything below is for `spec/device-protocol.md` and
`spec/open-questions.md`, to be applied in two patches (see *Application order* after (d)):
**Patch A** is (b), (c) and (d); **Patch B** is (a).

*(a) `up/announce` example.* This is **Patch B**: apply it only in `R2b-fw-2`'s commit (see
*Application order*). Replace the JSON block. The three new keys follow
`ota_slot_size`; the existing keys keep their order, and `proto` stays `1`. The
substrings `"ota_slot_size": 1966080` and `"partition_layout": "ab-4m-v1"` must stay
byte-for-byte (one space after the colon, same quoting), because
`tests/test_agent_partitions.py::test_ota_slot_size_is_the_number_in_the_protocol_spec`
matches them as text.

```json
{
  "proto": 1,
  "device_id": "a4cf12b3de90",
  "platform_type": "esp32c6",
  "fw_version": "1.4.2",
  "agent_version": "0.3.2",
  "link_type": "wifi",
  "power_class": "always_on",
  "expected_wake_interval_s": null,
  "parent_device_id": null,
  "partition_layout": "ab-4m-v1",
  "ota_slot_size": 1966080,
  "flash_chip_size": 4194304,
  "partition_table_sha256": "1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed",
  "rollback_capable": true,
  "capabilities": ["ota", "selftest", "identify"]
}
```

*(b) A paragraph under the existing `partition_layout` + `ota_slot_size` paragraph (Patch A):*

> `flash_chip_size`, `partition_table_sha256` and `rollback_capable` are measurements, where
> `partition_layout` is a name. `flash_chip_size` is the physical flash chip size in bytes, not
> the size in the image header; a device that cannot read it omits the field.
> `partition_table_sha256` is the lowercase hex SHA-256 of the device's decoded partition
> table: one line `{type}:{subtype}:{offset}:{size}\n` per entry the table contains,
> all four in decimal, sorted by offset ascending, with labels and flags left out. The
> expected value for each layout id is in the *Partition layouts* table below.
> `rollback_capable` is `true` once the board has booted an OTA-written image that its
> bootloader put into `PENDING_VERIFY`, `false` once it has booted one that its bootloader
> never put into `PENDING_VERIFY` (the bootloader cannot roll back), and `null` until either
> has been observed, which includes every board that has never completed an OTA. It is
> never derived from how the firmware was built. All
> three are additive under *Evolution rules*, rule 2, and `proto` stays `1`. An absent
> field means unknown, and the server never refuses a device for omitting one. The enroll
> body carries the same fields, and the server stores a malformed value as null rather
> than refusing the request.

*(c) A new column for the *Partition layouts* table (Patch A),* `partition_table_sha256`:
`ab-4m-v1` gets `1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed`, and
`ab-4m-arduino-v1` gets `05528998ae17fb6a7a5741443f9a7a4720c766f370fefc30814cbc3e391c1fc4`
(re-derive the latter from the real CSV when R3 checks it in). A new layout id adds its
own value; an existing row's value is never edited.

*(d) `spec/open-questions.md` (Patch A).* Delete the entry *Bootloader attestation on the
Arduino path* once (b) and (c) are applied, per that file's rule ("Answering one means moving it
into spec/ proper and deleting it here"). Refile the residue as a narrower question:

> **Attestation before the first OTA.** `rollback_capable` is measured by an OTA, so a
> board's first OTA runs unprotected if its bootloader lacks rollback, and nothing on the
> wire says so beforehand. Candidate: a `bootloader_sha256` in `up/announce`, compared
> with a catalogue of known-good bootloader digests (the agent bundle's, and the Arduino
> core's per core version). Blocked on a bench measurement of whether the on-flash digest
> equals the bundle file's, since esptool rewrites the image header's flash parameters at
> write time. The same digest would invalidate a stale persisted `rollback_capable` after
> an Arduino IDE upload.

**Application order (R2b-spec-2).** Patch A ((b), (c), (d)) can be applied any time after
the owner accepts the proposal. Patch B ((a), the three new keys in the `up/announce`
example) goes in **only in `R2b-fw-2`'s commit**:
`tests/test_ff_cfg.py::TestAnnounceMatchesTheSpec::test_the_firmware_builds_exactly_the_spec_keys`
requires `agent/main/ff_identity.c` to contain every key in that example, so applying
Patch B before the agent emits the keys turns `just test` red. In (a),
`"ota_slot_size": 1966080` and `"partition_layout": "ab-4m-v1"` stay byte-for-byte. Patch
B also coexists with R2b-spec-1's Patch B (`ssid`, `known_networks`, in
`docs/features/enrollment.md`); whichever lands second rebases on the other. Combined, the
announce is about 590 B, under esp-mqtt's 1024 B default buffer.

**Follow-ups once the spec change is applied (filed in `TODO.md`, blocked on the owner).**

- **agent (`R2b-fw-2`):** emit the three fields from `announce_object()` in the spec's key
  order; persist the `rollback_capable` observation in NVS from `classify_txn()` (a
  CRITICAL confirm/rollback path), `true` from the `TXN_CONFIRMING` branch; and give the
  `NEW`-at-target case a terminal outcome instead of "stale, discarded", which leaves the
  deploy parked at `rebooting`. Whether that is a new status or an existing one is a
  question, since the status vocabulary is open (DECISIONS 2026-10-02). Emits `false` only
  after `R2b-test-5`. Applies Patch B in the same commit.
- **server, ingest (`R2b-be-6`):** `AnnouncePayload`, `store.py`, `EnrollRequest` (store,
  never reject), `IDENTITY_FIELDS`, `Device` columns plus an Alembic migration (CRITICAL),
  and simulator flags `--rollback-capable {true,false}`, `--partition-sha`,
  `--flash-chip-size`.
- **server, gate (`R2b-be-7`):** in `deploy_precheck.py`, the `partition_table_mismatch`
  refusal and the `rollback_incapable` gating warning; `DeployRequest.override`;
  `PrecheckFinding.needs_override`; the `SUPPORTED_LAYOUTS` profile dict
  `{layout: {ota_slot_size, partition_table_sha256}}` with the `ab-4m-v1` fingerprint
  pinned in `tests/test_agent_partitions.py`. **LANDED 2026-10-05**; see
  [ota-deploy.md](ota-deploy.md) → *R2b-be-7*.
- **test (`R2b-test-5`):** a bench run of a rollback-less bootloader to settle the `false`
  signal (hardware-gated). The QEMU proof that an OTA'd image reports
  `rollback_capable: true` is part of `R2b-fw-2`.

**Step 2 — R3, and not before.** Move the catalog into the table, seed the builtins, allow
user-defined profiles via API/UI. The trigger is evidence, not the calendar: R3 produces
the second and third real layouts (`ab-4m-arduino-v1` already exists,
[`design/decisions/arduino-gets-its-own-layout-id.md`](../../design/decisions/arduino-gets-its-own-layout-id.md)),
and a maker with a stock `min_spiffs` table is the first user who cannot be served by a
code-resident dict. Building the table before that evidence is speculative schema.

Status (R3-fe-1, 2026-10-09): **built** (API in R3-be-2, dashboard in R3-fe-1). See
*Completed Work → R3-be-2* and *R3-fe-1*.

**Why the bootloader field matters, and why it cannot wait for a redesign**. Rollback exists in code jointly by the second-stage bootloader and the app, and **Fleetforge OTA
replaces the app, never the bootloader**. Per the ESP-IDF 6.2 migration notes, a device
whose bootloader predates rollback support never transitions
`ESP_OTA_IMG_NEW → ESP_OTA_IMG_PENDING_VERIFY`. The app detects this at startup and marks
itself valid to keep the OTA state consistent, *without* any rollback protection. Such a
board can never gain the safety net from us at any version, and it reports identically to a
protected one — image confirms, announce lands, dashboard green. The three failure modes
enumerated in [`TODO.md`](../../TODO.md) all assume the net is there: mode 1 (*image fails
to boot*) is delegated to the bootloader, and mode 2 (*boots, joins, never confirms*) is
the one we proved on metal. On such a board **both** silently degrade to mode 3's
no-automatic-recovery, and nothing distinguishes it. `esp_ota_get_state_partition()`
returning `ESP_OTA_IMG_NEW` where `PENDING_VERIFY` was expected is the detectable signal.

**Parameters to check**. Note the pattern: the geometry checks that matter are already
written — they just run against the *bundle we build*, at build time, and have no
counterpart for a device announcing itself.

| Parameter | Bundle (build/publish) | Device (announce/deploy) |
|---|---|---|
| chip versus artifact target | — | ✅ `deploys.py` — the device's `platform_type`, never a request field |
| slot size ≥ artifact size | ✅ `bundledir.py:229` | ✅ `_check_compatible()` |
| layout name equality | ✅ `catalog.py:194` manifest versus index | ✅ `_check_compatible()` device versus artifact |
| `ota_slot_size` matches claimed layout | ✅ `bundledir.py:186`, `catalog.py:205` | ❌ — no per-layout expectation is consulted |
| no `factory` partition | ✅ `make_manifest.py:238` | ❌ — `agent_main.c:128` only *logs* it |
| ota_0 and ota_1 both present, equal size, contiguous | ✅ `make_manifest.py:243-253` | ❌ |
| `ff_cfg` present | ✅ `make_manifest.py::config_partition` | ❌ |
| flash chip size | — | ❌ new |
| bootloader rollback-capable | ✅ `verify_bundle.py` asserts the Kconfig | ❌ new — and not implied by the bundle's config, see below |
| partition-table sha256 versus profile | — | ❌ new — the whole point of the fingerprint |

Every ❌ in the right column becomes a ✅ by transporting the same rule to `up/announce`,
which is why step 1 is small. The predicates exist and are tested, they need a second
caller and a wire field to read.

**Open, to settle when step 1 is written:** whether a `detected` profile is per-fleet or
global (for step 2, R3). Status (R3-be-2): **global** (DECISIONS 2026-10-09 R3-be-2
D1). The precedence rule when claim and measurement disagree is now
proposed as refuse-and-flag; see *Step 1 wire proposal (R2-spec-1)* above. The choice of
this design over the two alternatives gets a `DECISIONS.md` entry when step 1 lands, not
before — it is a plan until something is built. The R2-spec-1 entry covers the wire
proposal only. The `rollback_capable: false` half of refuse-and-flag was amended to a
gating warning with an override (R2b-spec-2); the fingerprint half stands.

**Out of scope:** a board *name* database. All three neighbors converge on chip variant +
flash geometry as the only schema that carries weight — ESPHome's 298-entry esp32 board
list holds `{name, variant}` and is not consulted for partitioning at all. A Fleetforge
board list would be 300 rows of cosmetics wrapped around two fields we already have.
