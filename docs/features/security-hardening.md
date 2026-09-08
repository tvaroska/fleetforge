# Signed OTA + Resumable Hardening (→ v1 complete)

**Status:** Planned
**Priority:** P0
**Target:** R7
**Depends on:** OTA Deploy & Auto-Rollback (ota-deploy.md), Simulation (simulation.md)

## Overview

Make it production-grade: firmware **signing** (secure boot / signature verify before
apply), **resumable downloads** with retry/backoff for flaky Wi-Fi, and both KPIs
surfaced. This closes the v1 defense-in-depth story: simulate (pre-flight) →
auto-rollback (per-device) → verified + signed + resumable transport.

## Phase 1: R7 — Signed OTA + resumable hardening

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R7-SEC-1 | Firmware signing: sign artifacts server-side; agent verifies signature before apply | P0 | 2d |
| R7-SEC-2 | Secure-boot alignment / key management docs + defaults | P1 | 1d |
| R7-FW-1 | Resumable download (range/offset), retry + backoff | P0 | 1.5d |
| R7-BE-1 | Surface both KPIs: delivery success + fleet safety | P0 | 1d |
| R7-FE-1 | Dashboard KPI view (delivery success vs fleet safety, with the gap) | P0 | 1d |
| R7-TEST-1 | E2E: unsigned/tampered artifact rejected; interrupted download resumes | P0 | 1d |

**Done when:** you can run it in earnest — production-grade safety + security. **v1 complete.**

## Metrics (SPEC.md)

- **Delivery success** = healthy AND running the intended new version (rollback = miss).
- **Fleet safety** = device ends healthy on some version (rollback = save).
- The gap between the two tells you *which stage* to fix.

## Post-v1 hardening

Per-device mTLS certs (replace token-only trust from R0).
