# Fleetforge — Roadmap

**Last Updated:** 2026-09-08 (project intake — pre-R0)
**Purpose:** Index of the release ladder and capability areas, plus the growth-stage
questions that are deliberately not v1's problem.
**Source specs:** [prd.md](../spec/prd.md) · [design/architecture.md](../design/architecture.md) · [architecture.md](../design/production.md) · [flows.md](../spec/flows.md) · [device-protocol.md](../spec/device-protocol.md) · [releases.md](releases.md)

> This file is an **index**. Requirements and targets live in [prd.md](../spec/prd.md);
> release contents in [releases.md](releases.md); tasks in `features/*.md`.

## Strategic vision

**Safe remote firmware updates for a fleet of ESP32 devices** — where "safe" means a bad
build is caught *before* the fleet, and any device that does get a bad update recovers
itself. Architected around **two thin waists**:

1. **Device-facing:** opaque, versioned artifact + a 4-verb update contract
   (`stage → apply → confirm → rollback`). Lets ESP32 → Pi → FPGA reuse one core.
2. **User-facing:** headless, API-first core (stable public API + SSE event stream).
   Every UI — including the built-in dashboard — is just a client.

---

## Release ladder

Each release is a functioning, demoable app that does one more thing end-to-end,
ordered to retire the biggest risk (bricking) first. Contents in [releases.md](releases.md).

### V1 — safe OTA on a handful of boards

| Release | Theme | Risk retired | Status | Feature area |
|---------|-------|--------------|--------|--------------|
| **R0** ⭐ | Enroll a board (UI + recognition + flash + connect) | Onboarding, recognition, device↔server connection | 🔨 Active | [enrollment](features/enrollment.md) |
| R1 | Upload new code (OTA deploy) | OTA transport works end-to-end | 📋 Planned | [ota-deploy](features/ota-deploy.md) |
| R2 ⭐ | Safe deploy: verify + auto-rollback | **Bricking** (the whole gamble) | 📋 Planned | [ota-deploy](features/ota-deploy.md) |
| R3 | Health & telemetry view | Fleet visibility | 📋 Planned | [health-telemetry](features/health-telemetry.md) |
| R4 | Custom self-test confirm | "boots but app logic broken" | 📋 Planned | [self-test](features/self-test.md) |
| R5 | Signed OTA + resumable hardening → **v1** | Production-grade safety + security | 📋 Planned | [security-hardening](features/security-hardening.md) |

### V2 — source to artifact: build & pre-flight verification

| Release | Theme | Status | Feature area |
|---------|-------|--------|--------------|
| R6 | Artifact API + provenance | 📋 Planned | [vcs-integration](features/vcs-integration.md) |
| R7 | Push ingestion (GitHub Action + templates) | 📋 Planned | [vcs-integration](features/vcs-integration.md) |
| R8 | Advisory simulation gate *(moved out of v1)* | 📋 Planned | [simulation](features/simulation.md) |
| R9 | Build from source (server-side compile) | 📋 Planned | [build-pipeline](features/build-pipeline.md) |
| R10 | Deploy policy → **v2** | 📋 Planned | [vcs-integration](features/vcs-integration.md) |

### V3 — robotic swarm

One ground vehicle as gateway + many flying drones — the first homogeneous fleet and the
first hierarchy. Gateway as edge relay, `parent_device_id`, groups & bulk deploy,
airtime-aware scheduling, delta updates. Full treatment in
[groups-deploy.md](features/groups-deploy.md).

---

## Capability areas

| Area | Feature file | Spans releases |
|------|--------------|----------------|
| Enrollment & provisioning | [enrollment.md](features/enrollment.md) | R0 |
| OTA deploy & auto-rollback | [ota-deploy.md](features/ota-deploy.md) | R1, R2 |
| Health & telemetry | [health-telemetry.md](features/health-telemetry.md) | R3 |
| Self-test (sim gate + device confirm) | [self-test.md](features/self-test.md) | R4 (defined R0) |
| Signing & resumable hardening | [security-hardening.md](features/security-hardening.md) | R5 → **v1** |
| VCS integration & deploy policy | [vcs-integration.md](features/vcs-integration.md) | R6, R7, R10 |
| Simulation backend | [simulation.md](features/simulation.md) | R8 (V2) |
| Build pipeline (server-side compile) | [build-pipeline.md](features/build-pipeline.md) | R9 (V2) |
| Groups, gateway, hierarchy, delta | [groups-deploy.md](features/groups-deploy.md) | V3 (swarm) |

---

## Beyond V3

Canary / staged rollout (unlocks *safe* auto-deploy) · gateway-mediated non-IP radios
(Zigbee / BLE / LoRa) · Secure Boot v2 (new devices only) · Raspberry Pi adapter ·
SoftAP provisioning · per-device mTLS · Grafana/Prometheus telemetry export.

### The scale question — Thread

A 100+ node low-power swarm reaches past Wi-Fi: AP association limits, ~1–5 mA merely to
stay associated, and 2.4 GHz contention. **Thread** answers it — 802.15.4 mesh, hundreds
of nodes, Sleepy End Devices in µA, and **IPv6-native, so MQTT and HTTPS run unchanged**.
That last property is why v1's IP-bearing link contract is worth its (near-zero) cost.

Two constraints bite regardless of radio: ~10–50 kbps in practice makes a 1.5 MB image
10–30 minutes of radio-on time *per device*, and N devices on one mesh multiply it.
**Delta updates**, **airtime-aware scheduling** and the **V3 edge cache** are the three
answers.

Hardware note: ESP32-C6 has Wi-Fi 6 + 802.15.4; ESP32-H2 is 802.15.4-only; classic ESP32
and S3 have no 802.15.4 at all. A Thread path is a new-hardware path.

---

## Growth-stage risks

*v1's own risks are in [prd.md](../spec/prd.md) → Key risks. These become real later.*

1. **Scale ceiling** — Wi-Fi will not reach a 100+ low-power swarm, and 802.15.4
   bandwidth makes full-image OTA energetically expensive. Mitigated in advance by
   keeping the link abstract (`esp_netif`, `link_type`) so Thread and delta updates stay
   additive rather than a rewrite.
2. **Sim ≠ reality** *(V2/R8)* — simulation reduces logic and boot bugs but not
   hardware, RF or timing bugs; hence advisory-only, with canary as the next layer.
3. **Sim-engine lock-in / licensing** *(V2/R8)* — mitigate by keeping simulation
   pluggable behind the `sim-runner` contract. See [design/architecture.md](../design/architecture.md).
4. **Server-side build is arbitrary code execution** *(V2/R9)* — single-tenant keeps the
   blast radius to your own code; multi-tenant hosting would change the threat model
   entirely. See [build-pipeline.md](features/build-pipeline.md).
5. **Self-host onboarding returns at V2** — TLS without public DNS is the unsolved part,
   deferred rather than answered.
