# Signed OTA + Resumable Hardening (→ v1 complete)

**Status:** Planned
**Priority:** P0
**Target:** R6 (→ v1 complete)
**Depends on:** OTA Deploy & Auto-Rollback (ota-deploy.md) — R2

## Overview

Make it production-grade: **app-level firmware signature verification** before apply,
**resumable downloads** with retry/backoff for flaky Wi-Fi, and both KPIs surfaced.

**App-level signing, not Secure Boot v2.** Secure Boot v2 burns a key digest to eFuse and
needs a re-signed bootloader, so it can never be enabled on an already-deployed board —
it is post-v1 and new-devices-only. What R6 ships is pure software the agent can receive
over OTA. The two are not interchangeable; see [design/architecture.md](../../design/architecture.md) →
*Flash-time immutables*. This closes v1: auto-rollback (per-device) + verified, signed, resumable transport.
The simulation pre-flight gate is V2 (R9), so v1's defense in depth is one layer deep
by design — see [releases.md](../releases.md).

## Phase 1: R6 — Signed OTA + resumable hardening

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R6-SEC-1 | Firmware signing: sign artifacts server-side; agent verifies signature before apply | P0 | 2d |
| R6-SEC-2 | Signing-key management: generation, storage, rotation, docs + defaults | P1 | 1d |
| R6-FW-1 | Resumable download (range/offset), retry + backoff | P0 | 1.5d |
| R6-BE-1 | Surface both KPIs from the deploy-event history recorded since R1 | P0 | 1d |
| R6-FE-1 | Dashboard KPI view (delivery success vs fleet safety, with the gap) | P0 | 1d |
| R6-TEST-1 | E2E: unsigned/tampered artifact rejected; interrupted download resumes | P0 | 1d |

**Done when:** you can run it in earnest — production-grade safety + security. **v1 complete.**

## Metrics

Definitions and targets: [prd.md](../../spec/prd.md) → *Success criteria*. The gap between the
two numbers tells you *which stage* to fix.

**Recording starts at R1, not here.** The `deploy_events` table is in the R0 schema and
every deploy outcome is written from R1 onward — otherwise R6 arrives with two metrics
and no history to compute them from.

## Post-v1 hardening

Per-device mTLS certs (replace token-only trust from R0).
