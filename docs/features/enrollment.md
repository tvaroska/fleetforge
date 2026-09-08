# Enrollment & Provisioning

**Status:** In progress (R0)
**Priority:** P0
**Target:** R0
**Flow:** [flows.md](../../spec/flows.md) → Flow 1

## Overview

Register a physical ESP32 board into the fleet and watch it come online — no
toolchain, no CLI. Detection + flashing happen **in the dashboard via Web Serial**
(`esptool-js` / ESP Web Tools). Trust is established by **auto-enroll via a scoped,
revocable token** baked in at flash time.

The dashboard is built against the **public API + SSE event stream from R0** — it is
the first client of a headless core, not privileged over future clients (HA/CLI/MCP).

## Decisions (from flows.md)

- **Detection/flashing:** in-dashboard Web Serial (`esptool-js`). Chrome/Edge only,
  needs `localhost`/HTTPS, one mandatory user click to grant the port. No batch
  enrollment in v1 (CLI flasher is post-v1).
- **ID granularity:** chip-level auto-detect + user-confirmed board shortlist
  (chip + flash + PSRAM matched to a board DB), "enter manually" always available.
  The running agent self-reports authoritative `platform_type` + capabilities.
- **`device_id` = eFuse MAC** (stable, factory-unique) — satisfies the identity contract.
- **Provisioning = USB config flash (v1):** broker URL + Wi-Fi creds + enrollment
  token baked in at flash time. SoftAP captive portal is post-v1 (Wi-Fi change = re-flash).
- **Trust = auto-enroll via token, exchanged over HTTPS** (`POST /v1/enrol`), not over
  MQTT — so the broker never authenticates a client it has never heard of. Tokens are
  short-lived, group-scoped, revocable and **single-use**: the device trades its token
  once for a per-device broker credential kept in NVS. Every enrolled device is visible
  and removable. Per-device mTLS drops into the same exchange post-v1.
  See [device-protocol.md](../../spec/device-protocol.md).

## Phase 1: R0 — Enroll a board

**Tasks live in [TODO.md](../../TODO.md) → R0** — 16 tasks, ~20d, with build order.
TODO.md is the single source; this file holds the decisions behind them.

**Architecture guardrails**
- Dashboard is built against the public API + SSE event stream, so HA / CLI / MCP are
  cheap later clients and none is privileged.
- The agent flashed here ships the **flash-time immutable layer** — wrong at R0 means
  physically recovering every deployed board. See [design/architecture.md](../../design/architecture.md).
- Broker and artifact endpoint are public from day one: per-device credentials, pattern
  ACLs and single-use tokens are R0 work, not hardening.

**Done when:** plug in a board, flash & register from the browser, watch it come online.

## Post-v1

- CLI flasher for batch/CI enrollment.
- SoftAP captive-portal provisioning (Wi-Fi change without re-flash).
- Per-device mTLS certs (replace token-only trust).
