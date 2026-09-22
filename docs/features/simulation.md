# Simulation Backend (advisory gate)

**Status:** Planned — **V2, not v1**
**Priority:** P1
**Target:** R9 (V2)
**Depends on:** Self-Test (self-test.md) — R5 (v1)
**Design:** [design/architecture.md](../../design/architecture.md) → Simulation backend

## Overview

Catch bad builds **before any device is touched**, at zero device cost. Boot the
artifact in an emulator and run the user's self-test → **warn + override** in the UI
(advisory-only, because sim ≠ reality for hardware/RF/timing bugs).

Simulation is **pluggable behind a `sim-runner` contract** (`boot artifact + run
self-test → pass/fail`) so no single engine locks us in across the platform ladder.

## Decisions (from design/architecture.md)

- **Harness:** `pytest-embedded` — same self-test runs on host, in sim, and on real
  hardware (the R5 self-test is reused verbatim).
- **v1 backend (ESP32):** Espressif's QEMU fork via `pytest-embedded-qemu` —
  first-party, self-hostable, mature, multi-DUT.
- **Growth backend:** Renode (MIT) — widest arch reach; spike ESP32 completeness
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

**Done when:** you can catch bad builds before any device is touched.

## Post-v1 (fidelity layer)

Real-hardware canary via `pytest-embedded` serial, automated at scale with **LAVA**
(open, self-host) — the pre-fleet layer between sim and per-device rollback.
