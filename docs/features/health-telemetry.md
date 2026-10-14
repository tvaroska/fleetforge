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
| R4-FW-3 | Agent + library forward firmware log lines on `up/log` (explicit call; ring buffer; simulator; Basic/basic_idf forward `morse:`). Touches CRITICAL `Fleetforge.cpp`. Needs Patches A+B (*up/log proposal*) | P1 | 1d |
| R4-BE-1 | Ingest + persist health/telemetry with SPEC retention (30d telemetry, 7d logs). Expose via API + SSE | P0 | 1.5d |
| R4-FE-1 | Live fleet status dashboard: online/offline, last-seen, boot-success, metrics | P0 | 2d |
| R4-FE-2 | Per-device log tail (newest first, level, text, dropped gaps) | P1 | 1d |
| R4-TEST-1 | E2E: metric emitted on device → visible live in dashboard | P0 | 0.5d |

R4-BE-1's `up/log` half follows *up/log proposal* (its own table + migration, bounded on write like `progress.py`, `_sanitise_detail` on `text`, `logged_at = received_at − age_ms`, plus an age sweep or predicate so a silent board's lines still expire).

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

### up/log proposal (S0-fw-5, proposed)

Proposal only; nothing is built. `spec/` is protected, so the patches are paste-ready here (the `reset_reason` and `lib_marker` precedent).

- **Decision:** the build is R4 (R4-FW-3, R4-BE-1, R4-FE-2); Sprint 0 is bricking, auth and security, and a missing remote log is none of these. The server already tolerates `up/log` (`ingestor/handlers.py` only moves `last_seen`), so R0 shipped without it harmlessly.
- **On the wire:** `up/log`, QoS 0, not retained (unchanged from the table). One payload is a batch: `{"dropped": n, "lines": [{"age_ms", "level", "text"}, ...]}`, oldest first. `proto` stays 1 (additive: the channel was declared, its payload never specified, no agent <= 0.6.0 publishes it).
- **What gets sent is the firmware's choice:** an explicit library call. Nothing is forwarded by default. `esp_log_set_vprintf` would miss `printf`/`Serial.printf` (where the `morse:` line comes from) and recurses, because esp-mqtt and mbedTLS log while publishing.
- **Limits:** `text` <= 200 bytes, a payload < 2048 bytes, at most one publish per 5 s while lines flow. This bounds a chatty board at about 400 B/s. esp-mqtt's default buffer is 1024 B: R4-FW-3 must check whether it raises `buffer_size` or caps at 1 KB (measure in QEMU). The server drops payloads > 8192 B whole.
- **Time:** `logged_at = received_at - age_ms` (a negative `age_ms` is treated as 0). The device needs no wall clock. `age_ms` is the name `docs/SWARM.md` 5.1 proposes for `up/telemetry`, so R4-FW-2 can adopt it unchanged.
- **Retention:** 7 days (unchanged) and the newest 5 000 lines per device (new bound, Patch B). Worst case at 5 boards is about 25 000 rows, 7-8 MB, so a looping board cannot fill the shared prod VM. Mechanism (R4-BE-1): trim count on write (the `progress.py` idiom). A silent board never writes again, so the 7-day rule also needs a sweep or an age predicate in the same trim.
- **Security:** the broker ACL already limits a device to its own `up/#`, so a line cannot be forged for another board. `text` is untrusted (sanitise like `deploys.py::_sanitise_detail`); the server never echoes it into its own logs; the read API is admin-only. Lines never carry passphrases, credentials, enrolment tokens or signed URLs.
- **Named gaps:** a reboot loses unsent lines (RAM ring), so a panic's last words are lost (a later RTC-noinit ring or core dump could fix it); QoS-0 loss in flight is invisible; plain `printf`/`Serial.printf` lines are not forwarded unless the maker calls the library; "Watch a board" pulses EN and reboots the board (S0-bug-3), so a running board's log cannot be read without a restart. That friction is real but not Sprint 0 class; the owner can pull R4-FW-3 forward.
- **Rejected:** build in S0 now (edits the near-frozen protocol before acceptance, and needs a migration R4-BE-1 owns); forward all ESP_LOG output; tee the UART/USB-CDC stream; a server-side log level; a per-batch `seq`; device wall-clock timestamps (against *Clock*); keeping a dead board's lines beyond 7 days (a retention change the prd does not promise).
- **Tests later:** R4-FW-3 and R4-BE-1 pin the spec example against the simulator/ingestor with a spec-reading test (the `test_library_marker.py` idiom). Patches A and B have no test that turns red, so the owner can apply both together at any time.
- **Dry-run result (throwaway worktree, 2026-10-14):** Patches A1-A3 + B applied to a detached worktree copy (text pasted from this section): the six spec-reading test files give 253 passed, 0 failed, the same count as the unpatched tree; the structural check (section order, example keys, 2048 B bound, topic row, open item gone, prd row) printed `UP-LOG PATCH OK`.
- **Patch A** (owner, `spec:` commit), `spec/device-protocol.md`, three hunks.

  **A1, Topics table.** Replace the row `| `up/log` | 0 | no | optional diagnostics |` with:

  ```
  | `up/log` | 0 | no | firmware log lines, batched (R4) |
  ```

  **A2, new section.** Insert directly after the `### `up/hb` — heartbeat` section (after "A sleepy device sends one per wake." and its blank line), before `### `dn/cmd` — commands`:

````markdown
### `up/log` — firmware log lines

```json
{"dropped":0,"lines":[{"age_ms":5120,"level":"I","text":"morse: HELLO (firmware 1.0.1)"},{"age_ms":0,"level":"W","text":"sensor: no reply"}]}
```

Log lines the firmware chose to send, batched, oldest first. A device sends nothing here
unless its firmware asks the agent to: which lines go out is the device's choice, never
the server's.

- `lines`: one entry per line, oldest first, at least one.
  - `text`: one line of UTF-8, no line break, at most 200 bytes (the device truncates).
  - `level`: the ESP-IDF letter, `E`, `W`, `I`, `D` or `V`. An open vocabulary; absent
    means `I`.
  - `age_ms`: milliseconds on the device's monotonic clock from the line being logged to
    this payload being built. The server stamps the line `receipt time − age_ms` (see
    *Clock*); it is never a wall-clock time. Absent means `0`.
- `dropped`: lines the device discarded since its last `up/log` publish, because its
  buffer filled while offline or it hit its own rate limit. Absent means `0`. A QoS 0
  publish lost in flight is not counted, because the device cannot know about it.

A device keeps one payload under **2048 bytes** and publishes at most one every **5 s**
while lines are flowing. It buffers in RAM while disconnected and flushes on reconnect,
so a reboot loses lines not yet sent. QoS 0, because a log line is not worth a retransmit
or a persistent-session slot. A log line never carries a passphrase, credential, enrolment
token or signed URL.

The server treats `text` as untrusted: it strips control characters, redacts URLs and
bounds the length. It drops a payload over 8192 bytes, or one it cannot decode, whole and
without refusing the device. It keeps lines as [prd.md](prd.md) → *Retention* says. Like
every live `up/` message, an `up/log` moves `last_seen`. The fields are additive under
*Evolution rules*, rule 2, and `proto` stays `1`.
````

  **A3, Open items for R0.** Delete the bullet `- Whether `up/log` ships in R0 or waits for R4.` (answered: R4, by S0-fw-5). Leave the dynsec bullet and the italic note.

- **Patch B** (owner, same `spec:` commit), `spec/prd.md` -> *Retention*. Replace the row `| Device logs (`up/log`) | **7 days** |` with:

  ```
  | Device logs (`up/log`) | **7 days**, and at most the newest **5 000 lines** per device |
  ```
