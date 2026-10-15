# Fleetforge — TODO

**Goal:** Self-hosted OTA firmware management for embedded fleets (ESP32 first) — a bad
build is caught before the fleet, and any device that gets one recovers itself.
**Updated:** 2026-10-09

## Where this stands

**Works on metal** — device `94a990dd09a4`, an ESP32-S3, against prod (`bingo.tvaroska.sk`):

- **Enroll** (R0, closed 2026-09-22). Browser flash → enrolled → live on the broker in
  13 s. `S0-test-3` passed an unaided run by someone who never saw the code.
  Write-up: [docs/features/enrollment.md](docs/features/enrollment.md).
- **OTA of the agent** (R1, `R1-test-1` passed 2026-09-23). A dashboard-driven deploy
  took the board `0.3.2 → 0.3.1` in ~25 s. Getting there fixed three prod defects that made deploy impossible (`../docs/ops-log.md` F-2026-09-23-001/002/003, all deployed).
  Write-up: [docs/features/ota-deploy.md](docs/features/ota-deploy.md).
- **Auto-rollback of "boots, joins, never confirms"** (2026-09-23). A deliberately broken
  `0.3.2-rbtest` came back on 0.3.1 in 71 s, unattended
  ([docs/runbooks/rollback-test.md](docs/runbooks/rollback-test.md)).

**Not yet, and why it matters:**

- Confirm/rollback is reported only from agent 0.4.0 on (R2-be-1). From then on every deploy
  ends `confirmed` or `rolled_back`.
- An image that boots, gets its announce acked and is broken anyway confirms itself and
  **nothing recovers it**. Roll to **one board at a time**. (The flaky radio is no longer a
  reason: R2-test-2 showed in QEMU that the download and the confirm timer never overlap.)
- A store connection that goes silent mid-download no longer holds the board's update
  slot: since agent 0.4.4 (R2-fw-5) the download fails as `download stalled` 60-80 s after
  the last byte and the next deploy starts (proven in QEMU). A peer that
  goes silent before the first 1 KB of body is still not caught.
- An OTA'd image that hangs before its broker session rolls back by itself since agent
  0.4.3 (R2-fw-4): proven in QEMU.
- Every device has `name: null`. Now `R2b-be-1` / `R2b-fe-6`.
- The operator experience (onboard, update, change Wi-Fi) is specified as three flows in
  `spec/flows.md` (2026-10-04) and is **R2b** below. Almost all of it is dashboard work that
  needs no new protocol; only the known-networks list touches the agent and `ff_cfg`.

**Approach change (2026-10-08): no hardware bench tests.** Every proof is QEMU, the simulator
or a unit test; a task whose acceptance needs a person at a board is not filed. The bench
tasks, scripts and runbooks are gone (`DECISIONS.md` 2026-10-08). **R3 is now open.**

**Was: R2 — safe deploy (verify + auto-rollback), opened 2026-10-03.** The CUJ-1 T3 gate
re-ran and passed on its graded segments (3: enroll, 5: OTA reports the new version); the dashboard
and server half of 6 and of the wrong-layout refusal is `just update-e2e` (R2b-test-3); the
on-device half is still QEMU. `jeep` found nothing assessable in what the
run could show, so the pass rests on the deterministic judge. Task list below; background in
[docs/features/ota-deploy.md](docs/features/ota-deploy.md) → *Phase 2*.

**Blocked:**

- `S0-bug-1` and `R2b-test-1` withdrawn 2026-10-09 (hardware-gated, no-bench approach).

<!-- Counters: spec=1 infra=7 db=1 be=6 fe=7 sec=1 fw=4 test=3 -->
<!-- Sprint 0 counters: fe=8 fw=6 infra=10 test=4 ops=1 bug=4 -->
<!-- R1 counters: be=3 fe=1 fw=2 test=1 -->
<!-- R2 counters: fw=6 be=1 fe=1 test=2 spec=1 (fw-3 is the R1-landed confirm timer) -->
<!-- R3 counters: spec=3 fw=8 be=2 fe=1 test=2 rel=1 -->
<!-- R4 counters: spec=1 fw=4 be=2 fe=2 test=1 -->
<!-- R5 counters: spec=1 fw=7 be=2 fe=1 test=1 -->
<!-- R2b counters: spec=3 fe=13 be=5 fw=1 test=4 -->

Live status lives ONLY here. States: `- [ ]` open · `- [x]` done · `- [!]`
attempted-but-failed. `spec/` and `design/` are status-free.

> Requirements: [spec/prd.md](spec/prd.md) · Wire contract: [spec/device-protocol.md](spec/device-protocol.md) ·
> Flows: [spec/flows.md](spec/flows.md) · Contracts & platform design: [design/architecture.md](design/architecture.md) ·
> Topology & stack: [design/production.md](design/production.md) ·
> Image storage & versioning: [design/artifacts.md](design/artifacts.md)
> Release ladder: [docs/roadmap.md](docs/roadmap.md) · [docs/releases.md](docs/releases.md) ·
> Completed work: [docs/features/](docs/features/) · Decisions: `DECISIONS.md`

> **Task IDs:** fleetforge is release-driven, so IDs are `R{N}-{category}-{number}`
> (for example,`R0-be-1`). Sprint 0 uses `S0-{category}-{number}`.
> Categories: db, be, fe, test, qa, sec, infra, fw, spec, rel, perf, ops.

**Deployment (v1):** single hosted instance at `bingo.tvaroska.sk` (domain reused from
the retired bingo app), single-tenant, **not a public product until V3**.
**Versions:** V1 = R0–R6 (safe OTA, ~5 boards) · V2 = R7–R11 (VCS + compile + simulation)
· V3 = robotic swarm (gateway + drones).

---

## Sprint 0: Critical Issues

Bricking risks, broker auth and security issues get filed here as they surface.

- [x] **S0-bug-1**: Board `94a990dd09a4` did not reach "On the fleet" (cause A fixed in the console) — withdrawn 2026-10-09, not done (hardware-gated; DECISIONS 2026-10-09 withdrawal)
- [x] **S0-infra-10**: Prod's flasher serves agent 0.3.2 (esp32s3) and 0.2.0 (esp32/c3/c6); the repo is at 0.4.5 (P1, 0.25d)
      Found by S0-bug-1 (2026-10-04): `gs://btvaroska/fleetforge/agent/index.json` was last
      published 2026-09-23. Every board flashed from bingo.tvaroska.sk gets a pre-R2 agent
      with none of R2-fw-1...6. Decide first whether the 0.3.x serial baseline is wanted
      (serial-console-bench.md Check E/F talk about a 0.3.1 baseline). If not, `just
      agent-publish-all` to prod per docs/runbooks/artifact-storage.md, *Publishing agent
      bundles*, with the user's go-ahead. Consider making `/release` or `just deploy` warn
      when the published agent_version lags `agent/version.txt`.
      _(2026-10-04: decided, 0.3.x baseline not kept (DECISIONS). Bundles rebuilt at 08de7b9
      (all four 0.4.5, agent-check-fresh green) and published to the dev MinIO only;
      `just agent-check-prod` added and wired into `just build` as a warning. Operator owed
      (needs go-ahead): publish to prod:
      mkdir -p /tmp/no-gcloud-adc && CLOUDSDK_CONFIG=/tmp/no-gcloud-adc OBJECT_STORE_BACKEND=gcs
      GCS_BUCKET=btvaroska GCS_PREFIX=fleetforge/
      GCS_IMPERSONATE_SERVICE_ACCOUNT=fleetforge-artifacts@btvaroska.iam.gserviceaccount.com
      just agent-publish-all ; then `just agent-check-prod` must print CHECK-VERSION OK and
      this task flips to [x].)_
      _(2026-10-04 re-check: bundles still fresh at 08de7b9, prod still 0.2.0/0.3.2; publish still owed, needs go-ahead)_
      _(2026-10-06: done. Bundles rebuilt at 7a58f80 (all four 0.4.7, agent-check-fresh green), published to prod GCS on the user's go-ahead; `just agent-check-prod` printed CHECK-VERSION OK, all four CURRENT.)_
- [x] **S0-test-3**: Someone who did not see the code onboards a board unaided — passed 2026-09-22 → [enrollment.md](docs/features/enrollment.md)
- [x] **S0-fw-3**: A board that browns out during RF calibration cannot escape it — withdrawn 2026-09-23, not fixed → [enrollment.md](docs/features/enrollment.md)
- [x] **S0-bug-2**: An `always_on` device's `announce` event reads `online=False` until its `presence` arrives (P2, 2h) _(done 2026-10-14; see DECISIONS.md)_
      Found 2026-10-14 on `84c7bbe0e8c0` (ingestor log: announce `online=False`, then presence `online=True` in the same second). `presence.is_online` reads `presence_reported`, which only the later `up/presence` sets, and `ingestor/handlers.py` emits the `DeviceEvent` on announce, so the SSE snapshot can flash offline on every connect. `GET /v1/devices` recomputes on read and is right. Fix: on announce, carry the previously reported presence or skip the `online` value until presence is known. Acceptance: a test that an announce for a connected always_on board emits no `online=False`; dashboard shows no offline flicker on connect.
      Added: 2026-10-14
- [x] **S0-bug-3**: Find out why `84c7bbe0e8c0` announced twice with no deploy attached (P2, 2h) _(done 2026-10-14; 22:23:22 was a reboot (cause unknown, probably the operator: console EN pulse or reset), 22:49:26 an MQTT reconnect inside the boot; reset_reason proposed → S0-fw-6; operator question owed; see DECISIONS.md and docs/features/health-telemetry.md)_
      Found 2026-10-14: extra `announce` + `presence` at 22:23:22 and 22:49:26 UTC, between deploys. The operator may have reset or re-plugged the board, or it rebooted on its own, and nothing the board sends tells the two apart (no reset reason in the announce). Ask the operator first; if unexplained, add `reset_reason` (`esp_reset_reason()`) to `up/announce` as an additive field and watch for a repeat. Acceptance: the two events are explained, or a spontaneous reboot is reproducible and filed with its reason.
      Added: 2026-10-14
- [x] **S0-fw-5**: Firmware logs reach the server, not only the serial port (P2, 1d; may belong in R4) _(done 2026-10-14; decided: the build is R4 (R4-FW-3 agent/library, R4-BE-1 ingest + retention, R4-FE-2 log tail), not Sprint 0; up/log payload and a 5 000-line retention bound proposed, spec not applied (owner: Patches A+B); see DECISIONS.md and docs/features/health-telemetry.md)_
      Found 2026-10-14 while bench-testing a custom blink build: `up/log` is listed as "optional diagnostics" and `spec/device-protocol.md` still leaves open whether it ships in R0 or waits for R4, so a maker's firmware log (the `morse:` line) is visible only over USB. Decide whether this stays here or moves to R4, then propose the `up/log` payload and retention as a `spec/` change (spec is protected: file a proposal, do not edit). Related: `docs/SWARM.md` section 5.1 (`age_ms` batched samples).
      Added: 2026-10-14
- [x] **S0-fw-6**: Announce `reset_reason` so a spontaneous reboot names itself (P3, 0.5d) — Patch A applied 2026-10-15 (owner) _(done 2026-10-15; reviewed; agent/library/IDF example announce it, simulator `poweron`/`sw`, ingestor logs it on a live announce, nothing stored; QEMU: `poweron` on --fresh, `panic` after a pre-connect abort() (bootloop image), `sw` after the confirm timer's rollback esp_restart() (hang image). Unprovable in QEMU: `sw` after the APPLY reboot and `panic` after an abort() AFTER connect (any soft reset after the network ran panics before app_main, harness limit); see DECISIONS.md and docs/features/health-telemetry.md)_
      From S0-bug-3: a reboot with no deploy cannot say whether it was the operator or a crash. Proposal and paste-ready Patch A/B: `docs/features/health-telemetry.md` → *Reset reason proposal*. Build: `ff_identity.c` emits it (before `lib_marker`), apply Patch B in the same commit, simulator sends `poweron`/`sw`, ingestor logs it on a live announce (no storage). Acceptance (QEMU, no bench): `just agent-qemu esp32 --fresh` announces `poweron`; after a QEMU OTA reboot it announces `sw`; a build with an injected `abort()` after connect announces `panic` on the next boot; spec-reading tests green. Withdraw it if the operator confirms S0-bug-3's event 1 and nobody needs crash attribution before R4.
      Added: 2026-10-14
- [x] **S0-bug-4**: The api builds a new object-store client on every request: prod `prod-fleetforge-api` sits at 84% of its 256 MiB limit (P1, 0.5d) _(done 2026-10-15; `get_object_store` now builds the store once per process, keyed on the settings the factory reads; see DECISIONS.md and docs/features/infrastructure.md)_
      Found 2026-10-15 from the `monitor-resources.sh` warnings (message says "CPU or memory"; it was memory). Prod api RSS stepped 75 to 218 MiB during the 2026-10-14 bench session and has stayed there. Cause: `api/deps.py::get_object_store` called `create_object_store` per request, so each request built a boto3 session and client (GCS: ADC resolution and an impersonation token) and the factory's "built once" cache lived inside the per-request store. Acceptance (local, v0.6.0 image with the fixed `src` mounted): 50 repeated downloads of a 1 MB agent part stay within +5 MiB (before: 157 / 196 / 243 MiB peak over 15 downloads).
      Added: 2026-10-15

---

## R2: Safe deploy — verify + auto-rollback

Retires bricking, the whole gamble. R2-FW-3 (device-side confirm timer) and most of
R2-TEST-1 landed early in R1 (`ff_mqtt.c`; `docs/runbooks/rollback-test.md`). Since
R2-be-1, agent 0.4.0 reports `confirming` → `confirmed` | `rolling_back` → `rolled_back`
after the reboot.

- [x] **R2-be-1**: Observe confirm/rollback outcome; record it to `deploy_events` (P0, 1d) _(done 2026-10-03; reviewed; see docs/features/ota-deploy.md)_
      Today every deploy parks at `rebooting` with `is_terminal: false`. Needs the agent to
      report `confirming`/`confirmed`/`rolling_back`/`rolled_back` (`ff_ota.h`).
- [x] **R2-fw-1**: Checksum verify before apply, on the real agent (P0, 1d) _(done 2026-10-03; reviewed; see docs/features/ota-deploy.md)_
      Confirm what already landed in R1 before starting; the simulator verifies sha256.
- [x] **R2-fw-2**: A/B slot apply, atomic switch — confirm against the R1 agent (P0, 1.5d) _(done 2026-10-03; reviewed; see docs/features/ota-deploy.md)_
- [x] **R2-fe-1**: Dashboard shows `good` vs `rolled-back` per device (P0, 0.5d) _(done 2026-10-03; see docs/features/ota-deploy.md)_
      Unblocked by R2-be-1: `deploy.state` is now `confirmed`/`rolled_back` with
      `is_terminal: true` (the simulator's `--confirm never` gives you a rolled-back row).
- [x] **R2-test-1**: Remaining failure modes — boot loop, brownout mid-write (P0, 1d)
      _(done 2026-10-03; reviewed; proven in QEMU — boot loop and power cut recover, a hang before the session does not → R2-fw-4; bench replay owed, see docs/runbooks/rollback-test.md; see docs/features/ota-deploy.md)_
      The `0.3.2-rbtest` run covers only "boots, joins, never confirms".
- [x] **R2-fw-4**: Arm the confirm timer before anything in `app_main` can wait forever (P0, 1d)
      _(done 2026-10-03; reviewed; agent 0.4.3, proven in QEMU — a hang before the session rolls back unattended at ~300 s; bench replay owed, see docs/runbooks/rollback-test.md; see docs/features/ota-deploy.md)_
      Found by R2-test-1. The timer is armed in `ff_mqtt_run()`, after `ff_net_bring_up`
      (retries forever), enrollment (retries forever) and every `park()`. An OTA'd image
      that never gets there stays PENDING_VERIFY until someone power-cycles it (QEMU:
      the `-hangtest` image, 330 s, no rollback). CRITICAL path. Replay: `FF_FAULT_TEST=hang`
      in docs/runbooks/agent-qemu.md. Consider `CONFIG_ESP_TASK_WDT_PANIC` too (a
      wedged task today warns and never resets).
- [x] **R2-test-2**: Flaky-Wi-Fi rollback spike (P1, 0.5d)
      _(done 2026-10-03; proven in QEMU — the download and the confirm timer never overlap; a silent peer mid-download holds the update slot for 600 s+ → R2-fw-5; bench replay owed, see docs/runbooks/rollback-test.md; see docs/features/ota-deploy.md)_
      Does a marginal radio stall the download past the confirm timer? Open since R0; it
      was the reason deploys are still one board at a time.
- [x] **R2-fw-5**: A download that stops making progress fails instead of holding the update slot forever (P1, 0.5d)
      _(done 2026-10-03; reviewed; agent 0.4.4, proven in QEMU (esp32) — a silent store ends `failed` / `download stalled` ≈ 80 s after the last byte and the next deploy runs in the same boot; bench replay owed; see docs/features/ota-deploy.md)_
      Found by R2-test-2. A peer that goes silent mid-download (QEMU: proxy blackhole,
      held 600 s) never ends `esp_https_ota_perform`: every 20 s read timeout is
      `-ESP_ERR_HTTP_EAGAIN` → `IN_PROGRESS`. The row parks at `downloading` and every
      new stage is refused ("another update is already in progress") until a power
      cycle. Safe (the running image stays VALID), not live. CRITICAL (`ff_ota.c`). Shape:
      abort after K consecutive empty reads (no bytes for ~60 s) → `failed` /
      `download stalled`. Replay: docs/runbooks/agent-qemu.md → *Driving a flaky link*, D3.
- [x] **R2-fw-6**: A re-delivered in-flight stage must not report `failed` against itself (P2, 0.5d) _(done 2026-10-03; reviewed; proven in QEMU (esp32) S1-S3; agent 0.4.5; see docs/features/ota-deploy.md)_
      Found by R2-test-2 (D3). The agent dedupes on the last command id only, so after any
      other command a re-POST of the running deploy (`reused: true`, same cmd_id) hits
      `ff_ota_start()` → `ESP_ERR_INVALID_STATE` → `failed` / "another update is already
      in progress" for the cmd that is running. The server marks it terminal and drops the
      real outcome when it arrives. CRITICAL (`ff_mqtt.c`/`ff_ota.c`). Shape: if the
      stage's cmd_id is the one in progress, log and ignore it.
- [x] **R2-spec-1**: Propose `rollback_capable` + partition fingerprint in `up/announce` (P1, 0.5d) _(done 2026-10-03; proposal filed, spec not applied; see DECISIONS.md and docs/features/board-profiles.md)_
      Proposal only (`spec/` is protected). Shape: `docs/features/board-profiles.md` step 1
      and `spec/open-questions.md` → *Bootloader attestation on the Arduino path*.

---

## R2b: Operator flows — onboard, update, change Wi-Fi

Opened 2026-10-04 from `spec/flows.md` (Flow 1, 2, 3) and the 2026-10-04 entries in
`DECISIONS.md`. Between R2 (safe deploy, done) and R3 (the library). Alex first, then Marcus;
Siddharth and Sarah are served by API shape; Elena deferred. **Almost all dashboard work, and
nothing here changes the wire protocol except the known-networks list** (`R2b-spec-1`,
`R2b-fw-1`) and the board measurements (`R2b-spec-2`, `R2b-fw-2`). The stock layout stays the default until R3 (Arduino layout and library marker
are R3). Order inside each flow: the one-place status first, then the cards, then the rest.

### Foundations

- [x] **R2b-spec-1**: Propose the known-networks list and `ssid` for `spec/device-protocol.md` (P1, 0.5d) _(done 2026-10-04; reviewed; proposal filed, spec not applied; see DECISIONS.md and docs/features/enrollment.md)_
      Proposal only (`spec/` is protected, and the file is near-frozen: additive only). Shape:
      `ff_cfg` carries a list of networks (SSID + passphrase) and old single-network blobs
      stay readable; `up/announce` gains an optional `ssid` (never the passphrase). Decide:
      how many networks, the selection rule (fixed order or strongest in range), and what a
      board with no known network does. Record in `DECISIONS.md`. Blocks `R2b-fw-1`,
      `R2b-fe-12`, `R2b-be-5`.
- [x] **R2b-spec-2**: Decide `R2-spec-1`: apply, amend or drop (P2, 0.25d) _(done 2026-10-04; reviewed; decided: amend; proposal amended, spec not applied; see DECISIONS.md and docs/features/board-profiles.md)_
      The pre-check warns on `rollback_capable: false` (Flow 2), and that field exists only if
      the 2026-10-03 proposal is applied. Until decided, the warning is skipped.
- [x] **R2b-spec-3**: Spike: how the agent writes its network list, and what "keep identity" needs (P2, 1d) _(done 2026-10-04; findings only, spec not applied; see DECISIONS.md and docs/features/enrollment.md)_
      Findings only, in `docs/features/enrollment.md`. Questions: `ff_cfg` or NVS for a list
      the running agent edits (NVS is lost on "Erase All Flash"); how a re-flash keeps the
      broker credential without burning a token; what Improv over serial needs on the agent.
      Gates any task for "change Wi-Fi without USB", which is not filed yet.

### Flow 1 — Onboard a board

- [x] **R2b-fe-1**: One status strip: UI and API versions, board, firmware, state (P0, 1d) _(done 2026-10-04; see docs/features/enrollment.md)_
      Replaces the footer's split of UI and API versions from the table's firmware column.
      Read-only, from `buildInfo`, `GET /v1/healthz`, and the selected board's device row.
      Acceptance: after a deploy, one glance shows the running UI, API and the board's
      firmware; a stale cached bundle is visible (UI and API differ).
- [x] **R2b-fe-2**: Pre-flight card before a flash (P0, 1d) _(done 2026-10-04; see docs/features/enrollment.md)_
      From `predictDeviceId(chip)` and the device list already fetched: new board, or
      known board with firmware, online state and "re-flashing issues a new token and
      re-enrols it; its current baseline ends". No backend change. Acceptance: flashing a
      known board shows the card first; a new board shows "new board".
- [x] **R2b-fe-3**: Onboarding result card, with plain-language flash failures (P0, 1.5d) _(done 2026-10-04; see docs/features/enrollment.md)_
      One card composed from the console summary, the device row and the versions: device id,
      firmware, link, clock source, enrolled, on the fleet; on failure one cause and one next
      action. Fold in the flash write/verify failure copy: say what to try first (cable,
      port, lower baud) and name the flash chip only when it repeats. Depends on `R2b-fe-1`.
- [x] **R2b-fe-4**: Boot count, reset reason and milestone retraction in the timeline (P1, 1d) _(done 2026-10-04; see docs/features/enrollment.md)_
      Read `frontend/src/boardConsole.ts` first: a boot-boundary detector already exists
      (`RESET_BANNER`, commanded-reset handling), so this may be small. Acceptance: a board
      that reboots during watch shows the count and reason ("rebooted 3x: brownout") and the
      milestones it had reached are retracted.
- [x] **R2b-fe-5**: Watch the console and the server together (P1, 1d) _(done 2026-10-04; see docs/features/enrollment.md)_
      A native-USB reset must not read as "no board": mark Enrolled / On the fleet from the
      device list or `GET /v1/events` even when the console lost the port. Built without waiting
      for the bench: the server's view is the truth either way. It has no bench test.
- [x] **R2b-be-1**: Set a device's name and tag (P1, 0.5d) _(done 2026-10-04; see docs/features/dashboard.md)_
      `name` is in the schema and in `GET /v1/devices`, but nothing sets it, so every device
      is `null`. A `PATCH /v1/devices/{id}`, admin-only. Acceptance: round-trips and shows in
      the table.
- [x] **R2b-fe-6**: Name a board from the result card (P1, 0.5d) _(done 2026-10-04; see docs/features/dashboard.md)_
      Last line of Flow 1. Depends on `R2b-be-1`.
- [x] **R2b-test-1**: Re-run the unaided onboarding test against the new flow — withdrawn 2026-10-09, not done (hardware-gated; DECISIONS 2026-10-09 withdrawal)

### Flow 2 — Update a board

- [x] **R2b-fe-7**: Upload a build from the dashboard (P0, 1.5d) _(done 2026-10-04; see docs/features/dashboard.md)_
      Replaces `docs/runbooks/upload-artifact.sh` as the way in (`spec/standards.md` →
      *dashboard*). `POST /v1/artifact` exists, so this is the form: file, version, target.
      Acceptance: an admin holding only the password uploads a `.bin` and sees it in the
      Deploy dropdown, with no shell.
- [x] **R2b-be-2**: Dry-run pre-check for a deploy (P0, 1d) _(done 2026-10-04; see docs/features/ota-deploy.md)_
      `POST /v1/devices/{id}/deploy` already refuses a mismatched build with a 409, but only
      when sent. Expose the same reasons without sending: layout and slot-size mismatch,
      board offline, sleepy. Returns refusals and warnings as plain sentences. Reuse the
      checks in `api/routers/deploys.py`; one source of truth.
- [x] **R2b-be-3**: Refuse a merged full-flash binary (P1, 1d) _(done 2026-10-04; see docs/features/ota-deploy.md)_
      A `*.merged.bin` carries bootloader and partition table at `0x0`; sending it as an
      app image overwrites them. Detect at upload from the image header and size and name the
      app `.bin` to pick. Nothing detects this today. Needs a fixture of a real merged image
      and a real app image.
- [x] **R2b-fe-8**: Pre-check card on the Deploy cell (P0, 1d) _(done 2026-10-04; see docs/features/dashboard.md)_
      Shows the dry run: current to target, what is refused or warned, "rolls back on its own
      if it never reconnects". Warnings can be overridden; refusals cannot. Depends on
      `R2b-be-2`.
      Overriding a gating warning sends `override: [code]` in the deploy body; `needs_override` marks which (R2b-be-7).
- [x] **R2b-fe-9**: Update timeline with elapsed seconds and stall text (P1, 1.5d) _(done 2026-10-04; see docs/features/dashboard.md)_
      sent, downloading, staged, rebooting, confirming, confirmed, from `deploy.state` and
      `deploy_events`. **No percentage and no progress bar** (`deploy.ts`: `pct` is a
      transition log). A stall says what it is and when the board gives up (60 to 80 s for a
      silent download, R2-fw-5).
- [x] **R2b-fe-10**: Update result card (P1, 1d) _(done 2026-10-04; see docs/features/dashboard.md)_
      Firmware before to after, good or rolled back with the reason, UI and API versions,
      when. Extends R2-fe-1's verdict. Depends on `R2b-fe-1`.
- [x] **R2b-be-4**: Record who sent a deploy (P2, 0.5d) _(done 2026-10-04; see docs/features/ota-deploy.md)_
      Audit record for Flow 2's result card (Marcus). Check what `deploy_events` already
      stores before adding a column; a migration is CRITICAL.
- [x] **R2b-fe-11**: "Send again" after a failure before reboot (P2, 0.5d) _(done 2026-10-04; see docs/features/dashboard.md)_
      Safe because a repeated stage is deduplicated on the board (R2-fw-6). Not offered after
      a rollback.
- [x] **R2b-test-3**: Update flow end to end (P1, 1d) _(done 2026-10-05; see docs/features/ota-deploy.md)_
      Upload from the dashboard, pre-check refuses a wrong-layout build, upload refuses the merged
      binary, deploy a good build to `confirmed`, deploy a deliberately broken one to
      `rolled back`. Extends CUJ-1 steps 5 and 6.
- [x] **R2b-fw-2**: Agent announces `rollback_capable`, `partition_table_sha256`, `flash_chip_size` (P2, 1.5d) _(done 2026-10-05; reviewed; see docs/features/board-profiles.md)_
      CRITICAL (`ff_identity.c::announce_object`, `ff_mqtt.c::classify_txn` confirm path).
      Emit in the spec's key order; persist the observation in NVS; `true` from the
      `TXN_CONFIRMING` branch; give `NEW`-at-target a terminal outcome instead of "stale
      transaction record — discarded" (leaves the deploy at `rebooting`); emit `false` only
      after `R2b-test-5`. Applies Patch B in the same commit. QEMU proof: an OTA'd image
      announces `rollback_capable: true` and the `ab-4m-v1` fingerprint. New agent version.
      Spec applied in 8cb5335 (Patch A). Apply Patch B (announce example keys) in this task's commit. See docs/features/board-profiles.md → *Step 1 wire proposal*.
      Patch B is owed as the owner's `spec:` commit right after this lands, never before (the harness may not edit spec/; DECISIONS 2026-10-05 R2b-fw-2 amends A4).
- [x] **R2b-be-6**: Ingest and store the three board measurements (P2, 1d) _(done 2026-10-05; reviewed; see docs/features/board-profiles.md)_
      `AnnouncePayload`, `ingestor/store.py`, `EnrollRequest` (store, never reject: malformed
      → null + log), `IDENTITY_FIELDS`, `Device` columns + Alembic migration (CRITICAL),
      simulator flags `--rollback-capable {true,false}`, `--partition-sha`,
      `--flash-chip-size`.
      Spec applied in 8cb5335 (Patch A). Patch B not applied here: it goes in only in R2b-fw-2's commit (DECISIONS 2026-10-04 A4). See docs/features/board-profiles.md → *Step 1 wire proposal*.
- [x] **R2b-be-7**: Pre-check and deploy gate on the measurements (P2, 1d) _(done 2026-10-05; reviewed; see docs/features/ota-deploy.md)_
      `deploy_precheck.py`: `partition_table_mismatch` refusal, `rollback_incapable` gating
      warning; `DeployRequest.override`; `PrecheckFinding.needs_override`;
      `SUPPORTED_LAYOUTS` → `{layout: {ota_slot_size, partition_table_sha256}}` with a pin in
      `tests/test_agent_partitions.py`. Depends on `R2b-be-6`.
      Spec applied in 8cb5335 (Patch A). Apply Patch B (announce example keys) in this task's commit. See docs/features/board-profiles.md → *Step 1 wire proposal*.
### Flow 3 — Change the network (known networks)

- [x] **R2b-fw-1**: Agent reads a list of known networks and joins the first it can see (P1, 2d) _(done 2026-10-05; reviewed; see docs/features/enrollment.md)_
      CRITICAL (agent, `ff_cfg`, protocol). Old single-network `ff_cfg` stays readable. Reports
      `ssid` in `announce`. States "no known network in range" and keeps trying. New agent
      version; `just agent-verify` and the QEMU run unchanged. Depends on `R2b-spec-1`
      being accepted.
      Spec applied in 8cb5335 (Patch A). Apply Patch B (announce example keys) in this task's commit. See docs/features/enrollment.md → *Known networks: wire proposal*.
- [x] **R2b-fe-12**: Flasher takes several networks, passphrase field uses the password manager (P1, 1d) _(done 2026-10-05; see docs/features/enrollment.md)_
      `ffcfg.ts` encodes the list; "add another network". The no-browser-storage rule in
      `FlashBoard.tsx` stays: the field is marked for the browser's own password manager,
      and nothing is written to storage. Depends on `R2b-spec-1`.
      Spec applied in 8cb5335 (Patch A). Apply Patch B (announce example keys) in this task's commit. See docs/features/enrollment.md → *Known networks: wire proposal*.
- [x] **R2b-be-5**: Ingest and expose the board's `ssid` and `known_networks` (P1, 0.5d) _(done 2026-10-05; reviewed; see docs/features/enrollment.md)_
      Additive fields on the device read model. Depends on `R2b-spec-1`.
      Spec applied in 8cb5335 (Patch A). Apply Patch B (announce example keys) in this task's commit. See docs/features/enrollment.md → *Known networks: wire proposal*.
- [x] **R2b-fe-13**: Fleet row shows the network: "on: shed", "knows 2 networks" (P1, 0.5d) _(done 2026-10-05; see docs/features/enrollment.md)_
      An offline board shows "offline, last on: shed": the server cannot tell out of range
      from powered off (R2b-spec-1). Only the result card, which reads the console, names
      "none of its 2 known networks is in range".
      Depends on `R2b-be-5`.
---

## R3: Thin OTA library + first CUJ

Opened 2026-10-08. The four-verb contract as something a maker embeds in **their own**
firmware: ESP-IDF component, Arduino library, a worked example, the first CUJ end to end.
Plan substance, the spike result (`R3-fw-1`, layout `ab-4m-arduino-v1`) and the CUJ
(`R3-spec-1`) are in [docs/features/ota-library.md](docs/features/ota-library.md).
**No hardware: every acceptance below is QEMU, the simulator, a compile or a unit test.**
Order: spec-3 → fw-2 → fw-3/fw-4 → fw-5/fw-6/be-1 → test-1 → spec-2 → test-2 → rel-1.
`spec/` and `design/` are protected: spec tasks file proposals, the owner applies them.

### Spec (proposals)

- [x] **R3-spec-3**: Decide how the library marker is encoded in the app binary (P1, 0.5d) _(done 2026-10-08; reviewed; decided: scanned 64-byte marker constant, announced as lib_marker; proposal filed, spec not applied; see DECISIONS.md and docs/features/ota-library.md)_
      Open since `spec/flows.md` (R2b). Candidates: a string in `esp_app_desc_t`, a section
      the library links in, or a constant the pre-check can find in the image. It must be
      readable by the server at upload (`R3-be-1`) and survive both ESP-IDF and Arduino builds.
      Record in `DECISIONS.md` and file the `spec/` proposal. Blocks `R3-fw-6`, `R3-be-1`.
- [x] **R3-spec-2**: Propose the CUJ-1 Driver table for R3 (P1, 0.5d) _(done 2026-10-09; reviewed; proposal filed, spec not applied; see DECISIONS.md and docs/features/ota-library.md)_
      Steps 1-2 (sketch compiles with the library) and 6 (bad build recovers) get harnesses
      (`R3-fw-4`, `R3-test-1`); step 5 names `just update-e2e` for the server and dashboard
      half (DECISIONS 2026-10-05 proposal). Proposal only; `spec/cujs.md` is protected.
      Depends on `R3-test-1`.
      Per R3-fw-4: the row 1-2 harness is `just lib-quickstart --build-only` (the README blocks, verbatim, in a clean temp tree: Arduino esp32/esp32s3 + build B, ESP-IDF esp32/esp32s3).
      Per R3-test-1: the row 6 harness is `just lib-quickstart` (full run, `phase_rollback`: an `FF_ROLLBACK_TEST` build of the sketch). Pass condition: deploy `rolled_back`, `fw_version` = the previous build, no `confirmed` in the steps (the run fails on any).

### Firmware

- [x] **R3-fw-2**: Extract the protocol into an ESP-IDF component (P1, 2d) _(done 2026-10-08; reviewed; see docs/features/ota-library.md)_
      `agent/main/` already separates protocol from demo app: `ff_ota`, `ff_mqtt`,
      `ff_enroll`, `ff_cfg`, `ff_store`, `ff_identity`, `ff_net`, `ff_time`. Move them to
      a component with an `idf_component.yml`. The agent becomes its first consumer and
      must keep passing `just agent-verify` and the QEMU E2E unchanged.
      The real work is deciding the **public** surface — whatever ships is additive-only
      from then on, exactly like the wire protocol. Keep it to the four verbs, enroll,
      announce/heartbeat, and a version accessor. CRITICAL (confirm timer, A/B apply move).
      Acceptance: the agent builds from the component with no behavior change, the QEMU
      run in `docs/runbooks/agent-qemu.md` still passes. The component's public
      headers are a strict subset of what `agent_main.c` uses.
- [x] **R3-fw-3**: Arduino library wrapping the same C (P1, 2d) _(done 2026-10-08; reviewed; see docs/features/ota-library.md)_
      Depends on `R3-fw-2`. The persona writes Arduino or PlatformIO and does not use
      ESP-IDF (`docs/personas/PERSONAS.md` §1). Ships layout **`ab-4m-arduino-v1`** as a
      sketch-local `partitions.csv` that travels with the **example** (the prebuild hook
      reads only the sketch folder), config still in a flashable `ff_cfg`
      (`design/decisions/arduino-gets-its-own-layout-id.md`). The library must read the same
      known-networks list as the agent (`R2b-fw-1`), or an OTA to the maker's firmware
      strands the board. The stock core is already `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`;
      a configuration that cannot roll back must fail at build or enroll, not warn.
      Acceptance: Arduino-framework builds (PlatformIO on the dev box, `pio` is installed)
      of the example for esp32 and esp32s3 compile; the esp32 build enrolls and heartbeats
      in QEMU against the dev stack.
      Per R3-spec-3 (side finding, not fixed): an Arduino build's `esp_app_desc_t.version` is the core's IDF string (`esp-idf: v4.4.7 38eeba213a` on core 2.0.17), so `fw_version` and the upload form's version pre-fill are wrong unless the library supplies the maker's version.
- [x] **R3-fw-4**: The worked example — enroll → heartbeat → stage → report version (P1, 1d) _(done 2026-10-09; reviewed; see docs/features/ota-library.md)_
      Small enough to read in one screen: the PRD's Morse-code blinker, which changes its
      message between two builds so "the OTA worked" is visible in the log and the
      dashboard version. Depends on `R3-fw-2`, `R3-fw-3`.
      Per R3-fw-3: build on `agent/components/fleetforge/examples/Basic/` (the sketch, its
      `partitions.csv` and `platformio.ini`); QEMU runs through `lib-qemu/` + `just lib-bundle` / `just lib-qemu`.
      Acceptance: builds unmodified from a clean checkout on both ESP-IDF and Arduino
      (PlatformIO); the README quickstart is exactly the steps a reader follows, and a
      scripted run of those steps passes.
- [x] **R3-fw-5**: Reject a wrong flash layout loudly (P1, 1d) _(done 2026-10-09; reviewed; see docs/features/ota-library.md)_
      A build that does not reproduce a supported layout exactly must announce a different
      `partition_layout`, and the deploy refusal must tell a library user what to fix.
      **Do not use the IDE's `Maximum is N bytes` line as the check** (R3-fw-1 measured it
      wrong). Depends on `R3-fw-3`.
      Acceptance: a deliberately mismatched layout (simulator or QEMU, no board) triggers a
      refusal at deploy time naming the expected layout and slot size; the library does not
      start its OTA path on a bootloader without rollback.
- [x] **R3-fw-6**: Library build carries the marker and announces it (P1, 1d) _(done 2026-10-09; reviewed; Patch B applied; see docs/features/ota-library.md)_
      CRITICAL (announce is protocol). Implements `R3-spec-3`; additive field only. Depends
      on `R3-spec-3`, `R3-fw-2`.
      Acceptance: the QEMU example announces the marker; the stock agent and every pre-R3
      board announce none and are not refused for it (`null` never warns).
      Per R3-spec-3: the marker lives in the component, so the stock agent built from it carries and announces it too; "announce none" means agents ≤ 0.4.7. Applies Patch B (the lib_marker key in the up/announce example) in this task's commit.
      Per R3-be-2: `ff_identity.c`'s comment "pinned equal to SUPPORTED_LAYOUTS" (and `agent/tools/lib_bundle.py`'s) should now say BUILTIN_LAYOUTS (renamed; not edited in R3-be-2 to keep bundles fresh).
- [x] **R3-fw-7**: PlatformIO recipe (P2, 0.5d) _(done 2026-10-09; see docs/features/ota-library.md)_
      A working `platformio.ini` for the example, no registry publication (out of scope).
      Depends on `R3-fw-3`. Acceptance: `pio run` on a clean checkout builds both targets.
      Per R3-fw-3: the harness `examples/Basic/platformio.ini` exists (pioarduino 55.03.312-1 pinned,
      esp32 + esp32s3; needs PlatformIO Core >= 6.2.0). This task owns the clean-checkout recipe and README.
- [x] **R3-fw-8**: Arduino IDE package of the library (P2, 0.5d) _(done 2026-10-09; reviewed; see docs/features/ota-library.md)_
      The component layout (`include/` + `src/`) is invisible to the Arduino IDE, which compiles
      and includes `src/` only, so R3-fw-3 ships no `library.properties` (DECISIONS 2026-10-08).
      Add a flattening package step (one `src/` with the public headers, `library.properties`,
      the example with its `partitions.csv`) and prove it with `arduino-cli compile` of the
      example for esp32 and esp32s3 on core 3.3.12. Depends on `R3-fw-3`.

### Backend and dashboard

- [x] **R3-be-1**: Pre-check: a binary without the library marker is a gating warning (P1, 1d) _(done 2026-10-09; reviewed; see docs/features/ota-library.md)_
      The second gating code after `rollback_incapable` (`deploy_precheck.py`,
      `GATING_CODES`, `tests/test_deploy_precheck.py::TestOverrideCodes`). Detect from the
      image at upload. Not for the stock agent's own bundles. Depends on `R3-spec-3`.
      Acceptance: a plain sketch `.bin` warns and needs `override: [code]`; a library build
      and an agent bundle do not; unit tests pin all three.
      Per R3-spec-3: code no_library_marker (gating); scan rule and magic in docs/features/ota-library.md → *Library marker proposal*; artifacts uploaded before this have no verdict and never warn.
      Per R3-fw-6: agent bundles built from R3-fw-6 on carry the marker (`ff_marker.c`, `lib_version` = `FF_LIB_VERSION`), as do the Arduino and ESP-IDF library builds; a sketch with the library installed but never called carries none (gc-sections). The simulator never sends `lib_marker`. The reference reader in the proposal matched every library build (agent ×4, Basic esp32/esp32s3, the QEMU bundle, basic_idf) and found no marker in the 0.4.7 bundles or `tests/fixtures/firmware/*.bin`; the ingestor and enroll still ignore the key.
- [x] **R3-be-2**: Partition profile table with detected and user-defined entries (P2, 3d) _(done 2026-10-09; reviewed; see docs/features/board-profiles.md)_
      Board-profiles step 2 ([board-profiles.md](docs/features/board-profiles.md)): the
      `partition_profiles` table seeded from the catalog, `builtin` immutable vs `user`,
      a `detected` profile created in pending state from an unknown id with a valid
      fingerprint. Decide per-fleet vs global first (open in that doc). Alembic migration
      is CRITICAL. Depends on `R3-fw-3` (the second real layout).
      Acceptance: `SUPPORTED_LAYOUTS` is read from the table; both layouts seeded; an
      unknown announced layout lands as `detected` and is not deployable until adopted.
      Per R3-fw-5: a board on a map its firmware does not know announces the reserved id `unknown` (never a profile name), so a detected profile keys on `partition_table_sha256`; until adopted it is refused as `unsupported_layout` (`deploy_precheck.py`).
- [x] **R3-fe-1**: Dashboard adopts or names a detected profile (P2, 1d) _(done 2026-10-09; see docs/features/board-profiles.md)_
      Depends on `R3-be-2`. Acceptance: in `just update-e2e` style Chromium run, a detected
      profile shows, can be named, and then accepts a deploy.
      Per R3-be-2: the API is `GET/POST/PATCH/DELETE /v1/partition-profiles` (PATCH = adopt by naming, `{layout_id, ota_slot_size?}`; 409/422 details are plain text for the banner); the precheck has `device_partition_profile` (an adopted board still announces `unknown` in `device_partition_layout`); the upload form must offer catalog ids (adopted profiles), not `d.partition_layout`; there is no new SSE type, so re-read profiles on `device.announce`; `api.ts` needs the mirror types.

### Test and release

- [x] **R3-test-1**: E2E in QEMU — example firmware enrolls, updates, rolls back (P1, 1d) _(done 2026-10-09; see docs/features/ota-library.md)_
      The library gets the same proof as the agent: `docs/runbooks/agent-qemu.md` boots the
      real bundle against the dev stack. Three runs: a clean enroll, an OTA to a second
      build whose visible behavior differs, and a deliberately broken build that rolls back
      unaided and reports `rolled-back`. Depends on `R3-fw-4`.
      Acceptance: all three pass with no board; the rollback run fails the test if the
      device reports `confirmed`.
      Per R3-fw-4: runs one and two exist as `just lib-quickstart` (`phase_enroll`, `phase_ota` in `scripts/lib_quickstart.py`); add the broken build as `phase_rollback` after `phase_ota` (a QEMU bundle that cannot confirm, `apply: "on_command"`, power cycle; expect `rolled_back`, fail on `confirmed`). Each run pays one ~11 min hybrid compile in the fresh `lib-qemu/`.
      Per R3-fw-5: the rollback run uses the example's own `partitions.csv` (a wrong table announces `unknown` and is refused before any stage; `tests/fixtures/wrong-layout-partitions.csv` is for that refusal only). `ff_ota.c` now fails the build without `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`.
- [x] **R3-test-2**: Play CUJ-1 end to end with every segment gradeable (P1, 0.5d) _(done 2026-10-09; deterministic judge PASS on 1-2, 3, 5, 6; jeep PASS, no hard-fail trap; see docs/features/ota-library.md)_
      The T3 gate (`/verify`) with the segments from `R3-spec-2` applied by the owner.
      Depends on `R3-test-1`, `R3-spec-2`. Acceptance: deterministic judge passes on steps
      1-3, 5, 6; any segment still without a harness is listed, not scored.
      Per R3-test-1: steps 1-2, 5 and 6 on the library path are all one `just lib-quickstart` run (~25 min; needs the api on 10.0.2.2 origins).
      Per R3-spec-2: rows and graded halves in docs/features/ota-library.md → *CUJ-1 Driver proposal*; one full `just lib-quickstart` grades 1-2, 3, 5, 6 on the device, `just update-e2e` the dashboard half of 5 and 6 and the wrong-layout row; the duplicated-command assertion has no harness (list it, do not score it).
      Per R3-be-1: `just update-e2e`'s synthetic builds now carry a marker (`LIB_MARKER` in update-flow-e2e.mjs); a plain build gates as `no_library_marker`.
- [x] **R3-rel-1**: Quickstart, archive and release (P1, 0.5d) _(done 2026-10-09; local only: v0.5.0 tagged, not pushed; publish and deploy are the owner's; see docs/features/ota-library.md)_
      README quickstart for the library, `docs/features/ota-library.md` completed entries,
      `agent/version.txt` bump, `/release fleetforge minor`, then
      `just agent-publish-all` + `just agent-check-prod` **only with the owner's go-ahead**.
      Depends on everything above.
      Per R3-fw-7: the README's `git clone` step needs the library on GitHub `main` (origin is behind); once the release tag exists, add the pinned git form `lib_deps = Fleetforge=https://github.com/tvaroska/fleetforge.git#vX.Y.Z` to *Your own project*. PlatformIO finds the nested `library.json`, probed with `git+file://` in R3-fw-7.
      Per R3-fw-4: link the two example READMEs (`examples/Basic/README.md`, `examples/basic_idf/README.md`) from the library README; keep their `# quickstart:` blocks intact (`just lib-quickstart` runs them).
      Per R3-fw-6: after the `agent/version.txt` bump (and `library.json` + `ff_lib_version.h` with it), agents > 0.4.7 announce `lib_marker: 1` and carry the marker; until then dev-built 0.4.7 agents do too, while the released 0.4.7 (`v0.4.3`) announces none.
      Per R3-be-1: migration 0008 (`artifacts.has_lib_marker`); release note: artifacts uploaded before it have no verdict and never warn until re-uploaded (the re-upload fills it); the downgrade drops the verdicts.

### Order

1. `S0-bug-1` (Sprint 0). 2. `R2b-fe-1`, `-2`, `-3`, `-7` and `R2b-be-2`, `-3` (no dependencies).
3. `R2b-fe-8`..`fe-10`, `R2b-fe-4`, `R2b-fe-5`, `R2b-be-1`/`fe-6`. 4. `R2b-spec-1`, then `R2b-fw-1`,
`fe-12`, `be-5`, `fe-13`; `R2b-spec-2`'s follow-ups (`be-6`, `be-7`, `fw-2`) once the owner
applies Patch A; 5. `R2b-test-1` last. `R2b-spec-3` can run any time and gates future Improv work.

---

## R4: Health & telemetry view

Opened 2026-10-15 from the focus-group report (`~/Desktop/focus_group_consensus_report.md`,
synthetic personas; comparison and what was deferred: `DECISIONS.md` 2026-10-15). Fleet
visibility: what a board is doing, why it last reset, and what it logged. Plan substance:
[docs/features/health-telemetry.md](docs/features/health-telemetry.md) (*up/log proposal*,
*reset_reason as built*). **No hardware: every acceptance is QEMU, the simulator, a compile
or a unit test.** `spec/` is protected: spec tasks file proposals, the owner applies them.
Order: spec-1 (any time) · fw-1 → be-1 → fe-1 → test-1 · be-2 · fw-3 → fe-2 · fw-2 · fw-4.

### Spec (proposals)

- [x] **R4-spec-1**: Propose the `proto: 1` additive reservations (P1, 0.5d) _(done 2026-10-15; proposal only: Patch A+C owner (`spec:` commit), Patch B in R4-fw-1's commit; device_id widening noted, not built; dry-run green; see DECISIONS.md and docs/features/health-telemetry.md)_
      From the report's item 9 and `docs/SWARM.md` 5.2-5.4: absent `artifact.type` on `dn/cmd` `stage` means `"app"` (reserve `mcu_fw`, `model_weights`, `fpga_bitstream` and `up/announce.sub_versions`); optional `reason` and `expected_back_s` on `up/presence`; `"dock"` in `link_type`; document `min_free_heap` in `up/hb`. Paste-ready patches in `docs/features/health-telemetry.md`, one section, same shape as the `reset_reason` proposal; a dry-run that the spec-reading tests stay green. Proposal only, nothing built. The `device_id` format widening (`^[a-z0-9_-]{1,32}$`) is a server schema change: note it, do not build it.
      Added: 2026-10-15

### Firmware

- [x] **R4-fw-1**: `up/hb` carries `min_free_heap` and a boot-success flag (P0, 1d) _(done 2026-10-15; reviewed; see docs/features/health-telemetry.md)_
      `esp_get_minimum_free_heap_size()` beside `free_heap` (the leak signal a crash-looping board lacks); `boot_ok` true once the confirm path has run. Additive to `up/hb`; agent, ESP-IDF example and Arduino library through the same code. Simulator sends both. Acceptance (QEMU, no bench): the QEMU hb carries both keys; a spec-reading test pins them; `just agent-build-all` within the flash budget.
      Apply R4-spec-1 Patch B (the hb example gains `min_free_heap`) in this commit, with a test that parses the spec's hb example.
      Added: 2026-10-15
- [ ] **R4-fw-2**: User-defined telemetry channel with relative sample age (P1, 1.5d)
      `up/telemetry` batches `{"samples":[{"age_ms","name","value"}]}`; the server computes `sample_ts = received_at - age_ms` (`docs/SWARM.md` 5.1), so no wall clock on the board. A RAM ring buffer flushes on reconnect. Public API (`ff_telemetry()` / `Fleetforge.metric()`) is additive-only from release, so settle the names in a proposal first. CRITICAL (library public surface). Acceptance (simulator and QEMU): samples taken while disconnected arrive with their original spacing after reconnect.
      Added: 2026-10-15
- [ ] **R4-fw-3**: Forward firmware log lines on `up/log` (P0, 1.5d) — **needs the owner to apply `S0-fw-5` Patches A+B first**
      `ff_logf(level, fmt, ...)` and `Fleetforge.logf()`, explicit opt-in (customer privacy, cellular caps); batches under 2048 B, at most one publish per 5 s, `text` <= 200 B, `dropped` count. An optional compile-time `esp_log` sink, level-filtered and guarded against `ff-mqtt`/`mqtt_client`/`mbedtls` recursion, for debug and test builds only. Basic and basic_idf forward their `morse:` line. Acceptance (QEMU and simulator): the morse line arrives in the server's log table with an `age_ms`-derived time; a flood produces `dropped`, not a stalled heartbeat.
      Added: 2026-10-15
- [ ] **R4-fw-4**: Crash-surviving log tail (P2, 1d)
      Keep the last few lines in a magic-and-CRC-guarded `RTC_NOINIT_ATTR` buffer so lines written just before a `panic` or `task_wdt` flush on the recovery boot. A named gap in the up/log proposal. Acceptance (QEMU): an injected `abort()` three lines after a log call delivers those lines after reboot. The `FF_FAULT_TEST` images and the runbook's QEMU soft-reset limit apply; say plainly which part was proven.
      Added: 2026-10-15

### Backend and dashboard

- [ ] **R4-be-1**: Persist and serve health, reset reason and logs (P0, 2d)
      Migration (CRITICAL): `devices.last_reset_reason` plus rolling fault counts for `panic`/`task_wdt`/`brownout`, a log table bounded to the newest 5 000 lines per device and 7 days, telemetry 30 days (SPEC retention). `GET /v1/devices` and the `device.announce` SSE frame carry the reset reason; the ingestor stops being log-only for it. Acceptance: integration tests for ingest, bound-on-write and retention; `just test` green; the migration upgrades and downgrades on a populated dev DB.
      Added: 2026-10-15
- [ ] **R4-be-2**: `device.deploy` SSE events carry the transaction (P1, 0.5d)
      Add `cmd_id`, `state` and terminal `detail` to the `DeviceEvent` payload so a CI or test runner can wait on an event instead of polling `GET /v1/devices`. Additive; an old frontend must still render it. Acceptance: `tests/test_events_stream.py` pins the keys; `just update-e2e` still passes.
      Added: 2026-10-15
- [ ] **R4-fe-1**: Live fleet status (P0, 2d)
      Online/offline, last seen, boot success, last reset reason as a warning badge, `min_free_heap`, metrics. Acceptance: jsdom tests, and a real Chromium run against the simulator showing a `panic` reset badge.
      Added: 2026-10-15
- [ ] **R4-fe-2**: Per-device log tail (P1, 1d)
      Newest first, level, text, `dropped` gaps. Depends on R4-be-1. Acceptance: jsdom, and Chromium against the simulator.
      Added: 2026-10-15

### Test

- [ ] **R4-test-1**: A metric and a log line emitted on a device reach the dashboard live (P0, 0.5d)
      Simulator and QEMU, no bench. Depends on R4-fw-2, R4-fw-3, R4-be-1, R4-fe-1.
      Added: 2026-10-15

---

## R5: Custom self-test confirm, command seam and safe window

Opened 2026-10-15 (focus-group report, as R4). Catches "boots but the app is broken", and
adds the verbs the protocol already names but the agent ignores. `ff_mqtt.c` logs "not
implemented" for `reboot`/`rollback`/`apply`/`cancel`, parses `confirm_timeout_s` and
ignores it, and reboots on a confirm timeout with no safe-window check, which
`docs/SWARM.md` calls out for anything that flies. Plan substance:
[docs/features/self-test.md](docs/features/self-test.md). Every firmware task below is
CRITICAL (confirm timer, rollback path, A/B apply): stronger model and a review before commit.
Order: spec-1 → fw-1 → fw-2 → fw-3 → fw-4 → fw-5 → be-1 → be-2 → fe-1 → test-1 · fw-6, fw-7 last.

### Spec (proposals)

- [ ] **R5-spec-1**: Propose the R5 wire and API semantics (P0, 1d)
      The self-test hold and its structured `detail` (<= 64 B, `selftest failed: <reason>; returned to ota_x`); `confirm_timeout_s` honoured; the `apply`/`reboot`/`rollback`/`cancel` verbs and their acks; the safe-window predicate as the gate on **both** `apply` (`awaiting_safe_window`) and a confirm-timeout rollback reboot; `safe_mode` and `power_class: sleepy` helpers. Proposal only, paste-ready patches, spec-reading tests dry-run.
      Added: 2026-10-15

### Firmware

- [ ] **R5-fw-1**: Self-test entrypoint contract (P0, 1d)
      `ff_selftest_fn` / `Fleetforge.onSelfTest(fn)`, additive-only public surface. Acceptance (compile, QEMU): the Basic and basic_idf examples register one.
      Added: 2026-10-15
- [ ] **R5-fw-2**: Hold slot confirmation until the self-test passes; fail rolls back with a reason (P0, 1.5d)
      `esp_ota_mark_app_valid_cancel_rollback()` waits for the callback within `confirm_timeout_s`; the failure string is kept in `ff_txn` NVS and reported in `up/status.detail`. Acceptance (QEMU): a build whose self-test fails comes back on the old slot with the reason on the wire.
      Added: 2026-10-15
- [ ] **R5-fw-3**: Honour `dn/cmd.confirm_timeout_s` (P1, 0.5d)
      Persist it in `ff_txn` NVS and arm the confirm timer with it on the OTA boot; 300 s when omitted. Acceptance (QEMU): a `30` s command rolls a hung image back at about 30 s.
      Added: 2026-10-15
- [ ] **R5-fw-4**: Implement `apply`, `reboot`, `rollback` and `cancel` (P0, 2d)
      Release a `staged` `apply: "on_command"` slot; the other verbs per R5-spec-1. Acceptance (QEMU): each verb drives the documented state change and ack; `apply` on nothing staged is refused with a reason.
      Added: 2026-10-15
- [ ] **R5-fw-5**: Safe-window predicate gates apply and the rollback reboot (P0, 1.5d)
      `ff_set_safe_window_cb` / `Fleetforge.onSafeWindow(fn)`. While it says unsafe, `apply` reports `awaiting_safe_window` and a confirm-timeout rollback reboot is deferred. Decide and test what a board does when unsafe lasts longer than the confirm window. Acceptance (QEMU): unsafe defers both; safe releases them.
      Added: 2026-10-15
- [ ] **R5-fw-6**: Rapid-crash safe mode (P2, 1d)
      Three `panic`/watchdog resets within 60 s on an already-confirmed slot hold the board in an OTA-only recovery loop, without running the sketch. Acceptance (QEMU).
      Added: 2026-10-15
- [ ] **R5-fw-7**: Sleepy lifecycle helpers (P2, 1d)
      `Fleetforge.isConnected()`, `Fleetforge.isReadyForSleep()` and the `power_class: "sleepy"` configuration in `Fleetforge.h`.
      Added: 2026-10-15

### Backend and dashboard

- [ ] **R5-be-1**: Deploys honour the self-test result (P0, 1d)
- [ ] **R5-be-2**: REST endpoints for `apply`, `reboot`, `rollback` and `cancel` (P1, 1d)
      Under `deploys.py` / `devices.py`, behind the admin credential, validated against the device's announced capabilities.
- [ ] **R5-fe-1**: Dashboard shows the self-test result, the rollback `detail` and Apply/Reboot controls (P1, 1d)

### Test

- [ ] **R5-test-1**: Failing self-test rolls back; `apply` waits for the safe window (P0, 1d)
      QEMU and simulator, no bench. Depends on R5-fw-2, R5-fw-4, R5-fw-5, R5-be-1.
