# Simulation Backend (advisory gate)

**Priority:** P1
**Target:** R9 (V2)
**Depends on:** Self-Test (self-test.md) — R5 (v1)
**Design:** [design/architecture.md](../../design/architecture.md) → Simulation backend

## Overview

Catch bad builds **before any device receives it**, at zero device cost. Boot the
artifact in an emulator and run the user's self-test → **warn + override** in the UI
(advisory-only, because sim ≠ reality for hardware/RF/timing bugs).

Simulation is **pluggable behind a `sim-runner` contract** (`boot artifact + run
self-test → pass/fail`) so no single engine locks us in across the platform ladder.

## Decisions (from design/architecture.md)

- **Harness:** `pytest-embedded` — same self-test runs on host, in sim, and on real
  hardware (the R5 self-test stays in use verbatim).
- **v1 backend (ESP32):** Espressif's QEMU fork via `pytest-embedded-qemu` —
  first-party, self-hostable, mature, multi-DUT.
- **Growth backend:** Renode (MIT) — widest arch reach. Spike ESP32 completeness
  before relying on it.
- **Rejected:** Wokwi (SaaS, no real self-host) · Velxio (AGPL + license-gated QEMU,
  ESP32-only OSS path).

## Phase 1: R9 — Advisory simulation gate

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R9-INFRA-1 | `pytest-embedded` + Espressif QEMU in the Compose stack / runner | P0 | 1.5d |
| R9-BE-1 | `sim-runner` contract + ESP32/QEMU implementation | P0 | 2d |
| R9-BE-2 | Wire sim gate into deploy pipeline (pre-flight, advisory) | P0 | 1d |
| R9-FE-1 | Deploy UI: sim result + warn/override control | P0 | 1d |
| R9-TEST-1 | E2E: upload a boot-crashing build → sim warns before deploy | P0 | 1d |

**Done when:** you can catch bad builds before any device receives it.

## As built (`R9-1` & `R9-2`, 2026-11-19)

- **Pluggable `SimRunner` Port (`src/fleetforge/simulation.py`):**
  - Defines `SimStatus` (`pending`, `running`, `passed`, `failed`, `skipped`), `SimResult`, and the `SimRunner` `Protocol` (`simulate(*, sha256, target, image, partition_layout, artifact_type) -> SimResult`).
  - `EspressifQemuSimRunner` builds a 4 MiB raw flash image placing the app at `0x10000`, invokes Espressif QEMU (`qemu-system-xtensa` for `esp32`/`esp32s3`, `qemu-system-riscv32` for `esp32c3`/`esp32c6`) or a `pytest-embedded-qemu` harness under a wall-clock timeout, and classifies serial output (`selftest passed` / `marked app valid` / `confirmed` vs Guru Meditation panic / `selftest failed` / `rolled_back`).
  - `HostPreflightSimRunner` provides deterministic host-side ESP-IDF boot header (`0xE9`), `FFOTALIB` marker, and fault-vector (`FF_SIM_BOOT_CRASH`, `FF_SIM_SELFTEST_FAIL`) verification when QEMU binaries are absent on the worker.
- **External Worker (`src/fleetforge/sim_worker.py`):**
  - `python -m fleetforge.sim_worker [--once] [--interval 5.0] [--backend auto|qemu|host]` claims `sim_status = 'pending'` rows with `FOR UPDATE SKIP LOCKED`, downloads artifact bytes from `ObjectStore`, and persists verdicts outside the 256 MiB `fleetforge-api` container.
- **Schema, API, Advisory Gate & Audit Trail (`alembic/versions/0016_artifact_simulation.py`):**
  - Adds reversible `sim_status`, `sim_detail`, and `sim_ran_at` columns on `artifacts`, surfaced on `POST /v1/artifact?simulate=true`, `POST /v1/artifact/{sha256}/simulate`, and `GET /v1/artifact`.
  - `deploy_precheck.py` raises the gating warning `sim_failed` (`needs_override=True`) when `sim_status == 'failed'`; `POST /v1/devices/{id}/deploy` answers `409` unless `override` includes `"sim_failed"`, and `deploys.py::record_requested` records `"overrides": [...]` in `deploy_events.detail`.
  - React dashboard (`SimulationBadge.tsx`, `Dashboard.tsx`, `PrecheckCard.tsx`) displays `✓ sim passed` / `[! sim failed]` badges, `[Run sim]` triggers, and explicit override checkboxes.

## Post-v1 (fidelity layer)

Real-hardware canary via `pytest-embedded` serial, automated at scale with **LAVA**
(open, self-host) — the pre-fleet layer between sim and per-device rollback.
