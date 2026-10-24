# Groups, Cohort Rollouts & the Swarm Gateway

**Priority:** P1  
**Status:** Phase 1 (Groups & Staged Cohort Rollouts) **Shipped in V1 / CUJ-2 (`v0.7.2+`)**; Phases 2 & 3 scoped to **V3 — robotic swarm**  
**Depends on:** OTA Deploy & Auto-Rollback (`ota-deploy.md`) — R2, Multi-Project RBAC (`projects-and-users.md`) — P1, and Continuous Deployment (`vcs-integration.md`) — CUJ-2 / R11  
**Scope:** [`spec/prd.md`](../../spec/prd.md) → *Scope* · [`docs/releases.md`](../releases.md) → *R11 / V3 — robotic swarm*

## Evolution from V1 to V3

V1 initially targeted **~5 heterogeneous boards** — an e-paper frame, a vehicle, a drone, a morse
blinker: five projects, five different builds, where per-device capability checking mattered far
more than bulk deployment.

However, as multi-project support and the e-ink fleet workflow expanded (`CUJ-2`), single operators
began managing multiple devices per project. Consequently, **Phase 1 (Groups schema, group assignment,
and staged canary cohort rollouts)** was pulled forward into **V1 / CUJ-2 (`v0.7.2+`)**.

The remainder of this feature area — **Phase 2 (Swarm Gateway edge relay & hierarchy)** and
**Phase 3 (Airtime-aware radio scheduling & binary delta updates)** — remains scheduled for
**V3 — robotic swarm**.

## Multi-Use-Case × 10s of MCUs Architecture (2026-10-17 Design Notes)

When a single operator runs **multiple distinct use-cases** (e.g., *Greenhouse Climate*, *Irrigation Valves*, *Solar Telemetry*, *E-Ink Frames*) with **20–80+ microcontrollers per use-case**:

1. **Two-Level Boundary (`projects` vs. `device_groups`):**
   - **Use-Case / Application Boundary = `projects`** ([`projects-and-users.md`](projects-and-users.md)): Scopes `artifact_versions` to `(project_id, target, version)` so two use-cases that both run `esp32c6` can each upload `1.5.0` without `409 Conflict` and a board can never be offered firmware from a different use-case.
   - **Intra-Use-Case Cohort / Ring = `device_groups`** (`UNIQUE (project_id, name)`): Within a single project/use-case, groups represent release rings (`canary`, `prod`) or physical sub-locations (`zone-north`, `zone-south`). Both `FlashBoard` and `EnrollBoard` should allow binding minted `enrollment_tokens` to a `group_id` so batches of newly flashed boards land in their cohort automatically on first boot.
2. **Staged Cohort Rollout ("Canary → Bounded Wave" Instead of 40 Per-Row Clicks):**
   - Clicking `Deploy` → `Send` row-by-row breaks down past ~10 boards. A project rollout (`POST /v1/projects/{slug}/deploy`, plus CI auto-deploy on `POST /v1/artifact` and ref-switch auto-deploy on `PATCH /v1/projects/{slug}`) executes server-side across three state-driven rules in `deploy_version_to_project_devices` (`api/routers/deploys.py`):
     - **Wave 1 (Canary Gate):** When `canary=True` (default) and no board in the project has confirmed `version` yet, deploy to 1 canary board first (preferring online `is_dev`, then online `always_on` boards) and hold the remaining cohort (`waiting for canary <id> to confirm <version>`) until a board reports `confirmed`.
     - **Wave 2 (Bounded Concurrency):** Once a canary has confirmed `version`, roll out to the remaining $N$ boards with a concurrency cap (`max_parallel = 5` by default) so 40–80 ESP32s pulling a ~1.2 MB image simultaneously do not saturate a local 2.4 GHz Wi-Fi AP.
     - **Auto-Halt on Rollback / Failure:** If any board in the project cohort enters `rolled_back` or `failed` on `version`, halt immediately (`rollout halted: device <id> <state> on version <version>`) and stage nothing further.

## Phase 1: Groups & Staged Cohort Rollouts (Shipped in V1 / CUJ-2)

| ID | Task | Priority | Status | Description |
|----|------|----------|--------|-------------|
| SW-DB-1 | Tags/groups schema + device↔group membership | P0 | **Done** | `device_groups` table with `UNIQUE (project_id, name)` and `devices.group_id` / `enrollment_tokens.group_id` foreign keys (`alembic/versions/0001_initial_schema.py`). |
| SW-BE-1 | Assign devices to groups/tags (API) | P0 | **Done** | CRUD endpoints in `src/fleetforge/api/routers/groups.py` and `PATCH /v1/devices/{id}` group binding. |
| SW-BE-2 | Deploy to a group & staged cohort rollouts | P0 | **Done** | `POST /v1/projects/{slug}/deploy` with `group_id` cohort filter, Wave 1 Canary Gating, Wave 2 Bounded Concurrency (`max_parallel=5`), and automatic halt on rollback/failure (`src/fleetforge/api/routers/deploys.py`). |
| SW-FE-1 | Group management UI + group deploy | P0 | **Done** | Cohort management in `ProjectAccess.tsx`, target group selector in `Dashboard.tsx` (`UploadedBuilds`), and group assignment in `FleetView.tsx`, `EnrollBoard.tsx`, and `FlashBoard.tsx`. |
| SW-FE-2 | Per-device progress view for a bulk deploy | P0 | **Done** | `DeployKpiStrip`, `DeployCell`, and `DeployTimeline` progress visibility across multiple boards. |
| SW-TEST-1 | E2E & integration verification | P0 | **Done** | `tests/test_ci_cd.py` staged cohort rollout verification, group filtering, and canary gating. |

**Done when:** operators can organize devices into cohort groups, deploy firmware to a group with automated canary gating, and monitor rollout progress live.

## Phase 2: Gateway & hierarchy — ground vehicle + drones (V3 — robotic swarm)

One ground vehicle acts as **gateway** for a large number of flying drones. It is a
*mobile, in-fleet* node, not static on-site infrastructure: simultaneously a managed
device in the registry and the parent of its drones.

- **Gateway = edge relay**, not a radio protocol translator: local broker + artifact
  cache + upstream sync. Pull an artifact once over the uplink, serve it N times
  locally — which also collapses the airtime cost of a swarm update.
- **Disconnected operation** is the defining requirement. The vehicle will be out of
  internet range in the field.
- **Hierarchy:** `parent_device_id` (reserved in the R0 schema). The server tracks each
  drone individually but reaches it via its parent, and never assumes it holds an MQTT
  session per device.
- **Link:** if the vehicle runs a Wi-Fi AP (or ESP-NOW), the drones stay IP-bearing and
  no protocol bridging is necessary at all. Zigbee/BLE/LoRa bridging is a separate,
  later question.

## Phase 3: Scale hardening (V3 — robotic swarm)

- **Airtime-aware scheduling** — concurrency limits so a fleet deploy does not saturate
  the local link.
- **Delta updates** — 5–50 KB diffs against ~1.5 MB images, applied read-active-slot →
  write-inactive-slot. Thus, the A/B layout frozen at R0 is a prerequisite.
- **Safe-window deferral at scale** — drones must not apply mid-flight. The
  device-owned-reboot rule (design/architecture.md principle 5) is load-bearing here.
