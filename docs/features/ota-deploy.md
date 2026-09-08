# OTA Deploy & Auto-Rollback

**Status:** Planned
**Priority:** P0
**Target:** R1 (deploy), R2 (safe deploy ⭐)
**Depends on:** Enrollment (enrollment.md) — R0
**Flow:** [flows.md](../../spec/flows.md) → Flow 2

## Overview

Push new firmware to a registered board from the dashboard (R1), then make it **safe**:
a bad build is caught and any device that gets one **recovers itself** via A/B slot +
auto-rollback (R2). R2 is the single most important milestone — the whole gamble.

Artifacts are **opaque + versioned**; the server stores/targets/tracks but never parses
them. Users build the `.bin` with their own toolchain (idf.py / PlatformIO / Arduino) —
the server never builds.

Size limits, deploy-duration targets and the confirm-timeout default:
[prd.md](../../spec/prd.md) → *Requirements & targets*. Channel split and signed URLs:
[design/architecture.md](../../design/architecture.md) → *Transport*.

## The update transaction (4-verb contract, from design/architecture.md)

`stage → apply → confirm → rollback` — server orchestrates, never knows *how* **nor when**.

**Two authority rules, both device-side:**
- **The device owns the reboot.** `stage` delivers and verifies; the device applies only
  in a self-declared safe window and may sit in `awaiting_safe_window` indefinitely — a
  vehicle in motion or an airborne drone must not reboot on the server's schedule.
  Rollback reboots obey the same rule.
- **The device owns the rollback.** The confirm timer is armed on the device before the
  reboot. A board that cannot reach the broker is exactly the board that must roll back,
  and it will never receive a server command saying so. The server observes and records;
  it never triggers a rollback.

ESP32 adapter: write OTA1 partition → broker reconnect + self-test → switch to OTA0.

## Phase 1: R1 — Upload new code (OTA deploy)

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R1-BE-1 | Artifact upload `POST /v1/artifact` (opaque blob + version + platform_type); reject over the target's `ota_slot_size` and over the SPEC size limit | P0 | 1d |
| R1-BE-2 | Deploy orchestration: `stage → apply` (per-device), carrying a short-lived signed artifact URL | P0 | 1.5d |
| R1-BE-3 | Artifact download endpoint: signed-URL verification + HTTP range support | P0 | 1d |
| R1-BE-4 | Write every deploy outcome to `deploy_events` — the KPI history R5 computes from | P0 | 0.5d |
| R1-FW-1 | Agent gains `esp_https_ota` + "update" command handler | P0 | 2d |
| R1-FW-2 | Agent reports firmware version after reboot | P0 | 0.5d |
| R1-FE-1 | Per-device Deploy button + version-change feedback | P0 | 1d |
| R1-TEST-1 | E2E: push firmware → board version changes in dashboard | P0 | 1d |

> ⚠️ Not yet safe — a broken build stays broken until R2.

## Phase 2: R2 — Safe deploy: verify + auto-rollback ⭐

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R2-FW-1 | Checksum verify before apply | P0 | 1d |
| R2-FW-2 | A/B slot apply (OTA0/OTA1), atomic switch | P0 | 1.5d |
| R2-BE-1 | Observe confirm/rollback outcome; record the result to `deploy_events` | P0 | 1d |
| R2-FW-3 | **Device-side** confirm timer armed pre-reboot → `esp_ota` self-rollback on timeout | P0 | 1d |
| R2-FE-1 | Dashboard shows `good` vs `rolled-back` per device | P0 | 0.5d |
| R2-TEST-1 | Push a *deliberately broken* build → board auto-recovers | P0 | 1d |

**Done when:** a deliberately broken build deploys → the board auto-recovers to the
previous version.

## De-risking

Run a **throwaway OTA + auto-rollback spike during R0–R1** on real flaky Wi-Fi —
prove auto-rollback saves a bad build before relying on it. (Tracked as parallel work
in TODO.md.)
