# Enrollment & Provisioning

**Status:** In progress (R0)
**Priority:** P0
**Target:** R0
**Flow:** [FLOWS.md](../FLOWS.md) → Flow 1

## Overview

Register a physical ESP32 board into the fleet and watch it come online — no
toolchain, no CLI. Detection + flashing happen **in the dashboard via Web Serial**
(`esptool-js` / ESP Web Tools). Trust is established by **auto-enroll via a scoped,
revocable token** baked in at flash time.

The dashboard is built against the **public API + MQTT event stream from R0** — it is
the first client of a headless core, not privileged over future clients (HA/CLI/MCP).

## Decisions (from FLOWS.md)

- **Detection/flashing:** in-dashboard Web Serial (`esptool-js`). Chrome/Edge only,
  needs `localhost`/HTTPS, one mandatory user click to grant the port. No batch
  enrollment in v1 (CLI flasher is post-v1).
- **ID granularity:** chip-level auto-detect + user-confirmed board shortlist
  (chip + flash + PSRAM matched to a board DB), "enter manually" always available.
  The running agent self-reports authoritative `platform_type` + capabilities.
- **`device_id` = eFuse MAC** (stable, factory-unique) — satisfies the identity contract.
- **Provisioning = USB config flash (v1):** broker URL + Wi-Fi creds + enrollment
  token baked in at flash time. SoftAP captive portal is post-v1 (Wi-Fi change = re-flash).
- **Trust = auto-enroll via token:** short-lived, group-scoped, revocable. Every
  enrolled device is visible + removable. Per-device mTLS is post-v1 hardening.

## Phase 1: R0 — Enroll a board

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R0-INFRA-1 | Docker Compose stack scaffold: MQTT broker + control-plane API + dashboard, TLS defaults | P0 | 2d |
| R0-DB-1 | Device registry schema: `device_id`, `platform_type`, capabilities, fw version, last-seen | P0 | 0.5d |
| R0-BE-1 | Control-plane API scaffold (public, versioned) + device registry persistence | P0 | 1.5d |
| R0-BE-2 | Enrollment-token generation: short-lived, group-scoped, revocable | P0 | 1d |
| R0-BE-3 | MQTT broker integration: device announce (identity) + heartbeat ingestion | P0 | 1.5d |
| R0-BE-4 | Auto-enroll: validate token on first connect → device active | P0 | 1d |
| R0-FW-1 | ESP32 agent (connect-only, no OTA): Wi-Fi + MQTT connect, announce identity + heartbeat | P0 | 2d |
| R0-FE-1 | Dashboard: "Enroll a board" page (generate token) | P0 | 1d |
| R0-FE-2 | Dashboard: live device list via MQTT event stream | P0 | 1.5d |
| R0-FE-3 | Web Serial flasher (`esptool-js`): detect → confirm → flash agent + baked config | P0 | 2.5d |
| R0-TEST-1 | E2E: flash → connect → auto-enroll appears in dashboard | P0 | 1d |

**Done when:** plug in a board, flash & register from the browser, watch it come online.

## Post-v1

- CLI flasher for batch/CI enrollment.
- SoftAP captive-portal provisioning (Wi-Fi change without re-flash).
- Per-device mTLS certs (replace token-only trust).
