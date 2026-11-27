# Roadmap (`fleetforge`)

Specs: [prd.md](../spec/prd.md) · [architecture.md](../design/architecture.md) · [production.md](../design/production.md) · [flows.md](../spec/flows.md) · [device-protocol.md](../spec/device-protocol.md) · [releases.md](releases.md) · [DECISIONS.md](../DECISIONS.md). Every open task sits in a milestone below (no `TODO.md`; bare Python paths under `src/fleetforge/`, `ff_*.{c,h}` under `agent/components/fleetforge/src/`).

## Where we are

`V1` (`R0`–`R6`), `V2` (`R7`–`R11`), `V3` (server + C decoder), and `v0.9.3`–`v0.9.5` (`60` shipped audit/security, full-stack CI/Playwright E2E, monolith/contract/hygiene, and staged production release items archived in [roadmap-history.md](archive/roadmap-history.md)) are live in production (`v0.9.5` on `bingo.tvaroska.sk`). **Milestone 1 (`v0.9.5` hardware sign-off)** tracks the remaining physical ESP32 NimBLE board verification (`D-8-hw`); **Milestones 2–4 (`v0.10.0`–`v1.0.0`)** hold the 14 net-new feature items.

## Milestones at a glance

| # | Milestone | Done when | Target |
| :--- | :--- | :--- | :--- |
| 1 | **v0.9.5** Hardware NimBLE & Production Release Sign-Off | Physical ESP32 NimBLE check (`D-8-hw`) passes on target silicon (`v0.9.5` Cloud Build, `--from-prod-backup` staging, and GKE promotion shipped in `R-095`). | `v0.9.5` (1 item) |
| 2 | **v0.10.0** Firmware Heap Hardening & Provisioning Security | Agent ships dynamic mbedTLS freeing, DRAM fragmentation metrics, static tasks, QEMU heap poisoning, Secure Boot v2, SoftAP, mTLS, and local TLS. | `v0.10.0` (8 items) |
| 3 | **v0.11.0** Operator MCP, Prometheus Observability & Linux Agent | Prometheus/Grafana metrics, scoped MCP `2026-07-28` rollout tasks with `MRTR` confirm, and a Linux A/B OTA agent pass E2E verification. | `v0.11.0` (3 items) |
| 4 | **v1.0.0** Beyond V3 — On-Device Delta Swarm, Thread Mesh & Non-IP Radios | On-device `ffdelta-v1` apply + MQTT/ACL edge-relay daemon, OpenThread `802.15.4` mesh (`ESP32-C6`/`H2`), and BLE/Zigbee/LoRa proxies pass E2E. | `v1.0.0` (3 items) |

## Milestone 1 — v0.9.5: Hardware NimBLE & Production Release Sign-Off

Closes the remaining physical-board gate for `v0.9.5` (`R-095` staged release and the 59 `v0.9.3`–`v0.9.5` implementation items are archived in [roadmap-history.md](archive/roadmap-history.md)).

- [ ] **D-8-hw (P1) · Physical ESP32 target-board NimBLE verification** — Flash `agent/` (`0.7.0`) onto an ESP32 target board and verify `ff_net_wifi_improv_authorize()`, encrypted GATT writes (`BLE_GATT_CHR_F_WRITE_ENC` + `desc.sec_state.encrypted`), `3` opens/boot + `600s` cooldown, and `ff_cfg` priority over NVS on real NimBLE silicon (`ff_net_wifi.c`, `ff_net_wifi_improv.c`; host-`gcc` check `pytest -q tests/test_agent_verify.py -k improv` passes `1 passed` today).

## Milestone 2 — v0.10.0: Firmware Heap Hardening & Provisioning Security

Hardens ESP32 DRAM resilience and expands hardware security and zero-DNS self-hosted onboarding without altering `ab-4m-v1` flash-time immutables on existing boards.

- [ ] **F-1 · Reference agent dynamic mbedTLS buffer freeing** — Port `CONFIG_MBEDTLS_DYNAMIC_BUFFER=y`, `CONFIG_MBEDTLS_DYNAMIC_FREE_CONFIG_DATA=y`, and `CONFIG_MBEDTLS_DYNAMIC_FREE_CA_CERT=y` from `templates/esp-idf/sdkconfig.defaults` into `agent/sdkconfig.defaults` (`test_ci_cd.py` guard). Verify: `git grep -c "CONFIG_MBEDTLS_DYNAMIC_" -- agent/sdkconfig.defaults` returns `3` (`0` today).
- [ ] **F-2 · End-to-end DRAM fragmentation telemetry (`largest_free_block`)** — Report `largest_free_block` (`heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)`) alongside `min_free_heap` in `ff_identity.c` (`up/hb`), gate it in `ingest/handlers.py` & `deploy_policy_gates.py`, and surface warnings in `frontend/src/DeviceInspector.tsx`. Verify: `test_device_health.py` & `test_deploy_policy.py` pass on PG17 (`0` skips).
- [ ] **F-3 · Steady-state `cJSON` churn reduction & static FreeRTOS task allocation** — Cut heartbeat/telemetry heap churn and migrate tasks in `ff_mqtt.c` (`ff_hb`), `ff_log.c`, `ff_ota.c`, and `ff_sec.c` to explicit `heap_caps` stacks + `xTaskCreateStatic`. Verify: `git grep -c "xTaskCreateStatic(" -- agent/components/fleetforge/src/ff_log.c agent/components/fleetforge/src/ff_ota.c agent/components/fleetforge/src/ff_sec.c` returns `1` per file (`0` today).
- [ ] **F-4 · Heap poisoning & stack-canary gates in debug/QEMU simulation** — Enable `CONFIG_HEAP_POISONING_COMPREHENSIVE=y` and stack canaries in debug/QEMU profiles (`agent/`, `simulation.py`) so `R9` boots fail on heap/stack corruption. Verify: `tests/test_simulation_gate.py` catches a seeded corruption vector.
- [ ] **S-1 · Opt-in Hardware Secure Boot v2 profile for new ESP32 enrollments** — Add Secure Boot v2 signing recipes and Web Serial eFuse safeguards (`agent/`, `templates/esp-idf/`, `frontend/src/esptoolFlasher.ts`, `docs/runbooks/agent-build.md`) scoped to newly flashed boards. Verify: `test_agent_verify.py` & `frontend/src/flash.test.tsx` pass.
- [ ] **S-2 · Authorize-gated SoftAP fallback Wi-Fi provisioning adapter** — Add `ff_net_softap.c` behind `ff_net_adapter.h` for browsers without Web Bluetooth Improv, keeping physical authorization, boot-open caps, and `ff_cfg` precedence over NVS. Verify: host-`gcc` tests in `tests/test_agent_verify.py` pass.
- [ ] **S-3 · Optional per-device mTLS across Mosquitto & HTTPS artifact downloads** — Support X.509 client-certificate enrollment, rotation, and revocation across `broker/`, `api/routers/enroll.py`, `frontend/nginx.conf`, and `ff_mqtt.c` / `ff_ota.c`. Verify: TLS client-cert tests in `test_api_devices.py` & `test_api_artifact_download.py` pass.
- [ ] **S-4 · DNS-free local-CA TLS onboarding (`fleetforge tls init`)** — Add local root-CA + SAN leaf generation in `cli_commands.py` (`docker/`, `docs/runbooks/dev-stack.md`) and bake the trust anchor into enrollment `ff_cfg` blobs for LAN self-hosting. Verify: `test_cli.py` & `scripts/selftest_window_e2e.py` pass over a local-CA endpoint.

## Milestone 3 — v0.11.0: Operator MCP, Prometheus Observability & Linux Agent

Extends User-Facing and Device-Facing **Ports, Adapters and Contracts** to Prometheus/Grafana observability, operator MCP workflows, and Linux SBCs.

- [ ] **P-1 · Prometheus `/metrics` endpoints & Grafana dashboard bundle** — Expose authenticated Prometheus metrics on `api` and `ingestor` (`api/`, `ingestor/main.py`) covering wave latency, confirm/rollback counters, broker lag, and `[a-z][a-z0-9_]{0,30}` device telemetry, with Grafana JSON in `docker/observability/`. Verify: `tests/test_device_health.py` verifies exposition format and label hygiene.
- [ ] **P-2 · MCP `2026-07-28` operator adapter (`SEP-2663` Tasks & `MRTR` confirmation)** — Add `mcp/` exposing read-only status/KPI/crash-ring resources, `SEP-2663` async Tasks for cohort rollouts, and mandatory `MRTR` confirmation tokens before production mutations under scoped `ffa_...` RBAC. Verify: `tests/test_mcp_adapter.py` & `tests/test_isolation_guard.py` pass (`absent` today).
- [ ] **P-3 · Raspberry Pi / Linux A/B OTA agent adapter** — Build `linux_agent/` implementing `announce`, `stage`, `apply`, `ff_set_selftest` confirm, slot auto-rollback, ECDSA P-256 verification, and HTTP `Range` resume with zero server core changes. Verify: stage -> confirm and rollback tests in `tests/test_linux_agent.py` pass (`absent` today).

## Milestone 4 — v1.0.0: Beyond V3 — On-Device Delta Swarm, Thread Mesh & Non-IP Radios

Completes device-side `V3` swarm OTA and answers the 100+ node low-power scale question across IPv6 Thread meshes and gateway-mediated non-IP radios.

- [ ] **V3-1 · Streaming on-device `ffdelta-v1` apply & production UGV edge-relay daemon** — Wire streaming `ff_delta.c` reconstruction + SHA-256/ECDSA check into `ff_ota.c` with fallback to full `artifact.url` (closing `D-21`), and ship `edge_relay/` + `broker/dynsec.py` providing cached HTTP artifacts, child MQTT republishing, and per-child `dynsec` ACLs. Verify: host-`gcc` + QEMU/Mosquitto E2E tests in `tests/test_v3_swarm_and_delta.py` pass.
- [ ] **V3-2 · Thread (`802.15.4`) IPv6 mesh adapter (`ESP32-C6` / `ESP32-H2`)** — Add `ff_net_thread.c` behind `ff_net_adapter.h`, `ESP32-C6`/`H2` `templates/esp-idf/` profiles, and Border Router docs enabling 100+ sleepy/mesh nodes over native IPv6 MQTTS + airtime-capped `ffdelta-v1` OTA. Verify: `ESP32-C6`/`H2` container compile + 100-node rollout simulation in `tests/test_v3_swarm_and_delta.py` pass.
- [ ] **V3-3 · Gateway-mediated non-IP radio proxy adapters (BLE, Zigbee, LoRa)** — Add radio proxy Ports & Adapters in `edge_relay/radios/` translating `announce`, chunked `ffdelta-v1` `stage`, `status`, and `cmd` over BLE GATT, Zigbee, and LoRa onto the MQTT/HTTPS Device-Facing Waist. Verify: `@pytest.mark.port` suite `tests/contracts/test_radio_relay_contracts.py` passes (`absent` today).

## Shipped

- **`V1` (`R0`–`R6`)** — Web Serial enrollment, OTA deploy, A/B auto-rollback, embeddable ESP-IDF/Arduino OTA library, live health & crash-ring telemetry, custom self-test confirm, and ECDSA P-256 signing + resumable HTTP `Range` downloads — [archive](archive/roadmap-history.md#v1-r0-to-r6-safe-ota-on-a-handful-of-boards)
- **`V2` (`R7`–`R8`)** — Artifact provenance API, `fleetforge push` CLI, CI workflow templates, multi-project RBAC, and scoped `ffa_...` tokens — [archive](archive/roadmap-history.md#v2-r7-and-r8-artifact-provenance-and-push-ingestion)
- **v0.8.6 (shipped in v0.9.2):** The gate fails when the database-backed tests cannot run; `T-1`, `A-2`, `A-1` and `D-1`/`A-8` are fixed with regression tests that run in it; the `D-2` checks are run and recorded. — [archive](archive/roadmap-history.md#v086-shipped-in-v092)
- **v0.8.7 (shipped in v0.9.2):** The fifteen P1 review findings in its section are fixed, each with its named test passing with PostgreSQL up and 0 skipped. — [archive](archive/roadmap-history.md#v087-shipped-in-v092)
- **R9 Advisory Simulation Gate (shipped in v0.9.2):** `EspressifQemuSimRunner` + `sim_worker` boots uploaded artifacts in Espressif QEMU when a QEMU worker is enabled, captures the `R5` self-test verdict, and surfaces warn/override in the UI. — [archive](archive/roadmap-history.md#r9-advisory-simulation-gate-shipped-in-v092)
- **R10 Server-Side Build Pipeline (shipped in v0.9.2):** `builder` worker clones a repo ref and compiles in pinned, networkless ESP-IDF toolchain containers (`esp32`/`S3`/`C3`/`C6`) when a Docker-enabled builder worker runs, with queued builds, streamed logs, and build cache. — [archive](archive/roadmap-history.md#r10-server-side-build-pipeline-shipped-in-v092)
- **R11 Staged Cohort Rollout & Deploy Policy (shipped in v0.9.2):** Cohort-based staged rollout policies gate automatic promotions from canary to fleet, closing `V2`. — [archive](archive/roadmap-history.md#r11-staged-cohort-rollout-deploy-policy-shipped-in-v092)
- **V3 Robotic Swarm Hierarchy & Delta OTA (shipped in v0.9.2 — server side and standalone C decoder only):** UGV edge-relay gateway API (`parent_device_id`, `/relay`, `/relay-sync`), groups & bulk deploy, airtime-aware scheduling, and `ffdelta-v1` delta generation + standalone C decoder (`ff_delta.c`); on-device delta OTA and gateway MQTT republishing/per-child broker ACLs remain planned. — [archive](archive/roadmap-history.md#v3-robotic-swarm-hierarchy-delta-ota-shipped-in-v092)
- **v0.9.3 Audit and Security Hardening (shipped in v0.9.5):** All 32 review and audit items (A-21–A-35, D-21–D-28, T-16–T-26) verified on PostgreSQL 17 (0 DB skips) and Docker ESP-IDF v5.5.5 + Espressif QEMU — [archive](archive/roadmap-history.md#v093-audit-and-security-hardening-shipped-in-v095)
- **v0.9.4 Full-Stack CI, Browser E2E and Pre-Release Hardening (shipped in v0.9.5):** Locked Alembic upgrades, full-stack + 4 Chromium Playwright CI suites, @pytest.mark.port contracts, and 14 firmware/edge/server security fixes (physical ESP32 NimBLE check D-8-hw tracked in Milestone 1) — [archive](archive/roadmap-history.md#v094-full-stack-ci-browser-e2e-and-pre-release-hardening-shipped-in-v095)
- **v0.9.5 Monolith Decomposition, Contract Codegen and Test Hygiene (shipped in v0.9.5):** Split all 11 monoliths below 800 LOC, added OpenAPI + wire/capability contract codegen, wired 78.9%/93.1% coverage and pytest -n auto DB isolation, ran ASan/UBSan + 10/10 mutation sweeps, and promoted `v0.9.5` live on GKE (`R-095`) — [archive](archive/roadmap-history.md#v095-monolith-decomposition-contract-codegen-and-test-hygiene-shipped-in-v095)

