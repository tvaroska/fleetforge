# Health & Telemetry

**Status:** Planned
**Priority:** P1
**Target:** R4
**Depends on:** Enrollment (enrollment.md) — R0

## Overview

Watch fleet health live. The agent reports boot-success, uptime, and a few
user-defined telemetry metrics; the dashboard shows live status + last-seen + metrics.
Telemetry rides the health channel of the device contract (design/architecture.md → identity/health).

Heartbeat intervals, offline-detection windows and **retention limits** are specified in
[prd.md](../../spec/prd.md) → *Requirements & targets*; R4-BE-1 must implement the retention
policy, not just the ingest.

## Phase 1: R4 — Health & telemetry view

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R4-FW-1 | Agent reports boot-success + uptime | P0 | 1d |
| R4-FW-2 | User-defined telemetry metrics channel | P1 | 1d |
| R4-BE-1 | Ingest + persist health/telemetry with SPEC retention (30d telemetry, 7d logs); expose via API + SSE | P0 | 1.5d |
| R4-FE-1 | Live fleet status dashboard: online/offline, last-seen, boot-success, metrics | P0 | 2d |
| R4-TEST-1 | E2E: metric emitted on device → visible live in dashboard | P0 | 0.5d |

**Done when:** you can watch fleet health live.

## Post-v1

Grafana/Prometheus telemetry export (the "observe" pillar), richer alerting.
