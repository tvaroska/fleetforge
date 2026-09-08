# Fleetforge — Roadmap

**Last Updated:** 2026-09-08 (project intake — pre-R0)
**Purpose:** Strategic overview of the release ladder and capability areas.
**Source specs:** [SPEC.md](SPEC.md) · [DESIGN.md](DESIGN.md) · [FLOWS.md](FLOWS.md) · [RELEASES.md](RELEASES.md)

## Strategic Vision

**Safe remote firmware updates for a fleet of ESP32 devices, from a self-hosted
server** — where "safe" means a bad build is caught *before* the fleet, and any device
that does get a bad update recovers itself. Architected around **two thin waists**:

1. **Device-facing:** opaque, versioned artifact + a 4-verb update contract
   (`stage → apply → confirm → rollback`). Lets ESP32 → Pi → FPGA reuse one core.
2. **User-facing:** headless, API-first core (stable public API + MQTT event stream).
   Every UI — including the built-in dashboard — is just a client.

**Success metrics (track both):**
- **Delivery success** = healthy AND running the intended new version (rollback = *miss*).
- **Fleet safety** = device ends healthy on *some* version (rollback = *save*).

---

## Release ladder

Each release is a functioning, demoable app that does one more thing end-to-end,
ordered to retire the biggest risk (bricking) first.

### Path to v1

| Release | Theme | Risk retired | Status | Feature area |
|---------|-------|--------------|--------|--------------|
| **R0** ⭐ | Enroll a board (UI + recognition + flash + connect) | Onboarding, recognition, device↔server connection | 🔨 Active | [enrollment](features/enrollment.md) |
| R1 | Upload new code (OTA deploy) | OTA transport works end-to-end | 📋 Planned | [ota-deploy](features/ota-deploy.md) |
| R2 ⭐ | Safe deploy: verify + auto-rollback | **Bricking** (the whole gamble) | 📋 Planned | [ota-deploy](features/ota-deploy.md) |
| R3 | Groups & bulk deploy | Fleet-scale targeting | 📋 Planned | [groups-deploy](features/groups-deploy.md) |
| R4 | Health & telemetry view | Fleet visibility | 📋 Planned | [health-telemetry](features/health-telemetry.md) |
| R5 | Custom self-test confirm | "boots but app logic broken" | 📋 Planned | [self-test](features/self-test.md) |
| R6 | Advisory simulation gate | Bad builds before any device is touched | 📋 Planned | [simulation](features/simulation.md) |
| R7 | Signed OTA + resumable hardening → **v1** | Production-grade safety + security | 📋 Planned | [security-hardening](features/security-hardening.md) |

### Path to v2 (VCS integration)

| Release | Theme | Status | Feature area |
|---------|-------|--------|--------------|
| R8 | Artifact API + provenance | 📋 Planned | [vcs-integration](features/vcs-integration.md) |
| R9 | GitHub Action (push ingestion) | 📋 Planned | [vcs-integration](features/vcs-integration.md) |
| R10 | Per-group deploy policy → **v2** | 📋 Planned | [vcs-integration](features/vcs-integration.md) |

### Beyond v2

Canary / staged rollout (unlocks *safe* auto-deploy) · Raspberry Pi adapter ·
SoftAP provisioning · per-device mTLS.

**Interaction surfaces** (enabled by the API-first core): CLI · Home Assistant
(add-on + MQTT Discovery `update` entities) · Claude Code / MCP (read-rich, guarded
writes) · Grafana/Prometheus export.

---

## Capability areas

| Area | Feature file | Spans releases |
|------|--------------|----------------|
| Enrollment & provisioning | [enrollment.md](features/enrollment.md) | R0 |
| OTA deploy & auto-rollback | [ota-deploy.md](features/ota-deploy.md) | R1, R2 |
| Groups & bulk deploy | [groups-deploy.md](features/groups-deploy.md) | R3 |
| Health & telemetry | [health-telemetry.md](features/health-telemetry.md) | R4 |
| Self-test (sim gate + device confirm) | [self-test.md](features/self-test.md) | R5 (defined R0) |
| Simulation backend | [simulation.md](features/simulation.md) | R6 |
| Signing & resumable hardening | [security-hardening.md](features/security-hardening.md) | R7 |
| VCS integration | [vcs-integration.md](features/vcs-integration.md) | R8–R10 |

## Key risks (from SPEC.md)

1. **Onboarding friction** (self-hosted broker + certs) → one-command Compose + prebuilt agent.
2. **Bad pushes on flaky Wi-Fi** → resumable download, integrity check, atomic A/B, mandatory rollback handshake.
3. **Wrong abstraction guess** for Pi/FPGA → keep the contract minimal and opaque.
4. **Sim ≠ reality** → sim is advisory-only; canary is the planned next layer.
5. **Sim-engine lock-in / licensing** → keep sim pluggable behind the runner contract.
