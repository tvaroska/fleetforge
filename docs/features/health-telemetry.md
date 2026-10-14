# Health & Telemetry

**Priority:** P1
**Target:** R4
**Depends on:** Enrollment (enrollment.md) — R0

## Overview

Watch fleet health live. The agent reports boot-success, uptime, and a few
user-defined telemetry metrics. The dashboard shows live status + last-seen + metrics.
Telemetry rides the health channel of the device contract (design/architecture.md → identity/health).

Heartbeat intervals, offline-detection windows and **retention limits** are specified in
[prd.md](../../spec/prd.md) → *Requirements & targets*. R4-BE-1 must implement the retention
policy, not just the ingest.

## Phase 1: R4 — Health & telemetry view

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R4-FW-1 | Agent reports boot-success + uptime | P0 | 1d |
| R4-FW-2 | User-defined telemetry metrics channel | P1 | 1d |
| R4-BE-1 | Ingest + persist health/telemetry with SPEC retention (30d telemetry, 7d logs). Expose via API + SSE | P0 | 1.5d |
| R4-FE-1 | Live fleet status dashboard: online/offline, last-seen, boot-success, metrics | P0 | 2d |
| R4-TEST-1 | E2E: metric emitted on device → visible live in dashboard | P0 | 0.5d |

**Done when:** you can watch fleet health live.

## Post-v1

Grafana/Prometheus telemetry export (the "observe" pillar), richer alerting.

### Live announce never reports a connecting board offline
- **What was built:** `DeviceEvent.online` is now `bool | None` (still required, `v` stays 1).
  `ingestor/handlers.py::_online_snapshot` returns `presence.is_online(...)`, except that a live
  (`retained=False`) `up/announce` whose derived presence is not true reports `None`
  ("not known yet; this session's `up/presence` follows"). It is keyed on the channel, not
  `event_type`. Retained announce replays and all other message types are unchanged.
  `presence.py` is untouched, so `is_online` stays the single derivation.
- **Why:** the agent publishes announce then presence on every connect. At announce time
  `presence_reported` still holds the previous session's value (NULL after re-enroll, false
  after an LWT), so an `always_on` board's announce snapshot said `online=False`. The browser
  never reads the event's `online` (a frame only triggers a re-read of `GET /v1/devices`), so
  the dashboard could not flicker; only SSE watchers and the ingestor log saw the wrong value.
- **Seams and gotchas:** the agent publish order is load-bearing and was not changed. The
  ingestor does not hot-reload in dev, so restart it when testing. `api/eventstream._on_notify`
  validates with the same model, so an old api would drop a new ingestor's `null` frame; they
  ship together. `test_a_null_online_is_forwarded` guards against reverting the model to `bool`.
