# Groups & Bulk Deploy

**Status:** Planned
**Priority:** P1
**Target:** R3
**Depends on:** OTA Deploy & Auto-Rollback (ota-deploy.md) — R2

## Overview

Update many boards at once. Introduces tags/groups and group-targeted deploy with a
per-device progress view. Targeting granularity in v1 = **device or group/tag**;
staged/canary rollout is post-v1 (the top lever for delivery success).

## Phase 1: R3 — Groups & bulk deploy

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R3-DB-1 | Tags/groups schema + device↔group membership | P0 | 0.5d |
| R3-BE-1 | Assign devices to groups/tags (API) | P0 | 1d |
| R3-BE-2 | Deploy to a group (fan out the per-device transaction) | P0 | 1.5d |
| R3-FE-1 | Group management UI + group deploy | P0 | 1d |
| R3-FE-2 | Per-device progress view for a bulk deploy | P0 | 1.5d |
| R3-TEST-1 | E2E: deploy to a group → all members update, progress visible | P0 | 1d |

**Done when:** you can update many boards at once and watch per-device progress.

## Post-v1

Staged / canary rollout — deploy to a canary subset, gate the rest on canary health.
Unlocks *safe* auto-deploy (see vcs-integration.md, R10).
