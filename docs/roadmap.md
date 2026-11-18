# Roadmap (`fleetforge`)

Source specs: [prd.md](../spec/prd.md) · [design/architecture.md](../design/architecture.md) · [production.md](../design/production.md) · [flows.md](../spec/flows.md) · [device-protocol.md](../spec/device-protocol.md) · [releases.md](releases.md) · [DECISIONS.md](../DECISIONS.md). There is no `TODO.md`; all upcoming milestones and tasks live here.

## Where we are

`V1` (`R0`–`R6`) and `V2` `R7`–`R8` are shipped: Web Serial enrollment, A/B auto-rollback (`ab-4m-v1`), embeddable ESP-IDF/Arduino OTA library, live health & crash-ring telemetry, custom self-test confirmation (`ff_set_selftest`), ECDSA P-256 firmware signing with resumable HTTP `Range` downloads, artifact provenance API, `fleetforge push` CLI + CI workflow templates, and multi-project RBAC with scoped `ffa_...` tokens. Next up: complete `V2` (`R9` advisory simulation gate, `R10` server-side container build pipeline, `R11` staged cohort deploy policy), then `V3` robotic swarm hierarchy and delta OTA.

## Milestones at a glance

| # | Milestone | Done when | Target |
| :--- | :--- | :--- | :--- |
| **R9** | Advisory Simulation Gate (`V2`) | `pytest-embedded` + Espressif QEMU boots uploaded artifacts, executes the `R5` self-test behind the pluggable `sim-runner` contract, and surfaces warn/override in the UI. | `V2` (`P1`, Medium) |
| **R10** | Server-Side Build Pipeline (`V2`) | Server clones a repo ref, compiles in pinned ESP-IDF toolchain containers (`esp32`/`S3`/`C3`/`C6`) with queued builds, streamed logs, and build cache. | `V2` (`P1`, Large) |
| **R11** | Staged Cohort Rollout & Deploy Policy (`V2`) | Cohort-based staged rollout policies gate automatic promotions from canary to fleet, closing `V2`. | `V2` (`P1`, Medium) |
| **V3** | Robotic Swarm Hierarchy & Delta OTA | UGV edge-relay gateway (`parent_device_id`), groups & bulk deploy, airtime-aware scheduling, and delta OTA updates work end-to-end. | `V3` (`P2`, Large) |

## Milestone R9 — Advisory Simulation Gate (`V2`)

- [ ] **R9-1: Pluggable `sim-runner` contract & Espressif QEMU worker** — Implement the `sim-runner` contract ([design/architecture.md](../design/architecture.md), [features/simulation.md](features/simulation.md)) using `pytest-embedded` + Espressif QEMU to boot uploaded firmware artifacts and run the `R5` self-test before deploy (`done when`: a deliberately broken test build fails the sim gate while a valid build passes in automated tests).
- [ ] **R9-2: Advisory warn & operator override in API and dashboard** — Persist simulation verdicts on artifacts and surface advisory warnings with explicit operator override in `src/fleetforge/` and `frontend/` (`done when`: UI shows pass/warn status and records operator override in audit logs).

## Milestone R10 — Server-Side Build Pipeline (`V2`)

- [ ] **R10-1: Pinned ESP-IDF toolchain containers & queued build worker** — Clone a configured repo ref and compile artifacts in pinned single-tenant ESP-IDF containers per target (`esp32`, `esp32s3`, `esp32c3`, `esp32c6`) with build caching ([features/build-pipeline.md](features/build-pipeline.md)) (`done when`: a queued build produces a signed, provenance-tagged `.bin` stored in `ObjectStore`).
- [ ] **R10-2: Streamed build logs & dashboard trigger** — Stream live build logs over SSE and expose build status/retry controls in the API and dashboard (`done when`: end-to-end test verifies queued → running → succeeded/failed transitions and streamed log delivery).

## Milestone R11 — Staged Cohort Rollout & Deploy Policy (`V2`)

- [ ] **R11-1: Staged cohort deploy policy engine** — Add branch/tag-to-cohort deploy rules with health/rollback promotion gates ([features/vcs-integration.md](features/vcs-integration.md)) (`done when`: a failing canary cohort halts promotion to the next wave automatically).

## Milestone V3 — Robotic Swarm Hierarchy & Delta OTA

- [ ] **V3-1: UGV edge-relay gateway & device hierarchy (`parent_device_id`)** — Support parent/child device topology and local edge artifact caching on a UGV gateway ([features/groups-deploy.md](features/groups-deploy.md)) (`done when`: child nodes fetch staged artifacts through their gateway parent).
- [ ] **V3-2: Groups, bulk deploy, airtime-aware scheduling & delta OTA** — Add group-targeted waves, airtime-aware scheduling, and binary delta updates for constrained links (`done when`: group rollout and delta patch verification pass integration tests).

## Unscheduled backlog

- **MCP `2026-07-28` operator adapter:** Read-only status/crash-ring resources + `SEP-2663` Tasks for cohort rollouts + `MRTR` confirmation.
- **Thread (802.15.4) mesh support:** IPv6-native MQTT/HTTPS over Thread for 100+ node low-power swarms (ESP32-C6/H2 hardware path).
- **Gateway-mediated non-IP radios:** Zigbee, BLE, and LoRa relay adapters behind the device-facing waist.
- **Security & provisioning extensions:** Secure Boot v2 (new devices only), SoftAP provisioning, per-device mTLS, and self-host TLS onboarding without public DNS.
- **Platform & observability extensions:** Raspberry Pi adapter and Grafana/Prometheus telemetry export.

## Shipped

- **`V1` (`R0`–`R6`)** — Web Serial enrollment, OTA deploy, A/B auto-rollback, embeddable ESP-IDF/Arduino OTA library, live health & crash-ring telemetry, custom self-test confirm, and ECDSA P-256 signing + resumable HTTP `Range` downloads — [archive](archive/roadmap-history.md#v1-r0-to-r6-safe-ota-on-a-handful-of-boards)
- **`V2` (`R7`–`R8`)** — Artifact provenance API, `fleetforge push` CLI, CI workflow templates, multi-project RBAC, and scoped `ffa_...` tokens — [archive](archive/roadmap-history.md#v2-r7-and-r8-artifact-provenance-and-push-ingestion)
