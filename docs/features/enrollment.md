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

### Token Issuance (R0-be-2)

The admin API provides enrollment token lifecycle management through three endpoints:
`POST /v1/enrollment-tokens` (issue), `GET /v1/enrollment-tokens` (list), and
`POST /v1/enrollment-tokens/{id}/revoke`. Tokens are short-lived (24 h TTL from
`config.enrollment_token_ttl_hours`), group-scoped (or ungrouped via `group_id: null`),
and strictly single-use.

The plaintext token (format `ffe_<uuid>.<secret>`) is returned exactly once in the POST
response body so an operator can copy it into the flasher's baked config. The secret is
hashed with argon2id and never stored, logged, or re-derivable. The list endpoint shows
token status (active / used / revoked / expired) but never exposes the plaintext or hash.

Single-use enforcement relies on a conditional UPDATE statement (`BURN_SQL` in
`fleetforge.auth.enrollment`) that atomically marks a token as used only if it is still
active (not used, not revoked, not expired). This statement is shipped as an importable
constant rather than copy-paste prose, so `R0-be-4` (device enrollment) and the invariant
tests all reference the same implementation. The burn predicate (`used_at IS NULL AND
revoked_at IS NULL AND expires_at > now()`) defines what "active" means, and the API's
derived status must match it exactly—a dashboard showing "active" for a token the burn
would reject causes field enrollment failures.

Revocation uses `POST .../revoke` rather than `DELETE` because revoked tokens are retained
for 90 days (per `spec/prd.md` retention policy) and `used_by_device_id` provides
enrollment provenance for the fleet. Group CRUD deliberately does not exist in R0—tokens
can be group-scoped but nothing creates groups yet—so all R0 tokens are ungrouped in
practice. This is acceptable because bulk deployment is a V3 feature.

### Ingest & derived presence (R0-be-3)

The ingestor (`src/fleetforge/ingestor/`) is the fleet's **single MQTT subscriber**. It
consumes `ff/v1/d/+/up/#`, updates the device row, and emits one `ff_events`
notification per ingested message, which R0-be-5 fans out over SSE. It **never
inserts a device**: every statement is `UPDATE devices … WHERE device_id = :id AND
decommissioned_at IS NULL`, so publishing to the broker cannot join the fleet — only a
burned enrollment token can. Zero rows back is logged and dropped.

**Presence is derived, never stored** (`fleetforge/presence.py`, one implementation
shared with the API's device list): an `always_on` device is online iff the retained
`up/presence` value says so; a `sleepy` device is online iff `now - last_seen <
presence_tolerance × expected_wake_interval_s` (2.5, from `spec/prd.md` → *Timing*),
and its LWT is ignored entirely because it fires on every normal sleep.

The counterpart rule is when `last_seen` may move: **only on a live (`retain=False`)
message that is not `presence{"online":false"}`**. Retained `announce`/`presence` are
replayed to the ingestor on every reconnect, and the LWT is published by the broker
rather than the board; treating either as evidence of life would mark a dead fleet
alive. It is written as `GREATEST(last_seen, :at)` so an at-least-once QoS 1
re-delivery cannot move a device backwards.

An announce is applied field by field — anything the payload omits means "no change" —
and a `power_class` / `expected_wake_interval_s` pair that would violate a DB CHECK is
dropped from the update rather than allowed to lose the whole announce (the
`fw_version` in it is what tells the operator an OTA landed). A payload `device_id`
that disagrees with the topic is an impersonation attempt and is dropped: the topic is
what the `%u` pattern ACL binds to the broker username.

`up/status`, `up/telemetry` and `up/log` are accepted and only move `last_seen`;
persisting them is R1 (`deploy_events`) and R3 (telemetry) respectively.

## Post-v1

- CLI flasher for batch/CI enrollment.
- SoftAP captive-portal provisioning (Wi-Fi change without re-flash).
- Per-device mTLS certs (replace token-only trust).
