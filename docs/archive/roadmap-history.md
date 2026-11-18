# Roadmap History (`fleetforge`)

Archived shipped releases (`V1` `R0`–`R6` and `V2` `R7`–`R8`) and reference context moved out of [`../roadmap.md`](../roadmap.md) on `2026-11-18`. Full release descriptions live in [`../releases.md`](../releases.md) and task history in [`../features/`](../features/).

---

## V1 (R0 to R6) Safe OTA on a Handful of Boards

| Release | Theme | Risk retired | Feature area |
| :--- | :--- | :--- | :--- |
| **R0** | Enroll a board (UI + recognition + flash + connect) | Onboarding, recognition, device-server connection | [enrollment](../features/enrollment.md) |
| **R1** | Upload new code (OTA deploy) | OTA transport works end-to-end | [ota-deploy](../features/ota-deploy.md) |
| **R2** | Safe deploy: check + auto-rollback | Bricking (device-side confirm timer + A/B slot) | [ota-deploy](../features/ota-deploy.md) |
| **R3** | Thin OTA library + first CUJ | Embeddable ESP-IDF/PlatformIO/Arduino OTA library | [ota-library](../features/ota-library.md) |
| **R4** | Health & telemetry view | Fleet visibility, live status & crash log ring | [health-telemetry](../features/health-telemetry.md) |
| **R5** | Custom self-test confirm | Catches boots-but-broken app logic (`ff_set_selftest`) | [self-test](../features/self-test.md) |
| **R6** | Signed OTA + resumable hardening (`v1`) | ECDSA P-256 signing + resumable HTTP `Range` downloads | [security-hardening](../features/security-hardening.md) |

---

## V2 (R7 and R8) Artifact Provenance and Push Ingestion

| Release | Theme | Feature area |
| :--- | :--- | :--- |
| **R7** | Artifact API + provenance (`repo`/`commit`/`tag`/`build_url`/`test_passed`) | [vcs-integration](../features/vcs-integration.md) |
| **R8** | Push ingestion (`fleetforge push` CLI + Cloud Build / GitHub Actions templates + multi-project RBAC & scoped `ffa_...` tokens) | [vcs-integration](../features/vcs-integration.md), [projects-and-users](../features/projects-and-users.md) |

---

## Capability Areas & Growth-Stage Context

| Area | Feature file | Spans releases |
| :--- | :--- | :--- |
| Enrollment & provisioning | [enrollment.md](../features/enrollment.md) | R0 |
| Dashboard (cross-cutting UI: theme, typography, a11y) | [dashboard.md](../features/dashboard.md) | R0+ |
| OTA deploy & auto-rollback | [ota-deploy.md](../features/ota-deploy.md) | R1, R2 |
| Board profiles (layout catalog + measured attestation) | [board-profiles.md](../features/board-profiles.md) | R2, R3 |
| Health & telemetry | [health-telemetry.md](../features/health-telemetry.md) | R4 |
| Self-test (sim gate + device confirm) | [self-test.md](../features/self-test.md) | R5 |
| Signing & resumable hardening | [security-hardening.md](../features/security-hardening.md) | R6 (`v1`) |
| VCS integration & deploy policy | [vcs-integration.md](../features/vcs-integration.md) | R7, R8, R11 |
| Simulation backend | [simulation.md](../features/simulation.md) | R9 (`V2`) |
| Build pipeline (server-side compile) | [build-pipeline.md](../features/build-pipeline.md) | R10 (`V2`) |
| Groups, gateway, hierarchy, delta | [groups-deploy.md](../features/groups-deploy.md) | V3 (swarm) |
| Projects, multi-user auth & scoped tokens | [projects-and-users.md](../features/projects-and-users.md) | V2 |

### Scale Note (Thread) & Growth-Stage Risks

- **Thread (802.15.4 mesh):** A 100+ node low-power swarm exceeds Wi-Fi AP limits; Thread is IPv6-native so MQTT and HTTPS run unchanged (requires ESP32-C6/H2 hardware). Pair with delta updates, airtime-aware scheduling, and the V3 edge cache.
- **Growth-stage risks:** (1) Scale ceiling on Wi-Fi/802.15.4 bandwidth (mitigated by abstract `esp_netif`/`link_type`); (2) Simulation does not catch RF/hardware timing bugs (`R9` is advisory-only, paired with canary); (3) Sim-engine lock-in (mitigated via pluggable `sim-runner` contract); (4) Server-side build is arbitrary code execution (`R10` stays single-tenant); (5) Self-host TLS onboarding without public DNS (deferred).
