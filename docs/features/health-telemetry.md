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

### Why a board announced twice with no deploy (S0-bug-3)

- **What:** `84c7bbe0e8c0` published extra `announce` + `presence` at 22:23:22 and 22:49:26 UTC on 2026-10-14, with no deploy. The first was a **reboot** (full `link_up` + `time_synced` + `mqtt_connected` ladder at 22:23:12-22:23:22, hb phase `:19` -> `:22`, hb 81 B = uptime reset, broker takeover at 1792016601, no commander connect, no `deploy_events`). The second was an **MQTT reconnect inside the same boot** (`mqtt_connected` alone, hb phase and 83 B size unchanged, two queued hbs flushed together, broker takeover at 1792018164). Verbatim rows are in DECISIONS.md (2026-10-14).
- **Why:** the announce is republished on every broker connect by design, so a reconnect looks like a boot unless `device_progress` is read. Rule: `link_up` + `time_synced` within ~15 s before `mqtt_connected` is a boot; `mqtt_connected` alone is a reconnect. `up/hb.uptime_s` would also tell, but is not stored (R4).
- **Seams and gotchas:** the dashboard serial console deliberately pulses EN on every "Watch a board" (S0-fe-5), so an operator watching a log produces a reboot with no deploy and no LWT. Event 1's cause is unproven; the operator question (reset, re-plug, or console attach at ~22:23 UTC?) is still owed. `device_progress` is trimmed per device, so copy evidence out promptly. The S0-bug-2 plan's "fast reboot" for 22:49:26 was wrong.

### Reset reason proposal (S0-bug-3, proposed)

Proposal only; nothing is built. `spec/` is protected, so the patches are paste-ready here (the `lib_marker` precedent: DECISIONS 2026-10-08 R3-spec-3 and 2026-10-09).

- **On the wire:** `reset_reason`, one additive flat `up/announce` string: the `esp_reset_reason()` of the current boot, read once at boot. A reconnect inside one boot repeats the same value; reboot vs. reconnect is still read from `device_progress`. `proto` stays 1; about 25 B. The enroll body carries it (flat), and `EnrollRequest` already has `extra="ignore"`.
- **Values** (lowercase `esp_reset_reason_t` names, IDF 5.5): `unknown`, `poweron`, `ext`, `sw`, `panic`, `int_wdt`, `task_wdt`, `wdt`, `deepsleep`, `brownout`, `sdio`, `usb`, `jtag`, `efuse`, `pwr_glitch`, `cpu_lockup`. An unmappable value becomes `unknown`; a value the server does not know is logged as-is, never refused (Evolution rule 2). Absent on agents <= 0.5.0 means "not reported".
- **Server v1:** the ingestor logs it on a live announce, nothing else: no column, no migration, no API field. Storage and display are R4 (R4-BE-1 / R4-FE-1). Malformed (not a string, or longer than 16 chars) is logged as null, never a lost announce (`_TolerantPayload` rule).
- **Simulator:** `poweron` on first start, `sw` after a simulated OTA reboot, so `just sim` exercises it.
- **What it can and cannot tell:** spontaneous (`panic`, `*_wdt`, `brownout`, `cpu_lockup`) vs. external or commanded (`poweron`, `ext`, `sw`, `usb`). It cannot tell "operator re-plugged" from "dashboard console pulsed EN": on most chips both read as a chip reset (`poweron`; the C3's USB-Serial-JTAG reset may read `usb`). This is **unmeasured** (no bench, DECISIONS 2026-10-08), a named gap. QEMU can prove `poweron`, `sw`, `panic` (injected abort) and `task_wdt`.
- **Related open question:** `spec/open-questions.md` -> *enrollment: How much of the diagnosis belongs to firmware* already names `esp_reset_reason()`; this answers it for the connected case only.
- **Dry-run result (throwaway worktree, 2026-10-14):** Patch A alone: `tests/test_ff_cfg.py` + `tests/test_library_marker.py` all green (113 passed). A+B: exactly one failure, `TestAnnounceMatchesTheSpec::test_the_firmware_builds_exactly_the_spec_keys` ("ff_identity.c never emits 'reset_reason'"); the `lib_marker`-last tests stay green.
- **Patch A** (owner, `spec:` commit). In `spec/device-protocol.md` -> `### up/announce — identity`, add this paragraph directly after the `lib_marker` paragraph (ending "...stores a malformed value as null rather than refusing the request."):

  > `reset_reason` is why the chip last reset, as `esp_reset_reason()` reports it at
  > boot, lowercased without the `ESP_RST_` prefix: `poweron`, `ext`, `sw`, `panic`,
  > `int_wdt`, `task_wdt`, `wdt`, `deepsleep`, `brownout`, `sdio`, `usb`, `jtag`, `efuse`,
  > `pwr_glitch`, `cpu_lockup`, or `unknown`. It describes the current boot, so a
  > reconnect inside one boot repeats it. It is additive under *Evolution rules*, rule 2,
  > and `proto` stays `1`. It is absent from agents that predate it, and the server treats
  > absent and `null` alike. The server never refuses or warns on a device for it and
  > logs a value it does not know as-is. The enroll body carries it like every other
  > announce field.

- **Patch B** (S0-fw-6's commit only): in the announce JSON example insert `"reset_reason": "poweron",` on its own line **between** the `"capabilities": [...]` line and `"lib_marker": 1`. `lib_marker` must stay the last key (`test_the_spec_example_ends_with_lib_marker`, `test_lib_marker_is_the_last_key`). In `ff_identity.c::announce_object` the new `cJSON_AddStringToObject(root, "reset_reason", ...)` likewise goes after `capabilities` and before the `lib_marker` line.
