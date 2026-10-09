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

### R3-rel-1 (2026-10-09): library README quickstart, R3 archived, v0.5.0 prepared locally (agent/library 0.5.0); not deployed

Nothing was published or deployed: no push, no tag push, no `just deploy`, no `agent-publish*`.

**What shipped.**
- `agent/components/fleetforge/README.md` (new): what the firmware does, a toolchain table that links `examples/Basic/README.md`, its *Arduino IDE* section and `examples/basic_idf/README.md`, the pinned git form, requirements, flash layouts, the marker, public headers, what is not yet. It holds no `# quickstart:` block; the scripted blocks stay only in the two example READMEs.
- `examples/Basic/README.md`: link to the library README, a "Without a clone" pinned `lib_deps` block under *Your own project*, and the zip name `Fleetforge-0.5.0.zip`. `examples/basic_idf/README.md`: a link to the library README. No quickstart block changed.
- Repo `README.md`: a factual "Where this stands", a section pointing at the library README, and a *Where things live* row.
- `tests/test_worked_example.py::TestTheLibraryReadme`: links resolve, both example READMEs linked both ways, no quickstart block, the zip name equals `agent/version.txt`.
- Versions: agent and library `0.4.7 -> 0.5.0` (`version.txt`, `library.json`, `ff_lib_version.h`); app `0.4.3 -> 0.5.0` in the release commit.

**Pinned form.** `lib_deps = Fleetforge=https://github.com/tvaroska/fleetforge.git#v0.5.0` is true only once the owner pushes `main` and the tag. The local proof is the same line with `git+file://<repo>#v0.5.0`.

**Release notes v0.5.0.**
- Agent and library 0.5.0: every 0.5.0 agent and library build announces `lib_marker: 1` and carries the marker; the released 0.4.7 (`v0.4.3`) announces none (never warned).
- Migration `0007` (`partition_profiles`): restart `fleetforge-ingestor` after the api rollout; its retained replay records boards on unknown maps. The downgrade drops operator profiles and adoptions.
- Migration `0008` (`artifacts.has_lib_marker`): artifacts uploaded before it have no verdict and never warn until re-uploaded (the re-upload fills it). The downgrade drops the verdicts.
- New gating code `no_library_marker` (override by name).
- Named gaps carried from R3: the duplicated deploy command and the 2 s criterion have no harness; the IDF flavour is compile-only; QEMU boots the hybrid build; no registries.

**Owner checklist, in order (NOT done by this task).**
1. `git push origin main --tags` (makes Basic step 0's `git clone` and the `#v0.5.0` pin true).
2. Publish agent bundles BEFORE deploying (S0-infra-10): `just agent-check-fresh`, the typed GCS `just agent-publish-all` from the justfile comment, then `just agent-check-prod`.
3. `/release` steps 7-12 (`just build`, digests into `services/prod/docker-compose.yml`, `just deploy --yes --service fleetforge`, ops-log promotions).
4. Attach `dist/arduino/Fleetforge-0.5.0.zip` to the GitHub release.

### R3-test-2 (2026-10-09): CUJ-1 played end to end on the library path; deterministic judge PASS on steps 1-2, 3, 5, 6

No code changed. Evidence kept outside the repo in `/tmp/ff-r3-test-2` (logs, scorecard, jeep output).

**What was run.**
- `uv run pytest tests/test_enroll.py -q`: 42 passed.
- api origins swapped to 10.0.2.2 (QEMU slirp), then `just lib-quickstart`: exit 0, `== PASS (1833 s) ==`, Phases 1, 2a, 2b, 2c, no `SKIP  ESP-IDF`. The api was restored to localhost origins afterwards.
- `just update-e2e http://localhost:8088 /tmp/ff-r3-test-2/update-e2e`: exit 0, 7/7 pass (upload-good, upload-merged, precheck-wrong-layout, deploy-good, deploy-broken, adopt-detected-profile, traps). `elapsed_s` good 3.4, broken 26.3, adopted 3.3. The two harnesses ran sequentially because their origins conflict.
- Credential sweep over the evidence dir (`ff[ea]_...`, `"mqtt_password":"`): no file matched.

**Run values.** VB `1.1.0-qs1791559092` (cmd_id `07abea33fe3244bebb5dfdef63ac3ccd`, staged ota_1); VR `1.2.0-qs1791559092-rbtest` (cmd_id `f3ebec807da24af1b947db9e07feab69`, staged ota_0, rolled_back to ota_1).

**Scorecard.**

| Assertion | Verdict | Evidence |
|---|---|---|
| Steps 1-2: sketch compiles with the library | PASS | `own project (lib_deps -> the clone) esp32, esp32s3 SUCCESS`; `Arduino B differs from A yes`; both IDF builds `app version 1.0.0` |
| Step 3: row online | PASS | `online True, fw_version 1.0.0, partition_layout ab-4m-arduino-v1, capabilities ['ota'], agent_version 0.4.7` |
| Step 5: fw_version == uploaded build | PASS | `morse: HELLO (firmware 1.1.0-qs1791559092)`; `after the update: fw_version 1.1.0-qs1791559092, deploy confirmed (is_terminal True), 98 s after the deploy`; update-e2e upload-good, deploy-good |
| Step 6: rolled_back, previous fw_version | PASS | `deploy rolled_back (is_terminal True), fw_version 1.1.0-qs1791559092 (the previous build) ... no confirmed`; update-e2e deploy-broken |
| Traps: credentials, physical retrieval, wrong layout (server+dashboard half), stale milestone | PASS | `token 0 occurrences, mqtt_password 0 (19 files)`; step 6; PASS precheck-wrong-layout; PASS deploy-broken |
| Deploy within 5 min | PASS | 98 s (device), 3.4 s (simulated) |

**Listed, not scored.**
- The duplicated deploy command assertion: no harness (R3-spec-2 named gap 1).
- Dashboard reflects a change within 2 s: no harness measures it.
- Trap "raw UART log / ESP-IDF / this repo required": LLM rubric only.
- Wrong-layout device half: a procedure.
- Step 4 (physical install) and the unaided half of step 3: a person.
- The ESP-IDF flavour is compile-only; QEMU boots the hybrid `lib-qemu/` build; the reset after the confirm timer is a power cycle.

**jeep (recorded, not gating).** exit 0. PASS on step 3 row, step 5 fw_version, step 6 rolled_back, error text with next action, docs-equals-steps, readable sketch (63 lines), no source knowledge needed. NOT ASSESSABLE on the duplicated deploy command. No hard-fail trap named.

**Follow-ups proposed, not filed.** Add a dedupe re-publish of the same `dn/cmd` to `phase_ota` in lib-quickstart so the duplicated deploy command assertion can be scored; a harness measurement for the 2 s criterion.

**Judge commands (for /replan).** `just lib-quickstart` (with api on 10.0.2.2 origins), `just update-e2e`, `uv run pytest tests/test_enroll.py`, then the acceptance greps against the tee'd logs and `jeep` over `spec/cujs.md` CUJ-1.

### R3-be-1 (2026-10-09): an upload stores whether the library marker is in the image; a deploy of an unmarked build is gated as no_library_marker

**The problem.** From R3-fw-6 every library build carries the marker, but the server never
looked for it, so a plain sketch (no library, or the library installed and never started)
deployed like any other build and orphaned the board: nothing in it confirms the update or
listens for the next one. The decisions it implements were named in advance in the
*Library marker proposal* below ("Pre-check (`R3-be-1`), named here so it is not
re-decided"); this entry records how they shipped.

**What shipped.**

- **`src/fleetforge/lib_marker.py` (new).** `find_marker(data) -> LibMarker | None`, the
  spec's reading rule verbatim (pure, stdlib, never raises); `MAGIC`, `MARKER_SIZE`.
- **Migration `0008` + `Artifact.has_lib_marker` (`BOOLEAN NULL`, no default, no CHECK).**
  NULL = never scanned (every row from before R3-be-1; never warns), `false` gates, `true`
  is marked. The one server-computed column on `artifacts`; not `provenance`.
- **`POST /v1/artifact`** scans every accepted upload after the merged-image refusal and
  stores the verdict; the response gains `has_lib_marker`; the log line names the
  marker's format, version and offset or "no library marker". Never a refusal. The upsert
  is `ON CONFLICT (sha256) DO UPDATE SET has_lib_marker = EXCLUDED.has_lib_marker WHERE
  artifacts.has_lib_marker IS NULL`: a re-upload fills a NULL verdict and never changes a
  known one, and every other column keeps its first writer's value.
- **`deploy_precheck.py`.** `NO_LIBRARY_MARKER`; `GATING_CODES = (ROLLBACK_INCAPABLE,
  NO_LIBRARY_MARKER)`; `ResolvedArtifact.has_lib_marker` (no default);
  `warnings`/`gating_warnings`/`unmet_gates` take a required `artifact=` keyword. The
  finding is raised only when `artifact.has_lib_marker is False`, after
  `rollback_incapable`, with a backtick-free sentence naming the consequence and the fix
  (`Fleetforge.begin()` / `fleetforge_start()`).
- **`deploys.py`** selects `a.has_lib_marker` and passes the artifact to the gate and the
  warnings. **`schemas.py`**: `OverrideCode` is `"rollback_incapable" | "no_library_marker"`.
  No frontend code change: `deployPrecheck.ts` renders any `needs_override` code generically.
- **`frontend/scripts/update-flow-e2e.mjs`**: the synthetic builds end with one valid marker
  (`LIB_MARKER`, `lib_version` `0.0.0-e2e`), since they stand for library builds.
- **Fixtures** (real bytes, R3-fw-6 builds): `esp32.basic.app.head.bin` (Arduino Basic, first
  128 KiB), `esp32.agent.app.head.bin` (agent `app.bin`, first 128 KiB),
  `esp32.nocall.app.bin` (Basic with `Fleetforge.begin` deleted, whole file, no marker).
- **Tests.** `test_lib_marker_reader.py` (new: magic == spec == firmware, the real fixtures,
  every malformed case); `test_deploy_precheck.py::TestNoLibraryMarker`;
  `test_api_artifact_upload.py::TestTheLibraryMarkerVerdict` (verdict stored, NULL backfill,
  a known verdict never rewritten); `test_api_deploy.py::TestTheLibraryMarkerGate` (the
  acceptance end to end through upload, pre-check and deploy); `test_schema.py` (the column).

**T2 (2026-10-09, dev stack at `localhost:8088`, no board).**

| Step | Result |
|---|---|
| Migration | `just migrate`: `0007 -> 0008`; `\d artifacts` shows `has_lib_marker | boolean`, nullable; the 90 existing rows stay NULL |
| T2-A reader on whole binaries | agent `app.bin` ×4 and Basic esp32/esp32s3: `LibMarker(format=1, lib_version='0.4.7')`; `esp32.nocall.app.bin` and the stock Arduino `StartCounter.ino.bin`: `None` |
| T2-B live (simulated esp32 board online) | upload StartCounter / Basic / agent: 201, `has_lib_marker` false / true / true. Pre-check: plain `[("no_library_marker", true)]`, lib and agent `[]`. Deploy plain: 409 with the pre-check sentence; `override: ["force"]` 422; `override: ["no_library_marker"]` 202. Deploy lib without override: 202. SQL: f / t / t |
| T2-C `just update-e2e` | exit 0, 6/6 PASS; its three uploads stored `has_lib_marker = true` |
| T1 | `just test` 1745 passed (ruff, mypy, full suite on Postgres, incl. `test_models_match_migration` and `test_migration_downgrades_cleanly`); `node --check update-flow-e2e.mjs` 0 |

**Named gaps.**

- **Artifacts uploaded before R3-be-1 have no verdict and never warn** until the same bytes
  are uploaded again (which fills it). The migration cannot backfill: it has no object store.
- **The marker proves the library is linked, not started** (spec). A sketch whose code
  references the library on a path that never runs is marked and not warned.
- **The downgrade drops every stored verdict**; they come back only by re-upload, and NULL
  is fail-open.
- **The device's announced `lib_marker` is still not stored** (DECISIONS R3-be-1 D6).

### R3-fw-6 (2026-10-09): the library marker is in the component, linked by the announce; `up/announce` ends with `lib_marker`; spec Patch B

**The problem.** R3-spec-3 decided the marker (one 64-byte constant, magic
`14a948d18f12cfdd46464f54414c4942`) and Patch A put it in `spec/device-protocol.md`, but no
image carried it, so R3-be-1's pre-check had nothing to find. The spec also requires that
installing the library without calling it does not mark an image, which rules out every
way of force-keeping the object.

**What shipped.**

- **`src/ff_marker.c` + `src/ff_marker.h` (new, private).** `const ff_lib_marker_t
  ff_lib_marker __attribute__((aligned(4)))`, alone in its TU: the 16 magic bytes (spelled as
  literals so a test compares them to the spec), `format = FF_LIB_MARKER_FORMAT` (1),
  `lib_version = FF_LIB_VERSION`, reserved bytes zero by designated initializers.
  `_Static_assert`s pin size 64 and offsets 16/20/52, and that the version string is
  non-empty and fits with its NUL. No `used`, `retain`, `KEEP()` or `-u`.
- **What keeps it.** `ff_identity.c` reads it twice: `announce_object` adds
  `cJSON_AddNumberToObject(root, "lib_marker", ff_lib_marker.format)` as the last key (after
  `capabilities`), and `ff_identity_init` logs `image: fleetforge library %s, lib_marker %u`
  with `ff_lib_marker.lib_version` (the address escape). The line sits **before**
  `device_id %s`: `frontend/src/diagnostics.ts` reads the device id from the last `ff-id`
  line's first 12-hex run, and this line has none.
- **`lib_version` is `FF_LIB_VERSION` in every build** (IDF agent, a maker's IDF project,
  Arduino). `ff_lib_version.h`'s comment now says so; it is still pinned to
  `agent/version.txt` and `library.json`. Not `PROJECT_VER` (in `basic_idf` it is the
  maker's `1.0.0`), not CMake's `version.txt` (a copied component has none).
- **CMake `SRCS`** gains `src/ff_marker.c`. `library.json` and `scripts/arduino_package.py`
  are unchanged (no srcFilter; the package globs `src/`).
- **Spec Patch B.** The `up/announce` example's `capabilities` line gains a comma and is
  followed by `"lib_marker": 1`. The only `spec/` edit.
- **Comments.** `ff_identity.c` and `agent/tools/lib_bundle.py` say `BUILTIN_LAYOUTS`
  (R3-be-2 rename).
- **Tests.** `tests/test_library_marker.py` (new): magic == spec table and ends `FFOTALIB`;
  layout asserts, field order and sizes, `aligned(4)`; format 1 and `FF_LIB_VERSION`, no
  `PROJECT_VER`; no force-keep spelling in the component, `library.json` or the package
  script; one definition, declared only in `src/ff_marker.h`, absent from `include/`,
  `agent_main.c` and `Fleetforge.cpp`; `lib_marker` is the last `cJSON_Add` before `return
  root;`; the spec example parses with `lib_marker` last; the `image:` line precedes
  `device_id` with no hex run; the ingestor decodes an announce with `lib_marker` 1, `"x"`
  and absent and keeps `fw_version`. `ff_marker.h` joins `PRIVATE_WHOLE_HEADERS`. Size
  budgets raised for all four targets.

**T2 (2026-10-09, no board; the reference reader of the proposal at `/tmp/ff-scan/scan.py`).**

| Step | Result |
|---|---|
| T2-A pre-R3 announce none | `git grep -n -e lib_marker -e FFOTALIB -e ff_lib_marker v0.4.3 -- agent` rc=1. The pre-rebuild 0.4.7 bundles (×4) and `tests/fixtures/firmware/*.bin` (×4): `no marker (magic x0)` |
| T2-B stock agent | `agent/dist/{esp32,esp32s3,esp32c3,esp32c6}/app.bin`: `marker format=1 lib_version=0.4.7 (magic x1)` ×4 |
| T2-C Arduino | `examples/Basic/.pio/build/{esp32,esp32s3}/firmware.bin`, `lib-qemu/.pio/bundle/esp32-qemu/app.bin`: `marker format=1 lib_version=0.4.7 (magic x1)` ×3 |
| T2-D ESP-IDF consumer | `basic_idf` built for esp32 in the pinned image: `marker format=1 lib_version=0.4.7 (magic x1)`; its `PROJECT_VER` is `1.0.0` |
| T2-E installed, not called | Basic with `Fleetforge.begin` deleted (grep 0): `pio run` SUCCESS, `ff_marker.c.o` compiled, image `no marker (magic x0)` |
| T2-F QEMU example | `I (7464) ff-id: image: fleetforge library 0.4.7, lib_marker 1` before `ff-id: device_id`; `enroll 200`; `announce acknowledged`. Last `up/announce`: `lib_marker` 1, last key, `ab-4m-arduino-v1`, `fw_version` 1.0.0. API: `000000000000` online, 1.0.0 |
| T2-G stock agent in QEMU | Same lines; announce `lib_marker` 1 (last key), `ab-4m-v1`, `agent_version` 0.4.7; `just agent-qemu-smoke esp32` HARNESS OK |
| T2-H server tolerance | `test_library_marker.py`, `TestAnnounceMatchesTheSpec`, `test_unknown_body_fields_are_ignored`: 22 passed |
| Builds | `just agent-build-all` BUNDLE OK ×4, 0 `warning:`; esp32 1,030,512 B (+192), s3 +224, c3 +224, c6 +224. `just lib-build esp32`/`esp32s3` SUCCESS; `just lib-bundle esp32-qemu` exit 0; `just lib-quickstart --build-only` PASS (481 s); `just test` green |

**Named gaps.**

- **LTO is unmeasured.** No build here uses `-flto`; the `image:` line's `%s` is the
  address escape that should survive it.
- **The marker proves the library is linked, not started.** T2-E covers only "installed,
  not referenced".
- **Dev-built 0.4.7 agents announce `lib_marker`.** "Agents ≤ 0.4.7 announce none" holds
  for the released 0.4.7 (`v0.4.3`) and becomes unambiguous at the R3-rel-1 bump.
- **The server ignores the key.** Storing it and the `no_library_marker` gate are R3-be-1.
- **The Arduino IDE package** (`just lib-arduino-check`, ~8 GB) was not re-run; the new `.c`
  goes through the generic glob that `tests/test_arduino_package.py` covers.

### R3-fw-8 (2026-10-09): the Arduino IDE package — generated, zip-installable, compiles the example on core 3.3.12 for esp32 and esp32s3

**What.** `scripts/arduino_package.py` (`just lib-arduino-package`) generates
`dist/arduino/Fleetforge/` + `dist/arduino/Fleetforge-0.4.7.zip` from the component: a flat
`src/` (the 8 `include/` headers + the 23 `src/` files, same basenames, a collision is an
error), `library.properties` generated from library.json (`includes=Fleetforge.h`,
`architectures=esp32`, never `dot_a_linkage`/`precompiled`), and `examples/Basic/` with
`Basic.ino` and `partitions.csv` byte-identical. Every `.c`/`.cpp` gains a prelude carrying
library.json's `-DLOG_LOCAL_LEVEL=ESP_LOG_INFO` (the IDE has no per-library flags) and a
bare `#line 1`; every other byte is the source's. The zip has one root `Fleetforge/` and is
byte-reproducible. `scripts/arduino_ide_check.py` (`just lib-arduino-check`) is the proof
harness: pinned arduino-cli 1.5.1 (sha256-checked) and `esp32:esp32@3.3.12` in
`/tmp/ff-arduino-ide`, `lib install --zip-path`, the INSTALLED example copied to a fresh
sketch folder and compiled with the default board menu, toolchain deleted at the end.
`tests/test_arduino_package.py` pins the package (27 tests, no toolchain). The example
README gains an *Arduino IDE* section (no quickstart block). Decisions: DECISIONS.md
2026-10-09 (R3-fw-8).

**T2 (`just lib-arduino-check`, from an empty `/tmp/ff-arduino-ide`, PASS in 233 s; core
install 148 s; `/` 16 GB free before, 14 GB with the toolchain, 16 GB after teardown).**

| Check | esp32:esp32:esp32 | esp32:esp32:esp32s3 |
|---|---|---|
| zip install → `lib list` | Fleetforge 0.4.7 in `<tc>/user/libraries` | (same install) |
| compile of the copied example | exit 0, 35 s | exit 0, 36 s |
| warnings from Fleetforge / sketch (`--warnings all` = `-Wall -Wextra`) | 0 | 0 |
| Used library | Fleetforge 0.4.7 at the installed path | same |
| `Basic.ino.bin` (slot 1966080 B) | 1165520 B (59.3%) | 1156304 B (58.8%) |
| built `partitions.bin` fingerprint | `05528998…1fc4` = ab-4m-arduino-v1 | same |
| `flash_args` | app @ 0x10000, boot_app0 @ 0xe000 | same |
| `nm` | `40186ac8 T verifyRollbackLater` | `420b6b08 T verifyRollbackLater` |
| ff-lib INFO format string in the app | present | present |
| IDE size line (observation) | `Maximum is 1310720 bytes` (88%) | `Maximum is 1310720 bytes` (88%) |

Negative control (`just lib-arduino-check --keep-toolchain --negative-control`, prelude
stripped from the temp package): every check passes up to (h), which FAILS ("the app has no
'fleetforge library %s, firmware %s'"); the esp32 app shrinks to 1152576 B (12944 B of INFO
strings compiled out). Positive control for the warning gate: an unused variable injected
into a copied sketch is reported in `compile --format json`'s `compiler_err` with the
sketch path. `git diff --stat` shows nothing under the component's `src/`, `include/` or
any `partitions.csv`.

**Plan deviations.** `prelude(manifest)` takes no filename and ends in a bare `#line 1`
(not `#line 1 "<basename>"`): diagnostics keep the installed path, which the warning gate
keys on. `compile --format json`, because arduino-cli 1.5.1 prints the *Used library* table
only with `-v`. The scratch `arduino-cli.yaml` also pins `build_cache.path` under the
toolchain dir (1.x defaults it to `~/.cache/arduino`).

**Named gaps.** Private headers are includable by a sketch (all of `src/` is on the
include path, as with PlatformIO). The IDE's size gate is the board menu's 1310720, not the
slot: past it the IDE refuses a build that fits (workaround: PartitionScheme "Minimal
SPIFFS"; the sketch-local table still wins). `library.properties` cannot pin the core
(3.3.12 is stated in `paragraph` and the README). Not on the Library Manager; the release
zip is R3-rel-1's.

### R3-spec-2 (2026-10-09): CUJ-1 Driver table proposed for R3; spec not applied

Decided: CUJ-1's Driver names the library harnesses. Rows 1–2 = `just lib-quickstart
--build-only`; rows 3, 5 and 6 on the device = one full `just lib-quickstart` run, phases 2a
(enroll), 2b (OTA) and 2c (rollback), named by the script's own headers so `/verify` can
attribute its output; rows 5 and 6 server and dashboard = `just update-e2e` (`upload-good`,
`deploy-good`, `deploy-broken`), and the wrong-layout row = `precheck-wrong-layout`, its
on-device half being a runbook procedure. A new `Graded` column says which segments and
halves are graded. `just agent-qemu`/`agent-qemu-smoke` (interactive / boot-only, stock agent)
and `sim-fleet` + curl (superseded by `update-e2e`) leave the table; the stale bench sentence
goes. Two separable Judge fixes ride along: `rolled-back` → `rolled_back` (the wire state),
and step 5's version source, since an Arduino library build's `fw_version` is the
`Fleetforge.begin()` literal, not `esp_app_desc_t.version`. Optional (d) drops the deleted
`upload-artifact.sh` from *Supported By*. The paste-ready text is in *Planned Work → CUJ-1
Driver proposal*. Named gap: the duplicated-command assertion has no harness (follow-up
suggested, not filed). Blocked on the owner: `R3-test-2`. Nothing is built and `spec/` is
untouched.

**T2 evidence.** Run from the repo root.

| Check | Result |
|---|---|
| `just --show lib-quickstart`; `just --show update-e2e` | both print the recipe, exit 0 |
| `python3 scripts/lib_quickstart.py --help \| grep -- --build-only` | matches (`--build-only  Phase 1 only: no stack, no QEMU`) |
| `grep -c` the three headers `== Phase 2a: enroll`, `== Phase 2b: deploy build B`, `== Phase 2c: deploy a broken build` in `scripts/lib_quickstart.py` | 3 |
| `id: '<s>'` in `frontend/scripts/update-flow-e2e.mjs` for `upload-good`, `deploy-good`, `deploy-broken`, `precheck-wrong-layout` | 4× OK |
| `uv run pytest --collect-only -q tests/test_enroll.py` | 42 tests collected |
| `docs/runbooks/agent-qemu.md` headings | `## The worked example, end to end (R3-fw-4, R3-test-1)` (line 762), `## A wrong flash layout is refused (R3-fw-5)` (line 891) |
| `spec/device-protocol.md` | line 252 `↘ rolling_back → rolled_back` |
| Dry run, `python3 -I /tmp/r3spec2/apply.py <repo>` (stdlib; copies `spec/cujs.md` to `/tmp/r3spec2/`, takes each anchor and replacement from this doc's labelled ```` ```text ````/```` ```markdown ```` fences inside the R3-spec-2 section) | exit 0; anchors found exactly once: (a) 13 lines → 22, (b) 2 → 2, (c) 3 → 4, (d) 3 → 3; one diff hunk (`@@ -79,34 +79,44 @@`) covering only the Supported By bullet, the Driver block and the two Judge bullets |
| On the patched copy: `grep -c` `rolled-back`, `bench`, `upload-artifact.sh` | 0, 0, 0 |
| Driver table cell counts (header, rows) | 4, 4, 4, 4, 4, 4; separator 4 columns |
| `git status --short spec/` | empty |
| `just lib-quickstart --build-only` | exit 0, `== PASS (504 s) ==` (then the script's usual summary table); 23 PASS lines: clean tree; Arduino A esp32/esp32s3 SUCCESS, 0 warnings, 1176000 / 1169392 B; own project SUCCESS ×2; Arduino B 1176000 B, differs from A; ESP-IDF esp32/esp32s3 0 warnings, 1103680 / 1108928 B, partition table == `ab-4m-v1`, `ROLLBACK_ENABLE=y`, app version 1.0.0 |
| `just update-e2e`, full `just lib-quickstart` | not run here; `R3-test-2` plays the full Driver (`R3-test-1` measured the full run at exit 0, 1512 s) |
| `just test` | ruff, format, mypy green; 1566 passed |

### R3-test-1 (2026-10-09): the library's rollback proof in QEMU; `just lib-quickstart` now enrolls, updates and rolls back a deliberately broken build

**Run three, in the same script and the same QEMU session.** `scripts/lib_quickstart.py`
gains `phase_rollback` after `phase_ota`, on the board left running build B. No firmware,
server, justfile-recipe or `lib-qemu/platformio.ini` change.

- **Build R** is the committed sketch with `"SOS"` → `"BAD"` and `"1.0.0"` →
  `1.2.0-qs<epoch>-rbtest` (same epoch as B, each literal guarded `count == 1`), bundled
  with `PLATFORMIO_BUILD_FLAGS=-DFF_ROLLBACK_TEST=1`. A and B get an env with that variable
  removed, so a stray value in the operator's shell cannot leak in. That switches on
  `ff_mqtt.c`'s existing hook: the announce is published and retained, its PUBACK is thrown
  away, the session never confirms, and the confirm timer is 60 s.
- **The flag is checked, not trusted.** R's `app.bin` must contain
  `FF_ROLLBACK_TEST: ignoring the announce ack` (A's and B's must not), must differ from A
  and B, and must fit the slot. Otherwise the run refuses to upload it.
- **The run.** Upload R, deploy `on_command`, wait for `update <cmd>: (ota_\d) is staged and
  bootable` (R's slot must differ from B's; both are captured from the log, never
  hardcoded), power cycle. On R's console, in order: the 60 s timer line, `ff-lib … firmware
  <R>`, `confirming on <R slot>`, the hook line, `no working session 60 s after an OTA boot`;
  plus `morse: BAD (firmware <R>)`. Then the QEMU `esp_restart()` panic (`rst:`), a second
  power cycle standing in for the reset a real board does itself, and on B's boot
  `transaction <cmd>: rolled_back (returned to <B slot>; <R slot> did not confirm)` and
  `morse: HELLO (firmware <B>)`.
- **Never `confirmed`, enforced three ways, fail-fast.** Every `GET /v1/devices` poll after
  R's deploy (`confirmed_breach`: neither `deploy.state` nor any `deploy.steps` entry is
  `confirmed` for R's cmd_id); R's console must never contain `CONFIRMED` or
  `transaction … confirmed` (`wait_for_lines(forbidden=…)`); and the final row
  (`check_rollback_row`) must be `rolled_back`, terminal, on B's version, with `confirming`
  and `rolled_back` in its steps and the slot-naming detail. `rolling_back` is reported, not
  required (best effort, 2 s grace).

**Plan corrections.** None of substance. The `morse: BAD` line repeats every loop, so it is
checked on its own rather than placed in the ordered list. The hybrid Arduino bootloader
does not log `Loaded app from partition at offset`, so the bootloader-offset check is
conditional and did not run (named gap). The R build did **not** trigger a framework
reinstall: 32 s.

Tests: `tests/test_worked_example.py::TestRollbackRun` (18: `rollback_version`, the
needle tripwire against `ff_mqtt.c`'s `#if FF_ROLLBACK_TEST` blocks, `has_rollback_hook`,
`test_confirmed_breach_*` ×5, `test_check_rollback_row_*` (good row, six refusals, no
`rolling_back`), `test_wait_for_lines_fails_fast_on_a_forbidden_line`,
`test_phase_board_runs_the_rollback_after_the_ota`). Full suite: 1566 passed.

**T2 evidence** (dev stack on 8088; api recreated with the 10.0.2.2 origins, then restored
to `localhost`, both verified with `docker inspect`):

| Check | Result |
|---|---|
| `just lib-quickstart` | exit 0, 1512 s (bundle A 727 s, B 20 s, **R 32 s**) |
| Bundles | A and B 1176912 B (differ), R 1177040 B < 1966080; R carries the hook, A and B do not; R differs from both |
| Run 1 | `enroll 200`, `announce acknowledged by the broker`, `morse: SOS (firmware 1.0.0)`; API online, `fw_version 1.0.0`, `ab-4m-arduino-v1`, `['ota']` |
| Run 2 | B `1.1.0-qs1791532201` staged into `ota_1` (47 s), power cycle, `CONFIRMED`, `morse: HELLO (firmware 1.1.0-qs1791532201)`; API `confirmed`, `is_terminal True`, 67 s |
| Run 3: upload / deploy | `POST /v1/artifact` 201 `1.2.0-qs1791532201-rbtest`; deploy `on_command` 202 |
| Run 3: stage | `update 5f87a826…: ota_0 is staged and bootable`; API `staged` 49 s after the deploy (B on `ota_1`) |
| Run 3: R's boot, in order | `OTA boot: 60 s from now …` (2120 ms), `ff-lib … firmware 1.2.0-qs1791532201-rbtest`, `confirming on ota_0`, `FF_ROLLBACK_TEST: ignoring the announce ack …`, `no working session 60 s after an OTA boot` (69425 ms); `morse: BAD (firmware 1.2.0-qs1791532201-rbtest)`; R appeared in the fleet on its own version; then `rst:` |
| Run 3: after the second power cycle | `transaction 5f87a826…: rolled_back (returned to ota_1; ota_0 did not confirm)`, `morse: HELLO (firmware 1.1.0-qs1791532201)` |
| Run 3: API | deploy `rolled_back`, `is_terminal True`, `fw_version 1.1.0-qs1791532201`, detail `returned to ota_1; ota_0 did not confirm`, steps `requested, staging, downloading, verifying, staged, confirming, rolling_back, rolled_back` (no `confirmed`), rolling_back seen: yes; 141 s after the deploy |
| Credentials | token 0, `"mqtt_password":"` 0 in 19 transcripts |
| Unplug | `online: false` (LWT) |
| Negative ("fails on confirmed") | unit tests `test_confirmed_breach_on_the_deploy_state`, `…_inside_the_steps`, `test_check_rollback_row_refuses[…confirm gate…, …broken build…]`, `test_wait_for_lines_fails_fast_on_a_forbidden_line` |
| `just lib-quickstart --build-only --skip-idf` | exit 0 |

**Named gaps.**

- **The reset after the timer is a power cycle in QEMU.** The board decides alone (timer →
  invalid → reboot); QEMU panics on `esp_restart()`, so the script pulls the power. A real
  board resets itself.
- **No bootloader-offset check**: the hybrid Arduino bootloader does not log the loaded
  partition. The `rolled_back` detail (read from otadata by the board) and `morse: HELLO`
  name the slot instead.
- **Only the confirm-timer path.** A sketch that crashes before the session (the
  bootloader's ABORTED path) is not exercised; possible later addition.
- The CUJ-1 Driver row 6 harness is now this run; `spec/cujs.md` is R3-spec-2's proposal.

### R3-fw-7 (2026-10-09): the example's `platformio.ini` is the recipe; a bare `pio run` builds both targets; `just lib-quickstart --fresh-pio-core` proves it from an empty PlatformIO core

**What.** `examples/Basic/platformio.ini` keeps its keys (only the header comment changed).
The README's `arduino-build` block is a bare `pio run` (`default_envs = esp32, esp32s3`),
gains a "Get the code" step 0 (`git clone`, substituted by the clean tree copy in the
script), and a new *Your own project* section with the `pio-own-project` block: copy
`Basic.ino`, `partitions.csv`, `platformio.ini` to `../my-blinker`, rewrite the one
`lib_deps` line to a symlink to the clone's component. `scripts/lib_quickstart.py` runs it,
parses PlatformIO's summary table (`pio_summary`/`check_envs`), and with `--fresh-pio-core`
(only with `--build-only`) runs the README blocks on an empty `PLATFORMIO_CORE_DIR`, asserting
it was empty before and holds `platforms/espressif32/platform.json` 55.03.312 after.

**T2 (`just lib-quickstart --build-only --skip-idf --fresh-pio-core`, PASS in 360 s).**

| Check | Observed |
|---|---|
| arduino-build from an empty core (bare `pio run`) | 284 s, esp32 + esp32s3 SUCCESS, 0 warnings |
| platform in the fresh core | espressif32 55.03.312 |
| app sizes A | esp32 1176096 B, esp32s3 1169504 B (slot 1966080 B) |
| own project (`../my-blinker`, symlink `lib_deps`) | 59 s, esp32 1176096 B, esp32s3 1169504 B, SUCCESS, 0 warnings |
| build B | esp32 1176112 B, differs from A |

`just lib-quickstart --build-only` (with IDF builds) still PASSes in 504 s. The git form of
`lib_deps` was probed (`pio pkg install -l "Fleetforge=git+file://...#main"` installed
`Fleetforge@0.4.7+sha.3affea2`) but is not documented: GitHub `main` is behind and no tag
carries the library. Left to R3-rel-1.

### R3-fw-5 (2026-10-09): a wrong flash layout is announced as `unknown` and refused at deploy time with the fix; `ff_ota.c` does not build without rollback

**The problem.** `FF_PARTITION_LAYOUT` was compiled in (`ARDUINO` → `ab-4m-arduino-v1`,
else `ab-4m-v1`) and announced whatever table was on flash. A sketch built with the wrong
`partitions.csv` announced the right name with a wrong fingerprint. The server refused it
as `partition_table_mismatch`, but that sentence was about profile disagreement and did not
say what to fix. A board announcing an id the server did not know, plus an artifact with no
layout, passed every check.

**What shipped.**

- **Firmware (`ff_identity.c`, every consumer).** A `FF_KNOWN_LAYOUTS` table holds both
  known ids with their fingerprints. At `ff_identity_init()` the announced
  `partition_layout` becomes the id whose fingerprint matches the measured one (D1). No
  match gives the reserved `"unknown"` (`FF_PARTITION_LAYOUT_UNKNOWN`, D2). A table that
  cannot be measured keeps the build's id, with a null fingerprint (fail-open). The
  exception is more than 32 entries, which gives `unknown` (D3). On `unknown`, one
  `ESP_LOGE` names the running slot size, the expected layout and its slot size, the
  consequence and the fix, with no 12-hex run (D4). The `board:` line gains `layout %s`.
  `FF_PARTITION_LAYOUT` is unchanged byte for byte, and now means "the layout this build
  ships".
- **No device-side refusal (D5).** The server can be corrected by shipping code; a
  refusal baked into firmware cannot be corrected on the board it refuses.
- **Server (`deploy_precheck.py`).** A new stable code, `unsupported_layout` (D6). It fires
  when the device's layout is set and not in `SUPPORTED_LAYOUTS`, with or without an
  artifact, whatever the artifact claims. It replaces `layout_mismatch` for that board,
  skips the fingerprint check, is never overridable, and gives 409 on `/deploy`. Order:
  `no_artifact_for_target` → `unsupported_layout` | `layout_mismatch` → `slot_too_small` →
  `partition_table_mismatch` → `no_ota_capability`. The sentence (D7) names what the board
  announced, the expected layout and its slot size, and the fix, with the source hint from
  `LAYOUT_SOURCES`. The expected layout is the artifact's if it is supported; otherwise
  every supported one is listed. `partition_table_mismatch` keeps its text and gains the
  same fix. `manifest.UNKNOWN_PARTITION_LAYOUT = "unknown"`.
- **Rollback guard (D8).** `ff_ota.c` has `#if !CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
  #error` next to the flash-encryption guard. `ff_ota.c` is always compiled and every
  consumer's bootloader comes from the same sdkconfig, so no consumer builds the OTA path
  without rollback. Before this, an IDF `main` that skipped `fleetforge_start.c`, or the
  agent itself, compiled it unguarded. It emits no code: +816 B on esp32 is the detection.
- **Tests.** `tests/test_layout_detection.py` (new): the C table equals
  `SUPPORTED_LAYOUTS`; `unknown` is spelled alike in C and Python and is never supported;
  the announce uses the detected id; the too-many and unmeasured branches; the error line's
  needles and no hex run; the `ff_ota.c` guard; the fixture. `tests/test_deploy_precheck.py`
  gains `TestUnsupportedLayout` and the appended fix. `tests/test_api_deploy.py`: the
  "other layout" cases use `ab-4m-arduino-v1`, and an `unknown` case proves precheck ==
  deploy and the 409. Size budgets were raised for all four targets.
- **Fixture.** `tests/fixtures/wrong-layout-partitions.csv` is the Arduino offsets with
  0x1C0000 slots (fingerprint `47db5392…7c4c`). It is not under `examples/`.

**T2 (2026-10-09, dev stack on :8088, no board).**

| Step | Result |
|---|---|
| T2-A sim `--platform-type esp32 --partition-layout unknown --ota-slot-size 1835008 --partition-sha 47db…7c4c` | row `unknown` / 1835008 / `47db…7c4c`; upload `r3fw5-1791526565` (`ab-4m-arduino-v1`) 201 |
| T2-A precheck / deploy | `deployable: false`, `["unsupported_layout"]`; deploy **409**, detail == precheck message; contains `unknown`, `1835008`, `ab-4m-arduino-v1`, `1966080`, `examples/Basic/partitions.csv`, `USB`; no backtick; sim log has no `dn/cmd`/stage, `deploy: null` |
| T2-A control: sim `ab-4m-arduino-v1` + `05528998…1fc4` | precheck `deployable: true`, no refusals |
| T2-B QEMU, lib bundle with the fixture's table at 0x8000 | `E (7708) ff-id: this board's partition table is not a layout this firmware knows: its OTA slot is 1835008 bytes, and this build expects ab-4m-arduino-v1, whose two OTA slots are 1966080 bytes each. …partitions.csv… flash it once over USB…`; `board: … sha256 47db…7c4c, layout unknown`; `enroll 200`; `announce acknowledged` |
| T2-B API | `000000000000`: `unknown`, 1835008, `47db…7c4c`; precheck `unsupported_layout`, deploy 409 with the same sentence; `ff-ota` lines in the log: 0 |
| T2-B positive control (`just lib-qemu --fresh`, correct table) | `layout ab-4m-arduino-v1`, `05528998…1fc4`, no `E … ff-id` line, precheck `deployable: true` |
| Agent no-regression (`just agent-qemu esp32 --fresh`) | `layout ab-4m-v1`, `1fa67e6b…59ed`, enroll 200; `just agent-qemu-smoke esp32` HARNESS OK |
| T2-C agent copy with `# CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE is not set` | docker build exit 1: `components/fleetforge/src/ff_ota.c:97:2: error: #error "fleetforge's OTA path needs CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y…"`; repo `agent/sdkconfig.defaults` untouched |
| Builds | `just agent-build-all` BUNDLE OK ×4, 0 `warning:`; esp32 1,030,320 B (+816). `just lib-build esp32`/`esp32s3` 0 warnings; `just lib-bundle esp32-qemu` exit 0; `just lib-quickstart --build-only` PASS (396 s); `just test` 1542 passed (ruff, format, mypy green) |

**Named gaps.**

- **A bootloader from an earlier, different flash** (`idf.py app-flash`) is only discovered
  after its first OTA, and is not reported as `false` (A3). The build guard covers a
  bootloader built with the app, not one left over from before.
- **Every unsupported map announces the same `unknown`.** R3-be-2's detected profiles must
  key on the fingerprint.
- **`UploadBuild.tsx` offers every fleet-reported layout**, so a fleet of only `unknown`
  boards pre-selects `unknown`, and the upload answers 400 "unknown partition_layout; this
  server understands: …". Not fixed.
- **`diagnostics.ts`'s "reported id" regex** already matches the `board:` line's
  fingerprint (pre-existing since R2b-fw-2). Not fixed. The new error line adds no hex run.

**Spec proposal (Patch A, NOT applied).** `spec/device-protocol.md` → *Partition layouts*,
add after the table:

> `unknown` is reserved. A board whose decoded partition table matches no layout id its
> firmware knows announces `partition_layout: "unknown"` (with its real `ota_slot_size` and
> `partition_table_sha256`). It is never a row in this table. The server refuses every
> deploy to it (`unsupported_layout`), naming the layout the build expects and its slot
> size. A board that cannot measure its table announces its build's id with
> `partition_table_sha256: null`.

### R3-fw-4 (2026-10-09): the worked example is a Morse blinker in Arduino and ESP-IDF, and `just lib-quickstart` plays its README; the first library OTA confirmed

**Two flavours, one example.**

- **Arduino / PlatformIO (the persona).** `examples/Basic/Basic.ino` became the blinker in
  place (61 lines). The folder keeps its name because tests, `lib-qemu/`, `just lib-build`,
  CRITICAL.md and the docs pin it. Its `partitions.csv` and `platformio.ini` are untouched.
  - `Fleetforge.begin(FW_VERSION)` is still the first statement of `setup()`.
  - `MORSE_MESSAGE "SOS"` and `FW_VERSION "1.0.0"` are `#ifndef`-overridable, and each
    literal occurs once, so the README's `sed` edits exactly those two lines.
  - `loop()` prints `morse: <msg> (firmware <ver>)` and blinks it (200 ms unit, standard
    gaps).
  - The LED is `LED_BUILTIN` where the variant defines it (S3 DevKitC-1: the RGB LED, which
    core 3.x drives through `digitalWrite`), else GPIO 2.
- **ESP-IDF.** A new project, `examples/basic_idf/`:
  - `main/main.c` is the reader's part (67 lines): `fleetforge_start()` first, then the
    same blinker, printing `ff_identity_fw_version()`.
  - `main/fleetforge_start.c`/`.h` is the boot ladder, copied unchanged. It is the C twin
    of `Fleetforge.cpp`: the confirm timer is armed first, an `#error` fires without
    `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`, and it uses only the public `include/`.
    It follows `agent_main.c`'s order with no `ESP_ERROR_CHECK`.
  - `CMakeLists.txt` sets `PROJECT_VER "1.0.0"` (= `fw_version`) and
    `EXTRA_COMPONENT_DIRS ../..`. The build's `Component paths` holds the one `fleetforge`
    component plus `basic_idf/main`.
  - `partitions.csv` is `ab-4m-v1`, `agent/partitions.csv` row for row, because an IDF
    build of the component announces that id. `sdkconfig.defaults` is the safety subset of
    the agent's: custom table, 4 MB, rollback, cert bundle, time/date check.
- **No public-surface change.** The component's `include/` is untouched. The copied ladder
  stands in for a public `ff_start()`, which is proposed for the owner in DECISIONS.

**The README is the script.** Each example has a README a person follows. Every command a
reader types for a build is in a fenced block that starts `# quickstart: <name>`: Basic has
`arduino-build`, `arduino-edit` and `arduino-build-b`; basic_idf has `idf-build` and
`idf-build-s3`. `scripts/lib_quickstart.py` (stdlib only, `just lib-quickstart`) copies a
clean tree (`git ls-files --cached --others --exclude-standard`) to a 0700 temp dir. It runs
the blocks verbatim and fails by name on a missing one.

- `--build-only` is the compile half.
- The full run adds the board steps in QEMU against the dev stack. USB flash →
  `just lib-qemu --fresh`. The self-reboot → `apply: "on_command"` plus a power cycle.
- Build B's version is run-unique (`1.1.0-qs<epoch>`).
- The admin password comes from env or `.env`, and is never printed or put in argv.
- The token is checked to occur 0 times in every transcript, and is revoked if a run dies
  before enrolling.
- The phases are functions (`phase_builds`, `phase_enroll`, `phase_ota`), so R3-test-1 adds
  `phase_rollback`.

**Plan correction found by the clean-tree run.** `just lib-bundle` failed on a fresh tree:
`lib_bundle: …/bootloader.bin, named by idedata.json, does not exist`. pioarduino decides to
reinstall the framework and hybrid-compile from a hash in the *project's*
`sdkconfig.defaults`. A clean `lib-qemu/` has none, so the first `pio run` does the ~11 min
compile and changes the build checksum. The next pio invocation (`-t idedata`) then wiped
`.pio/build/esp32-qemu` down to `idedata.json`. This was reproduced in a scratch tree: a
second `pio run` rebuilt in 28 s, and idedata then kept everything. The recipe now runs
`pio run` twice. The second run is a no-op when nothing changed (in-repo `just lib-bundle`:
20 s, no reinstall). The repo's own `lib-qemu/` keeps its matching hash, so a quickstart run
does not make it rebuild.

Tests: `tests/test_worked_example.py` (42). `BOOT_SEQUENCE_ORDER` was factored out of
`test_arduino_library.py` and is shared. Full suite: 1521 passed.

**T2 evidence** (dev stack on 8088; api recreated with `FF_PUBLIC_BASE_URL=http://10.0.2.2:8088
FF_S3_PUBLIC_ENDPOINT_URL=http://10.0.2.2:9000 … up -d --no-deps api`, both verified, then
restored to `localhost`):

| Check | Result |
|---|---|
| `just lib-quickstart --build-only` | exit 0, 416 s |
| Arduino A (`arduino-build`, esp32 + esp32s3), clean temp tree | 0 warnings from the library/sketch; `firmware.bin` 1175264 B / 1168656 B < 1966080 |
| `arduino-edit` + `arduino-build-b` | 0 warnings; esp32 1175264 B; bytes differ from A |
| ESP-IDF `idf-build` / `idf-build-s3` in `espressif/idf:v5.5.5@sha256:a9231d06…` | 0 warnings; app 1102832 B (esp32) / 1108112 B (esp32s3); decoded `partition-table.bin` == `agent/partitions.csv` (6 rows, ff_cfg 0x12000); resolved `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`, none of the 7 eFuse-burn options `=y`; `esp_app_desc_t.version` 1.0.0 |
| `just lib-quickstart` | exit 0, 1301 s (bundle A 742 s: fresh-tree hybrid compile; bundle B 20 s) |
| QEMU bundles | A app 1176144 B, B 1176160 B, differ |
| Boot A (`lib-qemu --fresh`, fresh token, `--hb 10`) | `ff-lib: fleetforge library 0.4.7, firmware 1.0.0`, `ff-enroll: enroll 200`, `announce acknowledged by the broker`, `morse: SOS (firmware 1.0.0)` |
| `GET /v1/devices` | `online: true`, `fw_version 1.0.0`, `partition_layout ab-4m-arduino-v1`, `capabilities ["ota"]`, `agent_version 0.4.7` |
| Upload B / deploy | `POST /v1/artifact` 201 (`1.1.0-qs1791521470`, `ab-4m-arduino-v1`); `POST …/deploy` `apply: on_command` 202 |
| Stage | `ff-ota: update 7416d299…: ota_1 is staged and bootable`; API `staged` 48 s after the deploy |
| Power cycle → B | `ff-mqtt: this image was written by OTA and is now CONFIRMED`; `morse: HELLO (firmware 1.1.0-qs1791521470)` |
| API after | `fw_version 1.1.0-qs1791521470`, deploy `confirmed`, `is_terminal: true`, 68 s after the deploy; steps `requested, staging, downloading, verifying, staged, confirming, confirmed`; `from_version 1.0.0`; `rollback_capable: true` (measured on the PENDING_VERIFY boot); token `used` by `000000000000` |
| Credentials | token 0 occurrences and no `"mqtt_password":"` in 13 transcripts (both QEMU logs, every build log, the script's output) |
| Unplug | `online: false` (LWT) |
| Negative: marker renamed to `arduino-compile` in a scratch copy | `--build-only` exit 1 at once: `…/Basic/README.md has no '# quickstart: arduino-build' block`; pytest cases `test_a_missing_block_fails_by_name`, `test_the_real_readme_with_a_renamed_marker_fails` |
| In-repo `just lib-build esp32`, `esp32s3` (clean `.pio`) | 0 `warning:` lines; 1175264 B / 1168640 B. `just lib-bundle esp32-qemu` exit 0 (20 s) |

**Named gaps.**

- **The IDF flavour is compile-only.** QEMU runs the Arduino flavour.
- **IDF `agent_version` is `PROJECT_VER`** (`ff_identity.c`'s non-ARDUINO branch). This is
  R3-fw-6's `lib_version`.
- **The QEMU run boots the hybrid build**, not the persona binary (inherited from R3-fw-3).
  The OTA artifact must be that build too.
- **`on_command` + a power cycle in QEMU** vs the dashboard default `auto` on a board.
- **The IDF example cannot light the S3 DevKitC-1's RGB LED**; there, the console line is the
  indicator.
- **Each quickstart run pays one ~11 min hybrid compile** in its fresh `lib-qemu/`.
- **The enrollment token rides in argv** to `just agent-cfg` → `ff_cfg.py`. This is the
  existing tooling's interface, and the recipe is `@`-quiet. The token burns at the
  board's enroll seconds later.
- **`ff_cfg` for library users is still a CLI step** (`ff_cfg.py` + `esptool write-flash`);
  the browser flasher writes agent bundles only.

### R3-fw-3 (2026-10-08): the Arduino library is the component plus `library.json` and `Fleetforge.cpp`; the example ships `ab-4m-arduino-v1`

**Layout.** The component directory IS the library. `agent/components/fleetforge/library.json`
(PlatformIO manifest, `-Wall -Wextra -Werror`, `-DLOG_LOCAL_LEVEL=ESP_LOG_INFO`) uses
PlatformIO's defaults (`src`, `include`), so every `src/*.c` compiles, the same set the CMake
`SRCS` pins. No second copy, no generator. The wrapper is the component's **second
consumer**: `src/Fleetforge.h` (the whole Arduino API, `Fleetforge.begin(fw_version)`) and
`src/Fleetforge.cpp` (the boot task). It reaches the component through `include/` plus one
private setter, and it does not include `Arduino.h`. The CMake `SRCS` is explicit, so the IDF
build never compiles the `.cpp`, and its body is guarded by `ARDUINO` anyway. The example is
`examples/Basic/` (`Basic.ino`, `partitions.csv`, `platformio.ini`). It is minimal, because
R3-fw-4 writes the worked example.

**Toolchain.** pioarduino `55.03.312-1` = Arduino-ESP32 **3.3.12 on ESP-IDF v5.5.5** (the
agent's IDF, and the core `ab-4m-arduino-v1` was measured on), pinned by exact URL. It needs
**PlatformIO Core ≥ 6.2.0**: the box had 6.1.19, upgraded with `uv tool upgrade platformio`.
The official `espressif32` 7.0.1 platform (core 2.0.17 = IDF 4.4) cannot compile the component.

**The rollback posture** (CRITICAL; all three pinned by `tests/test_arduino_library.py`):

1. `extern "C" bool verifyRollbackLater(void) { return true; }` in `Fleetforge.cpp`. Core
   3.3.12's `initArduino()` (`esp32-hal-misc.c:320`) runs `if (!verifyRollbackLater()) { if
   PENDING_VERIFY && verifyOta() → esp_ota_mark_app_valid_cancel_rollback(); }` **before
   `setup()`**, and the weak default returns false. Without the override every OTA'd Arduino
   image confirms itself at boot. `nm`: `T verifyRollbackLater` (strong) in all three ELFs,
   and the disassembly is `movi.n a2, 1; retw.n`.
2. `begin()` arms the confirm timer (`ff_mqtt_arm_confirm_timer`) before anything that can
   fail or return. It is preceded only by a loop of `esp_log_level_set`, so the timer's own
   "OTA boot: N s" line reaches the console.
3. `#if !CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE` → `#error`: a configuration that cannot roll
   back does not compile. Proven with a stub `sdkconfig.h` without the option: `error: #error
   "Fleetforge needs CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y …"`.

**The plan assumed archive linking; pioarduino does not archive.** Its builder sets
`lib_archive = False` unless the project says otherwise ("makes weak defs in framework and
libs possible"), and the Arduino IDE links library `.o` files directly too. So the override
is linked whenever the library is in the build, not only when `begin()` is called. A sketch
that includes the library and never calls `begin()` therefore never confirms an OTA'd image,
and the bootloader rolls it back at its next reset. That is the conservative failure. With
archive linking, by contrast, the sketch would silently auto-confirm.

**The three Arduino differences live in `ff_identity`, under `#if defined(ARDUINO)` only**,
so the IDF agent compiles the same text: app size **1029504 B before and after**.

- `FF_PARTITION_LAYOUT` is `"ab-4m-arduino-v1"` under ARDUINO, else `"ab-4m-v1"`, verbatim.
- `fw_version`: `ff_identity_set_app_versions(fw, agent)` (private, ARDUINO only, 1-31
  printable ASCII, called once by `begin()`). `ff_identity_fw_version()` returns it when set.
- `agent_version` in the announce is the library version (`FF_LIB_VERSION`).

**Side finding re-measured on core 3.3.12.** The persona build's `esp_app_desc_t` is
`version='6671d0b'`, `project_name='arduino-lib-builder'`, `idf_ver='v5.5.5'`. That is the
lib-builder's git hash, not the maker's version, so the setter is needed on 3.x too.

**One version, three files.** `src/ff_lib_version.h` (`FF_LIB_VERSION "0.4.7"`),
`library.json` `"version"` and `agent/version.txt` are pinned equal. The failure message
names all three.

**Logging.** The stock core builds with `CONFIG_LOG_MAXIMUM_LEVEL=1` (ERROR), so `ESP_LOGI`
is compiled out unless `LOG_LOCAL_LEVEL` is raised. `initArduino()` also sets `"*"` to ERROR
before `setup()`. Both were needed: `-DLOG_LOCAL_LEVEL=ESP_LOG_INFO` in `library.json` (the
flags reach the library's sources only, checked with `pio run -v`), and `begin()` raising
exactly the 13 `ff-*` tags to INFO (a test pins the list to every `TAG` in `src/`).
`CORE_DEBUG_LEVEL` was not needed and is not set.

**`-Werror` holds, with the core's carve-outs.** The core's global flags carry
`-Wno-error=unused-variable/unused-function/…` and `-Wno-unused-parameter/-Wno-sign-compare`,
which a trailing `-Werror` does not override (IDF's defaults carry the same set, so the CMake
component has the same posture). A deliberate `snprintf(b, 4, "%d", "x")` failed the build
with `-Werror=format=`. An unused variable only warns, but the T1 gate rejects any `warning:`
from the component.

**QEMU.** QEMU's esp32 has only the OpenCores NIC, and the stock Arduino libs lack its
driver. The QEMU build is a **separate project, `lib-qemu/platformio.ini`** at the repo root,
not a third env in the example:

- **Inputs.** The same sketch, table and library, plus
  `custom_sdkconfig = CONFIG_ETH_USE_OPENETH=y` (pinned: every other key equals `env:esp32`'s).
- **Hybrid compile has side effects.** pioarduino's hybrid compile rebuilds the Arduino libs
  **in the shared package dir**, and it writes `.dummy/`, `managed_components/` and two
  sdkconfig files into the project. Building `env:esp32` afterwards then deleted and
  re-downloaded the framework (`*** Reinstall Arduino framework ***`).
- **The fix.** `just lib-bundle` runs with its own
  `PLATFORMIO_CORE_DIR=~/.platformio-fleetforge-qemu`. The project sits outside `agent/`,
  because `test_agent_holds_no_credential` scans `agent/` file by file and found a
  `WIFI_SSID=` in a managed component.
- **The persona libs stayed stock.** Their `sdkconfig` sha256 is `7488c7a5…` before and
  after.
- **The resolved hybrid config differs from stock beyond the NIC.** It has no SPIRAM, no
  Matter/camera components, 240 MHz CPU and DIO 40 MHz flash. Rollback, mbedTLS and log
  settings are unchanged. Named gap below.

`agent/tools/lib_bundle.py` turns the build into a bundle `qemu_image.py` takes unchanged.
Offsets come from `idedata.json` `flash_images`, plus where the build put `firmware.bin` in
`firmware.factory.bin` (`application_offset` is absent from idedata on this platform; it is
used and cross-checked when present). The table is decoded with `make_manifest.py`'s
decoder. It refuses: otadata ≠ boot_app0's offset, ota_0 ≠ the app's offset, no `ff_cfg`,
unequal slots, a stale factory image, and a fingerprint ≠ the spec's. Output goes to
`lib-qemu/.pio/bundle/`, never `agent/dist/`. Recipes: `lib-build`, `lib-bundle`, and
`lib-qemu` (same `qemu_program`, own `flash-lib-esp32.bin`, same `ff-qemu-esp32` name and
refusal).

Tests: `tests/test_arduino_library.py` (41). Full suite 1479 passed.

**T2 evidence** (dev stack, traefik on 8088, board API base `http://10.0.2.2:8088`):

| Check | Result |
|---|---|
| Clean compile (`rm -rf examples/Basic/.pio`; `just lib-build esp32`, `esp32s3`) | both exit 0, zero `warning:` lines; `firmware.bin` 1171024 B (esp32), 1140352 B (esp32s3); esp32-qemu 1172384 B; all < 1966080 |
| `xtensa-*-nm firmware.elf` ×3 | `T verifyRollbackLater` (strong), `W verifyOta` (core default, unused); disassembly returns 1 |
| Built `partitions.bin` (esp32, esp32s3), decoded | nvs 0x9000/0x5000, otadata 0xe000/0x2000, ota_0 0x10000/0x1E0000, ota_1 0x1F0000/0x1E0000, ff_cfg type 1 subtype 0x40 0x3D0000/0x1000, coredump 0x3F0000/0x10000; fingerprint `05528998…1fc4` |
| `just lib-qemu --fresh` (fresh token, `--hb 10`) | `ff-lib: fleetforge library 0.4.7, firmware 1.0.0`, `ff-net: eth link up, ip 10.0.2.15`, `ff-enroll: enroll 200`, `ff-mqtt: mqtt connected as 000000000000`, `announce acknowledged by the broker`, `up/hb` every 10 s |
| `GET /v1/devices` | `online: true`, `platform_type: esp32`, `partition_layout: ab-4m-arduino-v1`, `ota_slot_size: 1966080`, `partition_table_sha256: 05528998…1fc4`, `fw_version: 1.0.0`, `agent_version: 0.4.7`, `capabilities: ["ota"]`, `rollback_capable: null`; token `used` |
| `up/hb` subscription | two heartbeats 10 s apart: `{"fw_version":"1.0.0",…,"rssi":null,…,"boot_ok":true}` |
| Restart without `--fresh` | `reusing the stored credential (no enrollment)`; enrollment-token counts unchanged |
| Transcript | enrollment token 0 occurrences; no password value |
| `just agent-qemu-stop esp32` | device `online: false` (LWT) |
| IDF agent: `just agent-build esp32`, size test, smoke | `BUNDLE OK`, 0 warnings, app 1029504 B (unchanged), `test_agent_power_and_size.py` 16 passed, `HARNESS OK` |

**Named gaps.**

- **Arduino IDE packaging.** There is no `library.properties`: the IDE compiles `src/` only
  and cannot see `include/`, so it needs a flattened package (follow-up task `R3-fw-8`).
  **Resolved by R3-fw-8** (see *R3-fw-8* above): `just lib-arduino-package` generates it,
  `just lib-arduino-check` proves it on core 3.3.12.
- **Upload form version pre-fill.** `frontend/src/appImage.ts` reads `esp_app_desc_t.version`,
  which is the lib-builder hash on Arduino builds. The operator types the version.
- **`ff_cfg` provisioning for library users.** It goes at 0x3D0000, and the browser flasher
  writes only `ab-4m-v1` agent bundles. This is R3-fw-4 / R3-rel-1 quickstart work.
- **Conflicting Arduino calls.** The sketch can still call Arduino
  `WiFi`/`configTime`/`HTTPUpdate` and conflict. This is documented in `Fleetforge.h`, not
  enforced.
- **The QEMU proof runs the `lib-qemu` hybrid build**, not the byte-identical persona binary.
  Its resolved config differs from stock beyond the NIC (SPIRAM, Matter/camera, clocks).
- **The layout id is chosen by `ARDUINO` at compile time.** Detection by fingerprint is
  R3-fw-5. **Resolved by R3-fw-5** (see *R3-fw-5* above): the announced id is now detected
  by fingerprint, and the compiled id is only the expected layout and the fail-open fallback.
- **Every library object is linked when the library is in the build** (no archive). See
  above. This matters for R3-fw-6's marker writer rule: "kept alive only by a reference"
  still holds under `--gc-sections`, but "a sketch that merely installs the library" is now
  "a sketch whose build includes it".

### R3-fw-2 (2026-10-08): the protocol is the `fleetforge` ESP-IDF component; the agent is its first consumer

The 12 `ff_*` modules moved with `git mv` from `agent/main/` to
`agent/components/fleetforge/`. Public headers are in `include/`. Everything else is in a
private `src/` (`PRIV_INCLUDE_DIRS`): every `.c`, `ff_ota.h`, `ff_txn.h`, `ff_net_adapter.h`,
and the five `ff_{identity,mqtt,net,store,time}_internal.h` halves split out of the old headers.
`agent/main/` holds only `agent_main.c` (unchanged), with `REQUIRES fleetforge`. IDF finds
the component from `<project>/components/` on its own.

**The public surface** is the 8 headers `agent_main.c` includes and the 16 functions it
calls, no more and no less. It is additive-only from here on, like the wire protocol. Kept
private on purpose: `ff_mqtt_publish_status` + `FF_STATUS_*` (a firmware could publish
`confirmed`), the rollback_capable writers (a firmware could *claim* it),
`ff_identity_announce_json`, `FF_PARTITION_LAYOUT`/`FF_OTA_SLOT_SIZE`, the NVS keys, and
the link/clock helpers. The version's single source is `agent/version.txt`, and
`idf_component.yml` has no `version:` (DECISIONS 2026-10-08 R3-fw-2). `FF_ROLLBACK_TEST`
moved to the component's CMakeLists with `ff_mqtt.c`, and `FF_FAULT_TEST` stays with
`agent_main.c`. IDF deps are `PRIV_REQUIRES`, and `-Werror` is on both components.

Tests: `tests/agent_src.py` is one definition of where the sources live, and
`agent_sources()` refuses to come back empty. Every test that read `agent/main/*` was
repointed. The private-symbol pins now read the `*_internal.h` they moved to. The new
`tests/test_ota_component.py` (14 tests) pins the surface rule, the hazards, the boundary,
the CMake posture and the manifest. A freshness case proves a commit under
`agent/components/fleetforge/` makes a bundle STALE. Agent test files: 231 → 246 passed,
0 skipped. Size budgets were raised to the exact measured byte (+64 B esp32, +48 B others).
The cause is the longer `__FILE__` paths, with no code change.

**T2 evidence** (baseline = the 0.4.7 bundles from `7a58f80`, copied aside before any rebuild):

| Check | Result |
|---|---|
| B1: `git diff --cached -M` on `ff_mqtt.c`, `ff_ota.c`, `ff_txn.c` | only `#include` lines (2, 1, 0); `ff_txn.c` 100% rename; `agent_main.c` unchanged |
| B2: prototypes + `FF_*` defines of each old split header vs public ∪ internal | equal for all 5 (identity 8+5, mqtt 3+12, net 4+0, store 6+13, time 3+0); no overlap; the other 6 headers are 100% renames |
| B3: `sdkconfig.resolved`, `config_sha256`, partition-table / otadata sha256, `partition_layout`, `ota_slot_size` | identical on esp32, esp32s3, esp32c3, esp32c6; bootloader differs only in its date string; no diff under `agent/partitions.csv`, `agent/sdkconfig.defaults*`, `spec/`, `design/` |
| B4: `strings` diff of `app.bin` | text changes only `./main/ff_{mqtt,net_wifi,net_openeth}.c` → `./components/fleetforge/src/…` and the build date/time (the rest is literal-pool address bytes shifted by relinking) |
| Build: `just agent-build` ×4 | `BUNDLE OK` ×4, 0 warnings; component manager: `Processing 1 dependencies: [1/1] idf (5.5.5)`, no error |
| Q0: `just agent-qemu-smoke esp32` | `HARNESS OK` |
| Q1/Q2: baseline vs component build, `--fresh`, fresh tokens | both `enroll 200`, credential stored, announce acked. `jq -S` announce diff empty, key order identical, device rows identical (`0.4.7`, `["ota"]`, `ab-4m-v1`, 1966080, table sha `1fa67e6b…`). Boot-log diff: build date, clock, uptime, one async eth line order |
| Q3: OTA to B `0.4.8-r3fw2-1791508042` (`on_command`, power cycle) | cmd `510453683aba…`: `pending_verify` → `confirming on ota_1` → `CONFIRMED` → record cleared. Rows `…, staged, confirming, confirmed`. `fw_version` = B |
| Q4: R `0.4.9-r3fw2-1791508042-rbtest` (`FF_ROLLBACK_TEST=1`) | cmd `7af7beb3d8fb…`: `FF_ROLLBACK_TEST: ignoring the announce ack on purpose`, then 60 s later `no working session … rolling back`. Rows `…, staged, confirming, rolling_back, rolled_back` (`returned to ota_1; ota_0 did not confirm`), **no `confirmed`**, board back on B |

B and R were built from scratch copies of `agent/` with their own `version.txt`, so the
checked-in `agent/version.txt` was never edited. Neither was built into `agent/dist`. The api
was recreated with the `10.0.2.2` origins for the run and restored afterwards. Proposed
(not applied): path-only fixes in `spec/open-questions.md`, `design/partitions.md` and
`design/decisions/enrollment-console-is-the-diagnostic-surface.md` (DECISIONS entry).

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

### Thin OTA library — Arduino, ESP-IDF component, PlatformIO (Priority: P1) — **LANDED 2026-10-09 (R3, v0.5.0 prepared; deploy pending the owner)**

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

#### Library marker proposal (R3-spec-3, 2026-10-08) — ACCEPTED: Patch A applied in 8f1f458, Patch B applied in R3-fw-6

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

#### CUJ-1 Driver proposal (R3-spec-2, 2026-10-09) — ACCEPTED: Patch A (a)-(d) applied in 8f1f458

`spec/` is protected during `/implement`, so this is the decision written as paste-ready
text for a later `spec:` commit (the route R2-spec-1, R2b-spec-2 and R3-spec-3 took).
**Nothing here is built.** No code, test, recipe or script change has been made. `R3-test-2`
(the T3 `/verify` play) is blocked in `TODO.md` until the owner accepts and applies Patch A.
The decision is logged in `DECISIONS.md` (2026-10-09, R3-spec-2), as proposed only.

**The question.** `spec/cujs.md` → CUJ-1 → **Driver** was written in `R3-spec-1`
(2026-09-22), before any R3 harness existed. Its rows name an interactive QEMU boot, a
simulator plus a curl, and "a QEMU run that … requires `rolled-back`" that did not exist.
Since then three harnesses landed. `R3-test-2`'s acceptance is "deterministic judge passes
on steps 1-3, 5, 6; any segment still without a harness is listed, not scored", so the table
must let `/verify` attribute every deterministic assertion to a concrete command.

**Where each row comes from.**

| Row | Source | Harness |
|---|---|---|
| 1–2 | `R3-fw-4` (DECISIONS 2026-10-09), its proposal (b) | `just lib-quickstart --build-only`: the README blocks verbatim in a clean temp tree (Arduino esp32/esp32s3 + edited build B; ESP-IDF esp32/esp32s3) |
| 3 | `R3-fw-4` full run | `just lib-quickstart`, `== Phase 2a: enroll and heartbeat …` (`phase_enroll`); `tests/test_enroll.py` stays for `POST /v1/enroll` |
| 5 | `R3-fw-4` full run; `R2b-test-3` (DECISIONS 2026-10-05) proposal | on the device `== Phase 2b: deploy build B …` (`phase_ota`, `fw_version` read back from the board); server and dashboard `just update-e2e` `upload-good`, `deploy-good` |
| 6 | `R3-test-1` (DECISIONS 2026-10-09); `R2b-test-3` | on the device `== Phase 2c: deploy a broken build …` (`phase_rollback`: `FF_ROLLBACK_TEST` build; pass = `rolled_back`, `fw_version` = build B, no `confirmed` anywhere); server and dashboard `just update-e2e` `deploy-broken` |
| — | `R3-fw-5`; `R2b-test-3` | server and dashboard `just update-e2e` `precheck-wrong-layout`; on the device `docs/runbooks/agent-qemu.md` → *A wrong flash layout is refused* (a procedure, so not graded) |

**What changes, and why.**

- **The library path is the journey.** Rows 3, 5 and 6 name the library harness first. The
  old row-3 `just agent-qemu esp32` is dropped (it boots and waits: interactive, not a
  driver), and so is `just agent-qemu-smoke esp32` (it proves boot and a config read, not
  enroll, and it boots the stock agent, not the persona's sketch). The old row-5
  `just sim-fleet 1 --capabilities ota` plus `POST …/deploy` is superseded by
  `just update-e2e`, which does exactly that through the real dashboard.
- **Halves inside a row, not extra rows.** Rows 5 and 6 each carry "On the device:" and
  "Server and dashboard:", so a segment stays one step of the journey.
- **A `Graded` column.** The spec's own *On segmented Drivers* paragraph says a segmented
  CUJ "declares which segments are gradeable"; the current table does not. Values: `yes`,
  or `server and dashboard half` for the wrong-layout row.
- **Phases named by the script's own headers** (`phase 2a`, `2b`, `2c`), so `/verify` can
  attribute one full run's output to rows 3, 5 and 6. One full `just lib-quickstart` run is
  Phase 0 + 1 + 2a + 2b + 2c on one emulated board (1512 s measured in `R3-test-1`); its
  preflight exits 2 unless the api hands out `10.0.2.2` origins.
- **The bench sentence goes.** The 2026-10-08 decision retired the hardware bench, so
  "proved at the bench rather than by a harness" is stale. It becomes "done by a person; no
  harness plays them and T3 does not grade them".
- **Two Judge fixes ride along**, each independently applicable, because `R3-test-2` cannot
  pass steps 5 and 6 mechanically without them:
  - **`rolled-back` → `rolled_back`.** The wire state is `rolled_back` (underscore):
    `spec/device-protocol.md`'s state machine `rolling_back → rolled_back`, the API's deploy
    state, and what `phase_rollback` requires. A mechanical judge grepping for the hyphen
    never matches.
  - **Step 5's version source.** "Read from the running image's own descriptor" is literally
    false on the persona's path. In an Arduino library build `esp_app_desc_t.version` is the
    core's IDF string (the R3-spec-3 side finding, re-measured in `R3-fw-3`); `fw_version`
    is the string the sketch passes to `Fleetforge.begin(FW_VERSION)`, compiled into the
    running image (`ff_identity.c::ff_identity_fw_version`, its `#if defined(ARDUINO)`
    branch; `Fleetforge.cpp`). The intent, "the running image, not what it was told to
    install", is kept and both sources are named.
- **Supported By (optional).** `docs/runbooks/upload-artifact.sh` no longer exists; the
  dashboard upload form (`R2b-fe-7`) replaced it. Stale status in a status-free file, not
  Driver substance, so it is a separate, optional item.

**Paste-ready spec text.** Everything below is for `spec/cujs.md`, one patch, **Patch A**,
items (a) to (d). Each item has an exact anchor (the text it replaces, quoted line for line,
which must occur exactly once) and its replacement. The items are independent and can be
applied in any order. No test or script reads `spec/cujs.md` (`grep -rln cujs tests/
scripts/ frontend/scripts` finds nothing), so `just test` stays green either way.
`R3-test-2` needs at least (a), and (b) and (c) for its deterministic judge to be
satisfiable on steps 5 and 6. (d) is optional.

*(a) Patch A. `spec/cujs.md` → CUJ-1 → Driver.* Anchor: the whole Driver block, from the
line that begins `**Driver:** segmented` through the line `bench rather than by a harness.`
(lines 88-100 today):

```text
**Driver:** segmented — this journey crosses four releases. Each segment names the harness
that plays it; a segment whose harness does not exist yet is **not graded**.

| Steps | Segment | Harness |
|---|---|---|
| 1–2 | Sketch compiles with the library added | The worked example's build on both ESP-IDF and Arduino, from a clean checkout |
| 3 | One flash → board on the fleet | `just agent-qemu esp32` (boots the real bundle against the dev stack and enrols); `just agent-qemu-smoke esp32` for the unattended form; `pytest tests/test_enroll.py` for the `POST /v1/enroll` surface |
| 5 | OTA a changed build → new version reported | `just sim-fleet 1 --capabilities ota` then `POST /v1/devices/{id}/deploy` for the server half; the on-device path per `docs/runbooks/agent-qemu.md` |
| 6 | A bad build recovers itself | A QEMU run that deploys a deliberately broken build and requires `rolled-back` |
| — | A wrong flash layout is refused, not flashed | A deploy against a build whose partition table disagrees with its announced `partition_layout` |

Steps 4 (physical install) and the unaided half of step 3 are human, and are proved at the
bench rather than by a harness.
```

Replace it with:

```markdown
**Driver:** segmented — this journey crosses four releases. Each segment names the harness
that plays it and whether it is graded; a segment whose harness does not exist yet is
**not graded**, and neither is a half that names a procedure rather than a command.

| Steps | Segment | Harness | Graded |
|---|---|---|---|
| 1–2 | Sketch compiles with the library added | `just lib-quickstart --build-only`: the worked example's README build steps, run verbatim in a clean copy of the tree — Arduino (PlatformIO) for esp32 and esp32s3 plus the edited second build, and ESP-IDF for esp32 and esp32s3 | yes |
| 3 | One flash → board on the fleet | `just lib-quickstart`, phase 2a (enroll): the example, flashed once in QEMU in place of the USB flash, enrolls and heartbeats against the dev stack and is online in the fleet list on the version it was built with; `pytest tests/test_enroll.py` for the `POST /v1/enroll` surface | yes |
| 5 | OTA a changed build → new version reported | On the device: `just lib-quickstart`, phase 2b (OTA): the edited build is uploaded, deployed and confirmed, and the board reports its version. Server and dashboard: `just update-e2e`, scenarios `upload-good` and `deploy-good` — the build is uploaded from the dashboard form, sent, and the result card says good | yes |
| 6 | A bad build recovers itself | On the device: `just lib-quickstart`, phase 2c (rollback): a build that never confirms is deployed, and the run requires `rolled_back` and the previous `fw_version` and fails on any `confirmed`. Server and dashboard: `just update-e2e`, scenario `deploy-broken` | yes |
| — | A wrong flash layout is refused, not flashed | Server and dashboard: `just update-e2e`, scenario `precheck-wrong-layout` — a refusal card with no Send, and a direct deploy is a 409 that sends nothing. On the device: `docs/runbooks/agent-qemu.md` → *A wrong flash layout is refused* (a procedure) | server and dashboard half |

One full `just lib-quickstart` run plays segments 1–2, 3, 5 and 6 on the device, in that
order, on one emulated board; the API must give that board `10.0.2.2` origins
(`docs/runbooks/agent-qemu.md` → *The worked example, end to end*). QEMU stands in for two
things a board does by itself: the one USB flash, and the reboot after an update (an
`on_command` apply and a power cycle). `just update-e2e` plays the dashboard against
simulated boards that report the versions they were told, so the `fw_version` assertions
of steps 5 and 6 are graded on the `lib-quickstart` run, never on it.

Step 4 (physical install) and the unaided half of step 3 are done by a person; no harness
plays them and T3 does not grade them.
```

*(b) Patch A. `spec/cujs.md` → CUJ-1 → Judge, step 6 state.* Anchor: the Judge bullet
(lines 109-110 today):

```text
  - After step 6, the device reports `rolled-back` and its `fw_version` is the pre-deploy
    value.
```

Replace it with (`rolled_back` is the deploy state in `device-protocol.md`; (a) already
drops the hyphenated form from the Driver row):

```markdown
  - After step 6, the device reports `rolled_back` and its `fw_version` is the pre-deploy
    value.
```

*(c) Patch A. `spec/cujs.md` → CUJ-1 → Judge, step 5 version source.* Anchor: the Judge
bullet (lines 106-108 today):

```text
  - After step 5, the `fw_version` the device reports equals the version of the build that
    was uploaded — read from the running image's own descriptor, not from what it was told
    to install.
```

Replace it with:

```markdown
  - After step 5, the `fw_version` the device reports equals the version of the build that
    was uploaded — read from the running image itself (its app descriptor, or in a library
    build the version compiled into it and handed to the library at start), not from what
    it was told to install.
```

*(d) Patch A, optional. `spec/cujs.md` → CUJ-1 → Supported By, the dashboard bullet.*
Anchor (lines 81-83 today):

```text
- `standards.md` → *dashboard* → **Getting firmware in is a dashboard operation** —
  step 5's first half. Alex uploads the `.bin` their IDE just built; today that step is
  `docs/runbooks/upload-artifact.sh`, which is outside the journey as written.
```

Replace it with:

```markdown
- `standards.md` → *dashboard* → **Getting firmware in is a dashboard operation** —
  step 5's first half. Alex uploads the `.bin` their IDE just built from the dashboard's
  upload form (`flows.md` Flow 2).
```

**Side finding (not patched).** `spec/standards.md` line 148 also says `rolled-back`; no
judge reads it, so it is left for the owner.

**Named gaps.** T3 lists these, it does not score them.

1. **"A duplicated deploy command produces one download, not two"** (a Judge must-pass) has
   no scripted harness. The dedupe (`ff_mqtt.c`, "duplicate command id=… — ignored (QoS 1
   redelivery)") was shown once by hand in QEMU (`R1-fw-1`, `docs/features/ota-deploy.md`).
   Neither `lib-quickstart` nor `update-e2e` re-publishes a `dn/cmd`. Suggested follow-up,
   **not filed**: in `phase_ota`, re-publish the same `dn/cmd` and require exactly one
   `downloading` step and the `duplicate command` console line. The gap is recorded here and
   in DECISIONS, not in the spec, because the spec is status-free.
2. **The ESP-IDF flavour is compile-only** (row 1–2). No IDF build of the example is booted
   or updated by any harness.
3. **QEMU boots the hybrid build, not the persona binary** (inherited from `R3-fw-3` and
   `R3-fw-4`: the sketch compiled in `lib-qemu/`'s QEMU-bootable hybrid project; the OTA
   artifacts of phases 2b and 2c are that build too).
4. **The wrong-layout row's on-device half is a procedure**, so only its server and
   dashboard half is graded.

### R3 task list — moves into `TODO.md` when R3 opens

_Historical. R3 opened 2026-10-08; every task is done, each with its entry under Completed Work._

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
