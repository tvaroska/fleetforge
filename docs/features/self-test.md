# Self-Test (write once, gate everywhere)

**Status:** Planned (default self-test defined at R0; custom entrypoint at R5)
**Priority:** P1
**Target:** R5
**Depends on:** OTA Deploy & Auto-Rollback (ota-deploy.md) — R2

## Overview

The **same self-test** is the simulation gate (pre-flight), the on-device confirm, and
later the canary assertion — *write once, run in sim and on device, no drift*
(DESIGN.md principle 3).

- **Default self-test** = "boots and reconnects to the broker" — catches boot-loops,
  zero user effort. Effectively active from R2 (confirm-on-reconnect).
- **Custom self-test** (R5) = an optional entrypoint baked into firmware. The agent
  calls it after boot; the simulator (R6) calls it as its gate. Catches "boots but app
  logic broken," not just boot-loops.

## Phase 1: R5 — Custom self-test confirm

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R5-FW-1 | Optional self-test entrypoint contract in agent/OTA library | P0 | 1d |
| R5-FW-2 | Run self-test at confirm time; fail → rollback | P0 | 1d |
| R5-BE-1 | Deploy honours self-test result in confirm/rollback decision | P0 | 1d |
| R5-FE-1 | Dashboard surfaces self-test pass/fail per deploy | P1 | 0.5d |
| R5-TEST-1 | E2E: deploy firmware whose self-test fails → auto-rollback | P0 | 1d |

**Done when:** you can catch "boots but app logic broken," not just boot-loops.

## Reuse note

The self-test entrypoint defined here is consumed by the simulation gate
(simulation.md, R6) with **no code change** — one test, three enforcement points.
