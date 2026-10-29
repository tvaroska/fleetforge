# FleetForge Agent Instructions (`~/projects/fleetforge/AGENTS.md`)

## 1. Purpose & Overview

**FleetForge** is a self-hosted OTA firmware management and pre-flight simulation platform for embedded fleets (ESP32 first, scaling to Raspberry Pi, Thread 802.15.4 meshes, and FPGAs). Its core guarantee is **safe remote firmware updates**: pre-flight gates catch bad builds before they reach the fleet, and any board that receives a bad update automatically rolls itself back.

## 2. Architecture & Release Ladder (`R0–R11`)

FleetForge is built around **two thin waists** (MQTT control plane + HTTPS artifact plane):
1. **Device-Facing Waist (`stage → apply → confirm → rollback`):** Opaque versioned binary artifact contract where the server orchestrates waves and the device owns reboot timing (`awaiting_safe_window`), custom self-test confirmation (`ff_set_selftest`), and bootloader A/B rollback (`ab-4m-v1`).
2. **User-Facing Waist (`src/fleetforge/` + `frontend/`):** Headless FastAPI/PostgreSQL/MinIO/Mosquitto core (`api` + `ingestor` entrypoints) with multi-project RBAC, scoped `ffa_...` tokens, ECDSA P-256 firmware signing (`ecdsa-p256-sha256`), rotating encrypted device secrets, and OTLP telemetry export.

### Release Ladder
- **V1 (`R0–R6`, Complete):** Web Serial enrollment (`R0`), OTA deploy (`R1`), A/B auto-rollback (`R2`), embeddable ESP-IDF/PlatformIO/Arduino OTA library (`R3`), live health/faults/RTC crash log ring (`R4`), custom self-test & safe-window gating (`R5`), and ECDSA P-256 signing + resumable HTTP `Range` downloads (`R6`).
- **V2 (`R7–R11`, Source-to-Artifact & Simulation):** Artifact provenance API (`R7`), Git CI/CD push ingestion (`R8`), advisory QEMU/simulation gate (`R9`), server-side container build pipeline (`R10`), and staged cohort rollout policy (`R11`).
- **V3 (Robotic Swarm):** UGV edge-relay gateway + drone fleet hierarchy, airtime-aware scheduling, and delta OTA updates.

## 3. Key Documentation & Protected Paths

- **Overview & Dev Runbook:** [`README.md`](README.md) · [`docs/runbooks/dev-stack.md`](docs/runbooks/dev-stack.md)
- **Protected Paths & Flash-Time Immutables:** [`CRITICAL.md`](CRITICAL.md) (`spec/device-protocol.md`, `ab-4m-v1` partition tables, confirm-timer rollback posture, Mosquitto ACL/dynsec split, auth/RBAC, ECDSA signing)
- **Architecture & Wire Specs:** [`design/architecture.md`](design/architecture.md) · [`design/production.md`](design/production.md) · [`spec/prd.md`](spec/prd.md) · [`spec/device-protocol.md`](spec/device-protocol.md) · [`spec/flows.md`](spec/flows.md)
- **Roadmap & Decision Log:** [`docs/roadmap.md`](docs/roadmap.md) · [`docs/releases.md`](docs/releases.md) · [`DECISIONS.md`](DECISIONS.md)
- **Embeddable Firmware Component:** [`agent/components/fleetforge/README.md`](agent/components/fleetforge/README.md)

## 4. Verification Gate

```bash
/usr/local/google/home/tvaroska/projects/bin/proj-test fleetforge
# or directly inside ~/projects/fleetforge:
uv run pytest
```
