# Fleetforge — Current Plan

**Project:** Self-hosted OTA firmware management for embedded fleets (ESP32 first).
**Specs:** [docs/SPEC.md](docs/SPEC.md) · [docs/DESIGN.md](docs/DESIGN.md) · [docs/FLOWS.md](docs/FLOWS.md) · [docs/RELEASES.md](docs/RELEASES.md)
**Roadmap:** [docs/roadmap.md](docs/roadmap.md)

This file holds **Sprint 0 (always-on) + the active release only**. Completed work is
archived to `docs/features/*.md`; the full release ladder lives in `docs/roadmap.md`.

> **Task IDs:** fleetforge is release-driven, so IDs are `R{N}-{CATEGORY}-{NUMBER}`
> (e.g. `R0-BE-1`). Sprint 0 uses `S0-{CATEGORY}-{NUMBER}`.
> Categories: DB, BE, FE, TEST, QA, SEC, INFRA, REL, PERF.

---

## Sprint 0 — Production readiness / security / critical bugs (always highest priority)

Nothing here yet — the project is pre-R0. Critical issues (bricking risks, broker
auth, security) get filed here via `/new-task` as they surface.

| ID | Task | Priority | Effort | Status |
|----|------|----------|--------|--------|
| — | (none yet) | — | — | — |

---

## Active release — R0: Enroll a board (UI + recognition + flash + connect)

**Goal:** *I can register a board and see it online.* No code-deploy yet.
**Risk retired:** onboarding + board recognition + device↔server connection.
**Architecture guardrail:** build the dashboard against a **public API + MQTT event
stream from R0** (API-first, headless core) so HA / CLI / MCP become cheap clients later.

See [docs/features/enrollment.md](docs/features/enrollment.md) for full detail.

| ID | Task | Priority | Effort | Status |
|----|------|----------|--------|--------|
| R0-INFRA-1 | Docker Compose stack scaffold: MQTT broker + control-plane API + dashboard, TLS defaults | P0 | 2d | Not started |
| R0-DB-1 | Device registry schema: `device_id` (eFuse MAC), `platform_type`, capabilities, fw version, last-seen | P0 | 0.5d | Not started |
| R0-BE-1 | Control-plane API scaffold (public, versioned) + device registry persistence | P0 | 1.5d | Not started |
| R0-BE-2 | Enrollment-token generation: short-lived, group-scoped, revocable | P0 | 1d | Not started |
| R0-BE-3 | MQTT broker integration: device announce (identity) + heartbeat ingestion | P0 | 1.5d | Not started |
| R0-BE-4 | Auto-enroll: validate token on first connect → device becomes active | P0 | 1d | Not started |
| R0-FW-1 | ESP32 agent (connect-only, no OTA): Wi-Fi + MQTT connect, announce identity + heartbeat | P0 | 2d | Not started |
| R0-FE-1 | Dashboard: "Enroll a board" page (generate token) | P0 | 1d | Not started |
| R0-FE-2 | Dashboard: live device list (online/offline, version, last-seen) via MQTT event stream | P0 | 1.5d | Not started |
| R0-FE-3 | In-dashboard Web Serial flasher (`esptool-js`): port select → chip detect → board-confirm shortlist → flash agent + baked config (broker/Wi-Fi/token) | P0 | 2.5d | Not started |
| R0-TEST-1 | End-to-end R0 test: flash → connect → auto-enroll appears in dashboard | P0 | 1d | Not started |

**Done when:** plug in a board, flash & register it from the browser, watch it come
online — no toolchain, no CLI.

**Parallel spike (de-risks R2):** throwaway OTA + auto-rollback spike on real flaky
Wi-Fi. Tracked in [docs/features/ota-deploy.md](docs/features/ota-deploy.md).
