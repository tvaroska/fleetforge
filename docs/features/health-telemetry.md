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
| R4-SPEC-1 | Propose the `proto: 1` additive reservations: absent `artifact.type` means `app`, `sub_versions`, a presence lease, `link_type` `dock`, `min_free_heap` (proposal only; *proto: 1 reservations proposal*) | P1 | 0.5d |
| R4-FW-1 | `up/hb` gains `min_free_heap`; `boot_ok` semantics pinned (false only while PENDING_VERIFY, read per heartbeat); spec Patch B; simulator sends both (*min_free_heap and boot_ok as built*) | P0 | 1d |
| R4-FW-2 | User-defined telemetry channel, `up/telemetry` with `age_ms`; `ff_telemetry()`/`Fleetforge.metric()`; proposal Patch A owner (*up/telemetry proposal*, *up/telemetry as built*) | P1 | 1.5d |
| R4-FW-3 | Agent + library forward firmware log lines on `up/log` (explicit call; ring buffer; simulator; Basic/basic_idf forward `morse:`). Touches CRITICAL `Fleetforge.cpp`. Needs Patches A+B (*up/log proposal*) | P1 | 1d |
| R4-BE-1 | Persist reset reason (+ fault counts derived on read), heartbeat health, telemetry and logs with SPEC retention (30 d telemetry, 7 d logs, 5 000 lines/device bound on write); `GET /v1/devices` fields, `/logs` and `/telemetry` sub-resources, `reset_reason` on `device.announce` (*R4-be-1 as built*; telemetry cap Patch proposed) | P0 | 2d |
| R4-FE-1 | Live fleet status dashboard: online/offline, last-seen, boot-success, metrics (a Health column; *R4-fe-1 as built*) | P0 | 2d |
| R4-BE-2 | `device.deploy` SSE frames carry `cmd_id`, `state`, `is_terminal` and a terminal `detail` so a CI waiter blocks on a frame instead of polling (*R4-be-2 as built*) | P1 | 0.5d |
| R4-FE-2 | Per-device log tail (newest first, level, text, dropped gaps; *R4-fe-2 as built*) | P1 | 1d |
| R4-TEST-1 | E2E: metric emitted on device → visible live in dashboard | P0 | 0.5d |

R4-BE-1's `up/log` half follows *up/log proposal*: its own table (migration 0009), bounded on write like `progress.py`, `deploys.sanitise_device_text` on `text`, `logged_at = received_at − age_ms`, and an hourly sweep plus an age predicate on every read so a silent board's lines still expire. Built: see *R4-be-1 as built*.

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

Since 2026-10-15 it is built (Patch A by the owner in 74b6bcb, Patch B and the code by S0-fw-6): see *reset_reason as built (S0-fw-6)* below.

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

### reset_reason as built (S0-fw-6)

- **What was built.** `ff_identity.c` reads `esp_reset_reason()` once in `ff_identity_init()`, maps it with an explicit `switch` (`reset_reason_name`, the 15 IDF 5.5 names lowercased without `ESP_RST_`, `default` and `ESP_RST_UNKNOWN` -> `unknown`, never a number), logs `ff-id: reset_reason <name> (announced)` before the `device_id` line (no 12-hex run, so `diagnostics.ts` is unaffected), and `announce_object` emits `"reset_reason"` after `capabilities` and before `lib_marker`, which stays last. The agent, the ESP-IDF example (`fleetforge_start.c`) and the Arduino library (`Fleetforge.cpp`) all call `ff_identity_init()` and share the file, so the three boot paths announce it identically with no new call in any of them (the `test_worked_example.py` parity tests are untouched and green). The enroll body is the same cJSON object, so it carries the field too. Patch B applied to `spec/device-protocol.md` in the same commit. `proto` stays 1; agent version not bumped (a release does that).
- **Server.** `announce_fields.normalize_reset_reason`: a string of 1-16 characters with no control character or lone surrogate passes as-is (unknown names included, Evolution rule 2); not a string, longer than 16, or control/surrogate -> `None` with the usual one-line `unusable ... stored as null` INFO; `""` -> `None` silently. `AnnouncePayload.reset_reason` uses it, so a malformed value never costs the announce. `handlers.py` logs `device <id> announced reset_reason=<value|null>` on a **live** announce of a registered device only (a retained replay repeats an old boot's reason on every ingestor reconnect). No column, no migration, no API field: `store.ANNOUNCE_FIELDS` leaves it out (`test_reset_reason_is_never_stored`). `EnrollRequest` has `extra="ignore"` and no such field: verified by test with `poweron`, `sw`, `7`, a 40-char string and `null`, all 200.
- **Simulator.** `DeviceIdentity.reset_reason` defaults to `poweron`; `StageRunner` rebinds it to `sw` on the apply reboot and on the confirm-timer rollback (both are `esp_restart()` on metal). A sleepy board's later wakes still say `poweron` (metal would say `deepsleep`); not modelled, as no sleepy agent exists yet.
- **Tests.** `tests/test_reset_reason.py` (new: the spec's 16 names = the firmware's map, each `ESP_RST_` + upper(name); `unknown` default; key between `capabilities` and `lib_marker`; read once, in `ff_identity_init`, logged before `device_id`; all three boot paths reach `ff_identity_init`; the spec example ends `capabilities, reset_reason, lib_marker`; the normaliser), plus additions in `test_ingestor.py` (live logs, retained does not, unknown as-is, 7 malformed shapes log `null` and keep `fw_version`, absent logs `null`, never stored), `test_simulator.py` (`poweron` -> `sw` across apply and rollback, key order) and `test_enroll.py`. `TestAnnounceMatchesTheSpec` and the `lib_marker`-last tests are green with Patch B.
- **T1.** `just test`: ruff, ruff format, mypy clean; 1783 passed. `just agent-build` for esp32, esp32s3, esp32c3, esp32c6: `BUNDLE OK`, 0 warnings each; the app grew +416 / +384 / +464 / +480 B, so `APP_SIZE_BUDGET_BYTES` (`tests/test_agent_power_and_size.py`) is raised to the exact measured bytes (the gotcha: the first `just test` ran against the old bundles and was green; it only failed once `agent/dist/` held the new ones). `just lib-build esp32` (Arduino, `ff_identity.c` recompiled): SUCCESS, no warnings.
- **T2 (QEMU, local dev stack only, nothing deployed or published).**
  - `poweron`: `just agent-qemu esp32 --fresh` -> `rst:0x1 (POWERON_RESET)`, `I (3426) ff-id: reset_reason poweron (announced)`, announce on the wire ends `"capabilities":["ota"],"reset_reason":"poweron","lib_marker":1}`; ingestor: `device 000000000000 announced reset_reason=poweron` (the retained replay at its restart logged nothing).
  - `panic`: board A (this tree, 0.5.0) staged a `FF_FAULT_TEST=bootloop` image of this tree (0.5.96-bltest, `on_command`, override `no_library_marker`), power-cycled: the bltest image logged `abort() was called`, `rst:0xc (SW_CPU_RESET)`, the bootloader went back to ota_0, and A logged `reset_reason panic (announced)`, `rolled_back (returned to ota_0; ota_1 did not confirm)`; ingestor `announced reset_reason=panic`. **Not the literal criterion:** the abort is in `app_main` before the network starts, not after connect (see gotcha).
  - `sw`: A staged a `FF_FAULT_TEST=hang` image of this tree (0.5.95-hangtest), power-cycled; at 301959 ms `no working session 300 s after an OTA boot — ... rolling back`, `esp_restart()`, `rst:0xc`, A on ota_0 logged `reset_reason sw (announced)` and `rolled_back`; retained announce on the broker ends `"reset_reason":"sw","lib_marker":1}`; ingestor `announced reset_reason=sw`. **Not the literal criterion:** this is the OTA *rollback* reboot (the confirm timer's `esp_restart()`), not the *apply* reboot.
- **Gotcha: why the literal variants cannot run in QEMU.** Every software reset (an `esp_restart()` or a panic) issued after the network has run panics on the next boot in `esp_timer_impl_init -> esp_intr_alloc` before `app_main` (the known harness limit, runbook *The emulator cannot survive `esp_restart()`*), so the first app line never runs and nothing is announced. Reproduced three ways here: the apply reboot of 0.5.90 (`apply: auto`), and a throwaway (not committed) build with `abort()` right after `announce acknowledged`; both loop on `InstrFetchProhibited, PC 0` until killed, and a kill is a power-on. Images that reset before the network ever ran (bootloop, hang) survive the soft reset, which is what the T2 above uses. New detail from decoding it: the fault is `timer_alarm_isr` firing inside `esp_intr_alloc` before `s_alarm_handler` is set (jump to 0), and at core init after the soft reset timer group 0 still holds `int_raw 0x8` (LACT) and `int_ena 0xc` that a cold boot has at 0. Throwaway shims that cleared stale CPU `INTENABLE`, quiesced the openeth NIC, or quiesced TG0 LACT before esp_timer init did not cure it, so the root cause is still open. An `esp_eth_stop` in a shutdown handler before `esp_restart()` did not either.
- **Named gap.** `ext`, `usb`, `brownout`, `*_wdt` and the C3's console-reset reading are unmeasured (no bench). Whether the dashboard console's EN pulse reads `poweron` or `usb` on each chip is the S0-bug-3 question this field exists to answer on the next repeat.

### min_free_heap and boot_ok as built (R4-fw-1)

- **What was built.** `ff_identity_heartbeat_json()` adds `"min_free_heap": esp_get_minimum_free_heap_size()` (the heap low-water mark since this boot, bytes) between `free_heap` and `boot_ok`, so the hb keys are exactly the spec example's, in order: `fw_version, uptime_s, rssi, free_heap, min_free_heap, boot_ok`. It is the one hb builder for the agent, the ESP-IDF example and the Arduino library, so all three send it with no other file touched (`Fleetforge.cpp`, `fleetforge_start.c`, `agent_main.c`, `ff_mqtt.c` unchanged). `boot_ok` was already sent; its body is unchanged and its stale R0 comment now states the semantics below. `proto` stays 1; agent/library version not bumped. Server: nothing (`HeartbeatPayload` is `extra="ignore"` and reads only `fw_version`; storing health is R4-BE-1).
- **`boot_ok` semantics: "the confirm path has run, or there was none to run".** True unless the running slot's otadata state is `ESP_OTA_IMG_PENDING_VERIFY`, read fresh on every heartbeat.

  | Boot | otadata state | boot_ok |
  |------|---------------|---------|
  | serially flashed (USB) | UNDEFINED / VALID | true (no confirm window) |
  | OTA boot, before the announce PUBACK | PENDING_VERIFY | **false** |
  | OTA boot, after `confirm_this_image()` | VALID | true |
  | OTA boot whose confirm timer fired (rolling back) | PENDING_VERIFY | false |
  | image the bootloader never armed (TXN_BOOTED_NEW) | NEW | true (nothing can roll it back) |

  The heartbeat task starts in `on_connected()` and first publishes `hb_s` after that connect, so a healthy OTA boot has normally confirmed by its first hb (`true`); an image stuck in its confirm window reads `false` until it rolls back.
- **Rejected.** (1) `false` for `ESP_OTA_IMG_NEW` too: an image OTA'd by a pre-0.4.0 agent onto a rollback-less bootloader is never marked valid, so it would report `false` forever on a working board. (2) Deriving it from `ff_mqtt.c`'s `session_confirmed`: couples a health read to the CRITICAL confirm path, and a serially flashed board would be "not ok" until its first PUBACK. (3) Caching it once at boot like `reset_reason`: an OTA boot that confirmed would say `false` for its whole life.
- **Simulator.** `DeviceIdentity.heartbeat(uptime_s, *, boot_ok=True)` sends `min_free_heap: 98000` (the spec example's value, `<= free_heap`). `StageRunner.boot_ok` is false only while a `pending` transaction is the image this board rebooted into (`pending is not None and not reboot.is_set()`: an apply in the current session sets both, and the image still running is the old, confirmed one). `_heartbeat_loop` takes a `boot_ok` callable and calls it every beat. `--confirm auto` clears `pending` in `on_boot()` before the first hb (`true`); `--confirm never` or a broken-marker image sends `false` until the confirm timer rolls back, and the image it returns to sends `true`.
- **Tests.** `tests/test_heartbeat_health.py` (new): the spec hb example parses, `min_free_heap` is an int `<= free_heap`, `boot_ok` a bool; the firmware's hb keys equal the example's in order; `min_free_heap` is `esp_get_minimum_free_heap_size()`; `boot_ok()` is called in the builder, compares only against `ESP_OTA_IMG_PENDING_VERIFY` (no `ESP_OTA_IMG_NEW`) and has no static cache; the simulator's keys equal the example's; the ingestor decodes the example. `TestAnnounceMatchesTheSpec::test_heartbeat_carries_the_spec_fields` now uses the parsed key list. `test_simulator.py`: `CONFIRM_NEVER` session hb `boot_ok:false` on the new image then `true` on the image it returned to; `CONFIRM_AUTO` `true`; `StageRunner.boot_ok` across an apply. Pin check: deleting the `min_free_heap` line from `ff_identity.c` fails `test_the_firmware_builds_exactly_the_spec_keys` and `test_min_free_heap_is_the_idf_low_water_mark`.
- **Flash budget.** All four targets rebuilt (`BUNDLE OK`, 0 warnings): esp32 1,031,056 (+128 B), esp32s3 1,011,056 (+112 B), esp32c3 1,070,672 (+112 B), esp32c6 1,120,208 (+96 B); `APP_SIZE_BUDGET_BYTES` raised to those exact bytes. `just lib-quickstart --build-only`: PASS (Arduino Basic esp32/esp32s3 and ESP-IDF basic_idf esp32/esp32s3, 0 warnings from the component). `just test`: 1796 passed.
- **T2 (QEMU, local dev stack only, nothing deployed).** `just agent-qemu esp32` (Ethernet, `--hb 10`; this box serves the stack on `FF_HTTP_PORT=8088`, so `--api-base http://10.0.2.2:8088`): six hbs 10 s apart, each mechanically checked for the exact key order, `0 < min_free_heap <= free_heap`, `rssi` null and `boot_ok` true (serially flashed). One line: `{"fw_version":"0.5.0","uptime_s":20,"rssi":null,"free_heap":227384,"min_free_heap":220764,"boot_ok":true}`. Ingestor: `ingested device.heartbeat device=000000000000`, no undecodable payload. Live simulator: `{"fw_version":"1.4.2","uptime_s":5,"rssi":-61,"free_heap":142000,"min_free_heap":98000,"boot_ok":true}`.
- **Not proven on the wire:** `boot_ok:false` from firmware inside a real OTA confirm window. In QEMU an OTA reboot after the network has run panics before `app_main` (the harness limit, *reset_reason as built*), so only the serially flashed `true` is observed; the `false` branch is pinned by the source-reading test and the simulator.
- **Spec.** Patch B (the hb example gains `"min_free_heap":98000`) applied in this commit. Patch A2 (the `min_free_heap` prose paragraph after the hb fence) is still the owner's, with A and C.

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

### proto: 1 reservations proposal (R4-spec-1, proposed)

Proposal only; nothing is built. `spec/` is protected, so the patches are paste-ready here (the `reset_reason`, `up/log` and `lib_marker` precedent).

- **Source:** focus-group item 9 (synthetic personas, DECISIONS 2026-10-15) and `docs/SWARM.md` 5.2-5.4. `spec/open-questions.md` -> *`artifact.type` on `dn/cmd` `stage`* already asked for exactly the "absent means `app`" sentence.
- **What each reservation buys, and why it is safe.** All are additive under *Evolution rules*, rule 2, and `proto` stays `1`. No agent <= 0.5.x sends any of the new fields, and the server already ignores unknown fields, so nothing changes on the wire today.
  - `artifact.type` absent means `app`; `mcu_fw`, `model_weights`, `fpga_bitstream` reserved. Fixes the meaning of absence before anyone assigns another.
  - `up/announce.sub_versions`: component name -> version string, for a device that updates other processors.
  - `up/presence` `reason` and `expected_back_s`: a retained "planned absence" lease (a drone leaving its dock).
  - `link_type` `dock`: a wired bus to a carrier or gateway (V3). `link_type` is already an open vocabulary: the column is TEXT, `LinkType` is advisory, and `test_ingestor.py::test_unknown_link_type_is_written_through` stores `satellite`. `ff_cfg.LINKS` is the flash-config `link` (wifi/ethernet), a different thing, not touched. Adding `DOCK` to `LinkType` is a later build.
  - `up/hb.min_free_heap`: `esp_get_minimum_free_heap_size()`, bytes, the low-water mark since boot, beside `free_heap`. Documents what R4-FW-1 will send.
- **Test shape (why each hunk is shaped as it is).** `TestAnnounceMatchesTheSpec` parses every `"key":` in the announce JSON example and requires `ff_identity.c` to emit it, so `sub_versions` is **prose only**; `test_the_spec_example_ends_with_lib_marker` and `test_the_spec_example_puts_it_last_but_one` pin the example's tail, so Patch A does not touch the example at all. `test_stage_payload_is_exactly_the_spec_keys` pins the server's stage payload (the server keeps omitting `artifact.type`), so the `dn/cmd` example is untouched too. `test_heartbeat_carries_the_spec_fields` uses a hard-coded key list, so adding `min_free_heap` to the hb example turns nothing red; it is still **Patch B, applied in R4-FW-1's commit** (the `reset_reason` rule: the example shows what the firmware sends, and R4-FW-1 adds the spec-parsing hb test).
- **Old-agent hazard (written into A4).** `agent/components/fleetforge/src/ff_mqtt.c` reads only `url`, `sha256`, `version` and `size` from `artifact`. A deployed agent sent `"type":"mcu_fw"` would write the blob into its OTA slot as an app. So a server sends a type other than `app` only to a device whose `capabilities` names a handler. `artifact.type` is nested and unrelated to the command's own top-level `type` (`stage`).
- **Presence lease and the LWT (written into A3).** The lease is a retained publish on `up/presence`. If the device then drops uncleanly, the LWT fires and its bare `{"online":false}` replaces the retained lease. A clean MQTT `DISCONNECT` suppresses the will (MQTT 3.1.1), so the device must disconnect cleanly after publishing. `PresencePayload` is `extra="ignore"` with only `online`, so a v1 server reads the lease as plain offline. A parent publishing for its child needs the V3 hierarchy ACLs.
- **Order-independent with the `up/log` Patch A.** Its A2 inserts after "A sleepy device sends one per wake." and its blank line. Our `min_free_heap` paragraph (A2 below) goes directly after the hb JSON fence, before "Interval from `dn/cfg`", so the two never share an anchor; the dry-run applies both orders and the results are byte-identical. up/log A3 deletes an *Open items* bullet; this proposal does not touch *Open items*.
- **Not proposed: the `device_id` widening** (`^[a-z0-9_-]{1,32}$`). It is a server schema change, noted and **not built**. The format is enforced in `identity.py::DEVICE_ID_RE`; the DB CHECK `devices.device_id_format` (`db/models.py`, `alembic/versions/0001_initial_schema.py`: a migration, CRITICAL); the `mosquitto/acl` comment (CRITICAL); topic parsing (`ingestor/protocol.py`); the frontend (`flash.ts`, `diagnostics.ts`, `boardName.ts`); and the agent's `FF_DEVICE_ID_LEN`. **Service-username collision:** `device_id` is the MQTT username, and that shape admits the broker's service usernames (`MOSQUITTO_*_USERNAME` in `mosquitto/configure.sh`); an enroll claiming one would make dynsec provisioning act on a service client. A 12-hex id can never collide today. Any widening needs a reserved-name rule (or a disjoint shape) before it ships. `spec/open-questions.md` -> *`device_id` format* already argues against widening in V1; the focus-group regex is a candidate for when a non-ESP32 platform exists (Patch C2 records this).
- **Rejected:** `sub_versions` in the announce example (reddens the parity test, and no agent sends it); a `type` in today's stage payload (the server has one kind); reserving capability names for the handlers now (no handler exists to name); a new `power_class` value for "sortie" (SWARM 5.2's own point: it is presence, not power).
- **Dry-run result (throwaway worktrees, 2026-10-15):** Patches A+C: the ten spec-reading test files (`test_agent_board_measurements`, `test_agent_known_networks`, `test_agent_partitions`, `test_arduino_library`, `test_deploy_precheck`, `test_ff_cfg`, `test_library_marker`, `test_reset_reason`, `test_broker_commands`, `test_presence`) give 318 passed, 0 failed, the same count as the unpatched tree; A+B+C: 318 passed, 0 failed. Order check: the `up/log` Patch A1-A3 then these A+B+C, and the reverse, every anchor found exactly once in both orders, and the two patched `spec/device-protocol.md` and `spec/open-questions.md` are byte-identical (`cmp`). The throwaway script took the patch text from this section and asserted: announce example keys unchanged (`reset_reason` last but one, `lib_marker` last), `sub_versions` in prose only, `dn/cmd` example unchanged and the `artifact.type` bullet after the `artifact.sig` bullet, the presence paragraph before `## Messages`, the `min_free_heap` paragraph between the hb fence and "Interval from `dn/cfg`", the hb example parsing as JSON with `min_free_heap` and `boot_ok` (Patch B), and *Open items for R0* byte-identical; it printed `R4-RESERVATIONS PATCH OK`.
- **Patch A** (owner, `spec:` commit), `spec/device-protocol.md`, four hunks, each hunk in its own block.

**A1, `### up/announce — identity`.** Insert these two paragraphs directly after the `reset_reason` paragraph (ending "...The enroll body carries it like every other announce field."), before `#### Partition layouts`:

```markdown
`link_type` names the link this broker session runs over. The agent sends `wifi` or
`ethernet`. It is an open vocabulary: the server stores a value it does not know as-is.
`dock` is reserved for a wired bus between a device and the carrier or gateway it docks on
(V3). A link that is not Wi-Fi reports `ssid` and `known_networks` as `null`, as
`ethernet` does.

`sub_versions` is reserved for a device that updates processors besides its own: an object
mapping a component name to the version string that component runs, one level deep, for
example `{"fc_fw": "4.5.1", "vision_model": "0.2.0"}`. It never repeats `fw_version`. No v1
agent sends it. Absent means none reported. It is additive under *Evolution rules*, rule 2,
and `proto` stays `1`; the server ignores it until a release defines what it stores.
```

**A2, `### up/hb — heartbeat`.** Insert directly after the closing fence of the hb JSON example, before the line "Interval from `dn/cfg`; ...":

```markdown
`min_free_heap` is the lowest free heap since this boot, in bytes
(`esp_get_minimum_free_heap_size()`), beside the current `free_heap`. A value that keeps
falling across heartbeats is the leak a board shows before it resets. It is additive under
*Evolution rules*, rule 2, and `proto` stays `1`. It is absent from agents that predate it,
and the server treats absent and `null` alike.
```

**A3, `## Presence — derived, never a socket state`.** Insert after the paragraph ending "...never the device's timestamp — see *Clock* below.", before `## Messages`:

```markdown
**Planned absence (reserved).** A device about to go quiet on purpose, for example a drone
leaving its dock, may first publish its own retained presence:
`{"online":false,"reason":"sortie","expected_back_s":1200}`. `reason` is an open vocabulary
the server shows as-is. `expected_back_s` is how many seconds after receipt it expects to be
back (receipt time, see *Clock*). The device then closes its session with a clean MQTT
`DISCONNECT`: an unclean drop fires the LWT, whose bare `{"online":false}` replaces the
retained lease. No v1 agent sends either field, and a v1 server treats the message as plain
`online:false`. Both are additive under *Evolution rules*, rule 2, and `proto` stays `1`.
Telling "away" from "overdue" is a later server's job. A parent publishing for its child is
V3 (*Hierarchy*).
```

**A4, `### dn/cmd — commands`.** Add one bullet after the `artifact.sig` bullet (ending "...required on boards that announce signature verification."):

```markdown
- **`artifact.type` is absent in v1, and absent means `"app"`**: the device's own
  application image, written to its inactive OTA slot. `mcu_fw` (a co-processor's
  firmware), `model_weights` and `fpga_bitstream` are reserved for artifacts a device hands
  to another processor or store it manages. A device that predates the field ignores it and
  would treat any artifact as an app, so a server sends a type other than `app` only to a
  device whose `capabilities` names a handler for it. It is unrelated to the command's own
  `type`.
```

- **Patch B** (R4-FW-1's commit only, `spec/device-protocol.md`). Replace the `up/hb` JSON example line with the block below. It stays out of the owner's `spec:` commit so the spec example never shows a key the firmware does not yet send; R4-FW-1 also adds the test that parses the spec's hb example. `boot_ok` is already in the example and already emitted by `ff_identity.c`; R4-FW-1's "boot-success flag" is about its semantics, not a new key.

**B, hb example line.**

```json
{"fw_version":"1.4.2","uptime_s":81234,"rssi":-61,"free_heap":142000,"min_free_heap":98000,"boot_ok":true}
```

- **Patch C** (owner, same `spec:` commit as A), `spec/open-questions.md`, two sentences. Each is appended to the end of the named paragraph.

**C1, the `artifact.type` paragraph** (ending "...so the type may never need to cross the wire."):

```markdown
Answered (R4-spec-1): `device-protocol.md` → `dn/cmd` says absence means `app` and
reserves `mcu_fw`, `model_weights` and `fpga_bitstream`.
```

**C2, the `device_id` format paragraph** (ending "...no device recall is involved."):

```markdown
The 2026-10-15 focus group proposes `^[a-z0-9_-]{1,32}$`; that shape admits the broker's
service usernames, so any widening needs a reserved-name rule first (R4-spec-1, not built).
```

### up/telemetry as built (R4-fw-2)

- **What was built.** Firmware: `include/ff_telemetry.h` (the maker API, one function) and `src/ff_telemetry.c` (new): a static ring of the newest 64 samples (`seq`, `esp_timer` time, value, name; 3.5 KB BSS) under a `portMUX` spinlock held for struct copies only; a flush task `ff_tel` (4096 B, prio 4) created on the first connect and never restarted, which drains back to back on every (re)connect notify and otherwise publishes at most one payload per 5 s; publish-then-commit (`s_unsent` moves only after `esp_mqtt_client_publish(..., 0, 0)` returned >= 0); hand-written JSON at most 900 B, values printed `%.15g` or `%.17g` (cJSON's round-trip rule). `src/ff_telemetry_internal.h` holds the two session hooks. `ff_mqtt.c`: `topic_telemetry` spelled with the other topics, `ff_telemetry_session_up()` as the last statement of `on_connected()`, `ff_telemetry_session_down()` in `MQTT_EVENT_DISCONNECTED`; the PUBLISHED/confirm branch, the confirm timer and `classify_txn` are untouched. Arduino: `bool Fleetforge.metric(const char *name, double value)` (a thin wrapper in the CRITICAL `Fleetforge.cpp`; only the include, the member and the `ff-tel` log tag were added). Both worked examples report `morse_loops`, one sample per blinked message (about 6.8 s). The library README has a *Telemetry* section. No version bump.
- **Server (log only; storage is R4-BE-1).** `clock.device_time(received_at, age_ms)`. `ingestor/protocol.py`: `TELEMETRY_NAME_RE`, `TELEMETRY_MAX_BYTES = 8192`, `TELEMETRY_MAX_AGE_MS` (30 days), `TelemetrySample` (strict: bool, string, non-finite and overflowing values, bool/float/over-age `age_ms` are unusable; negative or absent `age_ms` is 0) and `TelemetryPayload` (each raw sample validated on its own, the bad ones counted in `unusable`). `handlers.py`: `touch_last_seen` first, as before; then, live and registered only, a payload over 8192 B is logged and dropped, otherwise one INFO line `device <id> telemetry samples=N dropped=N unusable=N oldest_age_ms=N` and one DEBUG line per sample with its `device_time`. The event stays `device.seen`.
- **The maker-API rule.** `tests/test_ota_component.py` gains `MAKER_HEADERS = ("ff_telemetry.h",)`: `include/` minus the maker headers is exactly what `agent_main.c` includes; `agent_main.c` includes no maker header and calls none of their functions; both examples call every maker function (DECISIONS 2026-10-15, R4-fw-2).
- **Simulator.** `simulator/telemetry.py` (`TelemetryBuffer`, the firmware ring in Python, constants retyped and tripwired); `--telemetry-every SECONDS` (off by default) records `sim_counter` 1, 2, 3, ... on a fixed schedule for the board's whole life, connected or not; `_telemetry_loop` drains after `on_boot()` (identity and the outcome first), then publishes at most one payload per 5 s, QoS 0, `retain=False`; an apply reboot clears the buffer.
- **Tests.** `tests/test_telemetry.py` (65): the example (keys and order, oldest first, the proposal's two copies identical, the spec's copy once pasted), firmware pins (QoS 0/retain 0, 64/900/5000, `NAME_MAX` = the ingestor's `{0,30}` + 1, monotonic clock only, `isfinite`, the `ff_` refusal, no log/alloc/print in the record path, nothing but copies under the lock, commit after a successful publish), session wiring (after the announce and `report_txn_on_connect()`, down on DISCONNECTED, nothing near the confirm path), the public surface, the simulator buffer (ages, overflow, resend, commit, the 900 B split, refusals, cJSON number text), a sleepy session (asleep samples arrive on the next wake, QoS 0, consecutive values, 50 ms spacing), off by default, the CLI flag, the ingestor reading and four handler cases. Pin check: deleting `isfinite` fails `test_a_non_finite_value_and_the_reserved_prefix_are_refused`; QoS 1 fails `test_one_publish_at_qos_0_never_retained`.
- **T1.** `just agent-build-all`: four `BUNDLE OK`, 0 warnings. `APP_SIZE_BUDGET_BYTES` raised to the measured bytes: esp32 1,032,288 (+1,232 B), esp32s3 1,012,304 (+1,248 B), esp32c3 1,072,464 (+1,792 B), esp32c6 1,122,032 (+1,824 B). `just lib-quickstart --build-only`: PASS (Arduino Basic esp32 1,178,400 B / esp32s3 1,171,776 B, build B, the own-project path, ESP-IDF basic_idf esp32 1,106,240 B / esp32s3 1,111,408 B; 0 warnings from the component or the examples). `just lib-arduino-check`: PASS (arduino-cli 1.5.1, core esp32:esp32@3.3.12, the zipped IDE package with `ff_telemetry.c` flattened into `src/`: esp32 1,167,856 B and esp32s3 1,158,704 B, 0 warnings, `T verifyRollbackLater` in both ELFs). `just test`: 1864 passed; ruff, format and mypy clean.
- **T2 (local dev stack only; nothing deployed or published).** A watcher subscribed to `up/telemetry` and stamped every sample with the server's own `TelemetryPayload` decode and `clock.device_time(rx, age_ms)`; a judge checked the first batch after each outage (the drain's back-to-back payloads merged) for the expected count with `dropped` 0, consecutive values, every gap within ±0.3 s of the period, and the earliest sample stamped near the outage start.
  - (a) Simulator, sleepy (`--wake-interval 30 --awake-s 3 --telemetry-every 5`): three wakes judged, `TELEMETRY SPACING OK` each: 6, 7 and 6 samples, gaps 4.997-5.004 s, values 2-7, 8-14, 15-20 with no gap across wakes. One payload: `{"dropped":0,"samples":[{"age_ms":28206,"name":"sim_counter","value":2},{"age_ms":23203,"name":"sim_counter","value":3},{"age_ms":18203,"name":"sim_counter","value":4},{"age_ms":13203,"name":"sim_counter","value":5},{"age_ms":8203,"name":"sim_counter","value":6},{"age_ms":3203,"name":"sim_counter","value":7}]}`. Ingestor: `device f6597309b613 telemetry samples=6 dropped=0 unusable=0 oldest_age_ms=28206`.
  - (b) Simulator, always_on, `--telemetry-every 2` through `agent/tools/flaky_link.py` on 18883: the proxy killed for 40 s; the sim backed off and reconnected 61 s after the cut, then drained 31 samples in two back-to-back payloads (858 B and 705 B, both under 900), values 12-42 following the last pre-outage 11, gaps 1.998-2.003 s, earliest stamped 0.65 s after the cut: `TELEMETRY SPACING OK`.
  - (c) QEMU, the Arduino library board (`lib-qemu` builds `examples/Basic`), MQTT through the proxy, `--hb 10`: three live records (one sample each, period 6.82 s median), proxy killed (the serial log shows `mqtt disconnected; the client will reconnect`, slirp closed the guest socket), 60 s down, restarted; the board reconnected, re-announced and logged `ff-tel: publish ff/v1/d/000000000000/up/telemetry (qos 0, no retain, 10 samples, dropped 0, 507 B)`. Judge (period 6.819): 10 samples, values 4-13 after the last live 3, gaps 6.799-6.974 s, earliest stamped 2.1 s before the cut (the sample taken just before it), 62.9 s before arrival: `TELEMETRY SPACING OK`. Payload: `{"dropped":0,"samples":[{"age_ms":62890,"name":"morse_loops","value":4},{"age_ms":56070,"name":"morse_loops","value":5},{"age_ms":49096,"name":"morse_loops","value":6},{"age_ms":42276,"name":"morse_loops","value":7},{"age_ms":35394,"name":"morse_loops","value":8},{"age_ms":28546,"name":"morse_loops","value":9},{"age_ms":21743,"name":"morse_loops","value":10},{"age_ms":14912,"name":"morse_loops","value":11},{"age_ms":8113,"name":"morse_loops","value":12},{"age_ms":1312,"name":"morse_loops","value":13}]}`. Ingestor: `device 000000000000 telemetry samples=10 dropped=0 unusable=0 oldest_age_ms=62890`.
  - Stock agent (`just agent-qemu esp32`, no metric): hb keys unchanged, no `ff-tel` line (nothing buffered, nothing sent), `free_heap` 217,644 at uptime 20 against R4-fw-1's 227,384: about 9.7 KB, i.e. the 4.5 KB of BSS (ring plus payload buffer) and the idle 4 KB task with its TCB. The library board's hb is `free_heap` 170,068-171,684 with telemetry running.
- **Not proven here.** The ESP-IDF `basic_idf` path is proven by compile plus the shared component, not booted. Overflow on metal (`dropped > 0`) would need an outage over 7 minutes; the unit tests cover it. A QoS-0 sample lost in flight at the moment of an undetected link death is invisible by design; in (c) slirp closed the socket at once, so none was lost.
- **Named gaps / follow-ups.** The stock agent pays about 9 KB of RAM for a channel it never uses (BSS plus the idle task); creating the task lazily would need the record path to start it, which it must not (no allocation). `dropped` can over-count by samples displaced after a payload that already carries them was built (only when the ring is full during a build).

### up/telemetry proposal (R4-fw-2, proposed)

The spec declares the channel (`| up/telemetry | 0 | no | user-defined metrics (R4) |`) and leaves its payload open. This section is the payload and the public API names, paste-ready for the owner (the `up/log` and R4-spec-1 precedent). Unlike those two, the build ships in the same commit, unreleased (*up/telemetry as built*).

- **Decision:** build now, ahead of the owner's Patch A, because (1) the task's acceptance is a build acceptance (simulator and QEMU), unlike R4-FW-3, which is explicitly gated on its Patches A+B; (2) no `spec/` file is touched and no spec-reading test turns red; (3) nothing is released: `agent/version.txt`, `library.json` and `ff_lib_version.h` are not bumped, the names freeze at the next agent/library release, and Patch A is the gate before it, so a rename now is cheap; (4) the S0-bug-3 rejection ("building the field now edits the near-frozen protocol") was about an announce field pinned by spec-example parity tests, while this channel is already in the spec and only its payload is new.
- **On the wire:** `up/telemetry`, QoS 0, not retained (unchanged from the table). One payload is a batch, keys in this order:

  ```json
  {"dropped":0,"samples":[{"age_ms":13600,"name":"morse_loops","value":41},{"age_ms":6800,"name":"morse_loops","value":42},{"age_ms":0,"name":"battery_v","value":3.71}]}
  ```

  `samples` is oldest first, at least one entry. `age_ms` is the time on the device's monotonic clock (`esp_timer_get_time()`) from the sample being recorded to this payload being built; absent means 0, negative is read as 0. `value` is a finite JSON number. `dropped` counts samples the device discarded since its last successful publish (ring full while offline, or samples faster than one payload per 5 s can carry); absent means 0. QoS-0 loss in flight is not counted, because the device cannot know about it. It mirrors the `up/log` proposal on purpose (`dropped` plus a list, the same `age_ms`, the same limits), so R4-FW-3 can copy this module's ring and flush. `proto` stays 1.
- **Names and API** (additive-only once released):
  - ESP-IDF, new public header `include/ff_telemetry.h`: `esp_err_t ff_telemetry(const char *name, double value);`. `ESP_OK` when recorded (also when the record displaced the oldest buffered sample); `ESP_ERR_INVALID_ARG`, recording nothing, for a NULL or invalid name or a non-finite value. Any task, any time (before `fleetforge_start()`/`begin()`, while enrolling, while disconnected), **not from an ISR** (a later ISR-safe promise is additive). It never blocks, never allocates and never logs: R4-FW-3 will forward log lines, and a metric call that logs could recurse.
  - Arduino, `Fleetforge.h`: `bool metric(const char *name, double value);`, a `FleetforgeClass` member (`Fleetforge.metric("temp_c", 21.5)`), a thin wrapper over `ff_telemetry()`. One overload only: an `int` converts to `double` implicitly, and a second overload would be ambiguous.
  - Why these names: `ff_telemetry` matches the module and the topic, as `ff_logf` will match `up/log`; `metric` is what an Arduino user reaches for.
  - Name rule: 1-31 characters of `a-z`, `0-9` and `_`, starting with a letter (`^[a-z][a-z0-9_]{0,30}$`, a valid Prometheus metric name; Prometheus/Grafana export is on the post-v1 roadmap). The `ff_` prefix is reserved for the agent's own future metrics: `ff_telemetry()` refuses it, the server accepts it. A strict rule can be loosened later additively; loosening now and tightening later would be a recall.
- **Limits:** one payload is under 2048 B (spec text). The firmware keeps it at **900 B or less**, so the whole MQTT packet (topic `ff/v1/d/<12hex>/up/telemetry`, 33 B, plus the header) fits esp-mqtt's default 1024 B buffer without its large-message path. While connected it sends at most one payload every 5 s; on (re)connect it drains the backlog in consecutive payloads. The device keeps the newest 64 samples in RAM. The server drops a payload over 8192 B whole.
- **Time:** `sample_ts = received_at - age_ms` (`clock.device_time`, `docs/SWARM.md` 5.1). The board needs no wall clock, and a board whose SNTP failed still reports correctly timed samples. A sample's `age_ms` beyond 30 days (the retention window) makes that sample unusable.
- **Server reading:** an unusable sample (a name outside the rule, a value that is not a finite number or is a bool, an `age_ms` that is not an int or is a bool, an `age_ms` beyond 30 days) is skipped on its own and never costs the rest of the payload. A payload it cannot decode is dropped, and the device is never refused. `last_seen` moves as before, before any decode.
- **Security:** the broker ACL already confines a device to its own `up/#`, so a sample cannot be forged for another board. Names are validated against the rule and values are numbers, so nothing free-text from a board reaches a server log. No credential, URL or token is ever in a sample.
- **Named gaps:** a reboot loses unsent samples (RAM ring); QoS-0 loss in flight is invisible (a publish the socket accepted just before an undetected link death is gone); samples faster than about 15 per 5 s on a live session are dropped and counted; `dropped` can over-count by the samples displaced while a payload that already carried them was being built (only when the ring is full during a build).
- **Rejected:** (1) proposal only now, build later: the task's acceptance is a build acceptance, and the gate on the names is the release, not this commit; (2) cJSON for the payload: the size is known only after allocating and printing, and an exact 900 B bound is wanted; (3) a FreeRTOS mutex: lazy creation races, and a spinlock around struct copies is enough; (4) drop-newest when full: the newest samples are the most useful after a long outage; (5) a server-side metric whitelist: names are the firmware's choice; (6) a `seq` or a device timestamp on the wire: against *Clock*, and `age_ms` suffices (as `up/log`); (7) calling `ff_telemetry()` from `agent_main.c` to satisfy the public-surface test: it satisfies the regex and defeats the rule's intent; a stock-agent metric would invent product; (8) flushing from the heartbeat task: couples the cadence to `hb_s` (60 s) and adds code to the CRITICAL `ff_mqtt.c`; (9) QoS 1: the spec table says 0, and the outbox would grow with the backlog; (10) the names `ff_metric`, `ff_telemetry_record` and `Fleetforge.telemetry()`: the first breaks the module/topic match, the second is long for the one call a maker types, the third reads as a getter.
- **Dry-run result:** throwaway worktree, 2026-10-15, patch text taken from this file by script. This Patch A alone: 2 anchors, each found exactly once. With the `up/log` Patch A1-A3 + B: 6 anchors, each once, in both orders, and the two patched `spec/device-protocol.md` and `spec/prd.md` are byte-identical (`cmp`). With the R4-spec-1 Patch A + C: 8 anchors, each once, both orders byte-identical (`device-protocol.md`, `open-questions.md`). All three in four orders (`upl r4 tel`, `tel r4 upl`, `r4 upl tel`, `tel upl r4`): 12 anchors, each once, all three spec files byte-identical, and the message order is `up/hb`, `up/log`, `up/telemetry`, `dn/cmd`. The twelve spec-reading test files (`test_telemetry`, `test_heartbeat_health`, `test_reset_reason`, `test_library_marker`, `test_ff_cfg`, `test_agent_board_measurements`, `test_agent_known_networks`, `test_agent_partitions`, `test_arduino_library`, `test_deploy_precheck`, `test_broker_commands`, `test_presence`): 389 passed unpatched, 389 with this Patch A, 389 with all three. With this Patch A applied, `test_telemetry.py` reads the spec's section (`spec has section: True`) and `test_the_spec_section_once_pasted_is_the_proposal` passes.
- **Patch A** (owner, `spec:` commit), `spec/device-protocol.md`, two hunks.

  **A1, Topics table.** Replace the row `| `up/telemetry` | 0 | no | user-defined metrics (R4) |` with:

  ```
  | `up/telemetry` | 0 | no | user-defined metrics, batched samples (R4) |
  ```

  **A2, new section.** Insert directly before `### `dn/cmd` — commands`:

````markdown
### `up/telemetry` — user-defined metrics

```json
{"dropped":0,"samples":[{"age_ms":13600,"name":"morse_loops","value":41},{"age_ms":6800,"name":"morse_loops","value":42},{"age_ms":0,"name":"battery_v","value":3.71}]}
```

Numbers the firmware chose to report, batched, oldest first. A device sends nothing here
unless its firmware records a sample (`ff_telemetry()` in ESP-IDF, `Fleetforge.metric()` on
Arduino): which metrics exist is the device's choice, never the server's.

- `samples`: one entry per sample, oldest first, at least one.
  - `name`: 1 to 31 characters of `a-z`, `0-9` and `_`, starting with a letter (a valid
    Prometheus metric name). Names starting `ff_` are reserved for the agent's own metrics.
  - `value`: a finite JSON number.
  - `age_ms`: milliseconds on the device's monotonic clock from the sample being taken to
    this payload being built. The server stamps the sample `receipt time − age_ms` (see
    *Clock*); it is never a wall-clock time, so a board needs no clock to report one. Absent
    means `0`; a negative value is read as `0`.
- `dropped`: samples the device discarded since its last `up/telemetry` publish, because its
  buffer filled while offline or samples came faster than it may publish. Absent means `0`.
  A QoS 0 publish lost in flight is not counted, because the device cannot know about it.

A device keeps one payload under **2048 bytes**. While connected it publishes at most one every
**5 s**; right after a connect it drains what it buffered in consecutive payloads. It buffers in
RAM while disconnected, keeping the newest samples, so samples taken offline arrive after the
reconnect with their original spacing, and a reboot loses samples not yet sent. QoS 0, because
a sample is not worth a retransmit or a persistent-session slot.

The server skips a sample it cannot read (a name outside the rule, a value that is not a finite
number) without losing the rest of the payload, and drops a payload over 8192 bytes, or one it
cannot decode, whole and without refusing the device. It keeps samples as [prd.md](prd.md) →
*Retention* says. Like every live `up/` message, an `up/telemetry` moves `last_seen`. The fields
are additive under *Evolution rules*, rule 2, and `proto` stays `1`.
````

### R4-be-1 as built

- **What was built.** Migration `0009` (CRITICAL): seven nullable `devices` columns
  (`last_reset_reason`, `health_at`, `uptime_s`, `rssi`, `free_heap`, `min_free_heap`,
  `boot_ok`; no defaults, so no rewrite) and four plain tables, all `ON DELETE CASCADE`:
  `device_faults` (one row per fault boot), `device_logs` (`up/log` lines),
  `device_telemetry` (`up/telemetry` samples) and `device_metrics` (latest value per metric).
  New modules, each the owner of its table: `retention.py` (every number, plus `sweep`),
  `faults.py`, `device_logs.py`, `telemetry_store.py`. `deploys._sanitise_detail` is public as
  `sanitise_device_text` (one regex, two tables). The ingestor writes `last_reset_reason` on
  every announce (live or retained, absent -> NULL), the health group on every live heartbeat
  (malformed field -> NULL, never a lost heartbeat), a fault row on a live `panic` /
  `task_wdt` / `brownout` announce, and stores telemetry and log batches; each INSERT runs in
  a SAVEPOINT that logs a WARNING and keeps `last_seen` and the event on failure. The
  ingestor also runs the retention sweep (once at start, always logged; then hourly, logged
  when it deleted something). API: `GET /v1/devices` rows gain `last_reset_reason`,
  `fault_resets` (`window_s` = `fault_window_s`, default 86 400), `health` (null before the
  first live heartbeat) and `metrics` (32 newest by update, sorted by name), all before
  `deploy`, which stays last; the PATCH response carries the same. New admin-only
  sub-resources `GET /v1/devices/{id}/logs?limit=` (1..5000, default 200, newest first) and
  `GET /v1/devices/{id}/telemetry?name=&limit=` (name rule as the ingestor, 1..5000, default
  500). `DeviceEvent` gains `reset_reason` (set on `device.announce` only; `v` stays 1). The
  simulator gains `--reset-reason NAME` for the first boot of a run.
- **Fault counts and the reconnect rule.** Derived on read (count of rows in the window),
  never a stored counter. The agent re-announces on every broker connect, so a live
  heartbeat with `uptime_s` dates the boot (`floor = received_at - uptime_s - 1 s`) and deletes
  every fault row of the device at or after the floor but the earliest. All times are server
  receipt times; latency only moves the floor later, so an earlier boot's row is never
  deleted (a boot that died within about a second of announcing can instead cost the next
  boot's row, below). Named gap: reconnects in a boot that never heartbeats are counted.
- **Retention.** Logs: newest 5 000 per device trimmed on write, 7 days by sweep and read
  predicate. Telemetry: 30 days and the newest 500 000 samples per device, both by the sweep
  (trimming 500 k rows per batch would cost far more than the hourly pass; between sweeps a
  device can add at most ~11 k rows). Faults: 1 000 per device on write, 30 days by sweep.
  `device_metrics` rows older than 30 days are swept and hidden by the read.
- **Reconnect floor, one deviation from the plan.** The floor is `received_at - uptime_s -
  1 s`: `uptime_s` truncates, so without the second the boot's own first announce (sent
  within a second of the uptime origin, as the simulator's is) fell below the floor and the
  reconnect row survived as "the earliest" (seen on the dev stack: `panic` read 2 after a
  broker restart). The cost is named in `faults.py`: a fault boot that died and rebooted
  within about a second of announcing merges with the next boot (count low by one).
- **T2 evidence (dev stack, 2026-10-15).** Migration on the populated dev DB: 0008 -> 0009,
  149 devices before and after, 7 columns and 4 tables present; ingestor start logged
  `retention sweep deleted logs=0 telemetry_expired=0 telemetry_capped=0 metrics=0 faults=0`.
  Simulator `--reset-reason panic --telemetry-every 2 --heartbeat-interval 5` (device
  `1e60c99c020f`): SSE `"type":"device.announce",...,"reset_reason":"panic"`, heartbeats
  `"reset_reason":null`; the `/v1/devices` row:
  `{"last_reset_reason":"panic","fault_resets":{"window_s":86400,"panic":1,"task_wdt":0,"brownout":0},"health":{"uptime_s":20,"rssi":-61,"free_heap":142000,"min_free_heap":98000,"boot_ok":true,...},"metrics":[{"name":"sim_counter","value":13.0,...}],"last_key":"deploy"}`,
  six seconds later `uptime_s` 30 and `sim_counter` 16; `/telemetry?name=sim_counter&limit=5`
  returned 18, 17, 16, 15, 14, 2.0 s apart. `docker compose restart mosquitto`: re-announce
  of `panic` in the same boot, then `fault_resets.panic` 1 and one `device_faults` row.
  `just mqtt-pub` as the device with the proposal's example plus a URL:
  `[{"level":"W","text":"sensor: no reply <url>","dropped":0},{"level":"I","text":"morse: HELLO (firmware 1.0.1)","dropped":3}]`,
  `logged_at` 5.12 s apart; ingestor `device 1e60c99c020f log lines=2 dropped=3 unusable=0`,
  zero ingestor lines containing `morse: HELLO` or `evil.example`. Flood of 90 x 60 lines:
  exactly 5 000 rows, newest `flood 89-59`. An 8-day log and a 31-day sample were deleted by
  the start-up sweep (`logs=1 telemetry_expired=1`). Downgrade to 0008 with 5 000 logs and
  263 samples present removed the tables and columns and kept every device; upgrade to 0009
  and ingest resumed. Proven by tests only: the 500 000 telemetry cap, the fault-row trim,
  the fault and metric sweeps, every malformed-field case.
- **Not built (named follow-ups).** A time series of heartbeat health (`min_free_heap` trend:
  ~4 300 rows/device/day); new SSE event types (every frame already triggers the re-read); the
  frontend (R4-fe-1 mirrors the new `DeviceSummary` fields into `frontend/src/api.ts`, R4-fe-2
  reads `/logs`); the firmware `up/log` sender (R4-fw-3, still gated on the owner's Patches
  A+B).
- **Spec status.** Nothing under `spec/` changed. The up/log payload (Patch A) and its 5 000
  bound (Patch B) and the up/telemetry payload (R4-fw-2 Patch A) are still owner-pending; the
  server is built ahead of them, as R4-fw-2 was. `tests/test_device_health.py` reads the spec's
  up/log example once pasted (the proposal's copy until then) and reads both per-device
  bounds from `spec/prd.md` once applied, from the proposals until then.
- **Telemetry cap proposal (R4-be-1, proposed; owner, `spec:` commit).** `spec/prd.md` ->
  *Retention*. Replace the row `| Telemetry samples | **30 days** |` with:

  ```
  | Telemetry samples | **30 days**, and at most the newest **500 000** samples per device |
  ```

  Why: the firmware's own limits allow ~3 samples/s sustained (about 15 per 5 s payload),
  about 7.8 M rows per device in 30 days, on a shared prod VM; the up/log proposal bounded
  logs for the same reason. Order-independent with up/log Patch B (a different row). No test
  turns red when it is applied (the test reads the prd row once it carries a bound).

### R4-fe-1 as built

- **What the table shows.** One new column, **Health**, after Last seen (every other cell
  keeps its index). Online/offline and last seen were already rendered and are not repeated.
  Top to bottom, each line only when the server has something to say: the reset line; boot
  state (`boot ok`, or the bold underlined `boot not confirmed` for `boot_ok: false`, meaning
  the running image has not confirmed and rolls back if it never does); the server's fault
  count (`panic ×2, brownout ×1 in 24 h`, zeros omitted); the vitals (`heap low 95.7 KiB ·
  up 2 h 3 min · -61 dBm`, from the newest live heartbeat); and the latest metrics (first 4
  inline, the rest behind `+N more`, in the server's name order). A board that reported
  nothing (an old API, an agent < 0.5.0) shows a muted `—`. An offline board's vitals read
  `last reported ...` and are muted, with the heartbeat's receipt time in the title.
- **Badge rule.** `panic`, `int_wdt`, `task_wdt`, `wdt`, `brownout`, `cpu_lockup`,
  `pwr_glitch` and `efuse` get a boxed bold `! last reset: <reason>` badge
  (`data-testid=reset-badge`, `data-reason`); the plain-language label is in its title. Every
  other name (`poweron`, `ext`, `sw`, `deepsleep`, `usb`, `jtag`, `sdio`, `unknown`, and any
  name this dashboard has not heard of) is muted text, never a badge: an unknown future name
  is shown as itself and no fault is claimed that we cannot name. The set is broader than the
  server's three counters because the badge describes the current boot's cause and the
  counters are the server's history. The badge is not aged out client-side: a board that
  panicked a week ago and has run since still says `panic`, which is true, and the `up 6 d`
  beside it gives the age.
- **Theme.** Monochrome: the warning is a word, `.warn` weight and a `.badge` border, with an
  aria-hidden `!` glyph; no tint.
- **Live.** Nothing new: every heartbeat, telemetry batch and announce frame already triggers
  `useFleet`'s coalesced `GET /v1/devices`, and the frame body is never read (a test sends a
  frame carrying `reset_reason: panic` while the API says `poweron` and no badge appears).
- **API mirror.** `api.ts` gains `HealthSnapshot`, `FaultResets`, `MetricValue` and four
  optional `DeviceSummary` fields before `deploy`, so an older API and every older fixture
  still render. Logic is in `deviceHealth.ts` (pure); `format.ts` gains `formatBytes`.
- **T2 (dev stack, `FF_HTTP_PORT=8088`).** `just health-e2e` (a Chromium driving the real
  dashboard against a `--reset-reason panic --telemetry-every 2 --heartbeat-interval 5`
  simulator and a poweron control):

  ```
  PASS panic-badge: as expected
  PASS health-fields: as expected
  PASS live-metric: as expected [sim_counter 3 -> 6]
  PASS traps: as expected

  4/4 pass; wrote to /tmp/ff-r4-fe-1
  ```

  The badge computed to weight 700 with a solid border; the control row showed `last reset:
  poweron` and no badge. `E2E_BREAK=<id>` made each of the four scenarios FAIL (exit 1).
  API cross-check for the fault board: `last_reset_reason` panic, `fault_resets.panic` 1,
  `health.min_free_heap` 98000, `boot_ok` true, last key `deploy`.
- **Not built (named follow-ups).** The status strip does not show the reset badge (a
  `Segment` with tone `warn` would). No low-heap or weak-RSSI threshold: neither the spec nor
  the server defines a number. No metric history or sparkline (`GET
  /v1/devices/{id}/telemetry` exists). The log tail is R4-fe-2. The reset reason cannot tell
  an operator re-plug from the dashboard console pulsing EN.

### R4-be-2 as built

`DeviceEvent` gains four optional fields after `reset_reason`: `cmd_id`, `state`, `is_terminal`, `detail`. They are set on `device.deploy` only (the frame emitted when `record_observed_status` wrote a row) and are `null` on every other type, including a deduped retained replay and a status with no `cmd_id` (both stay `device.seen`). `v` stays 1. `detail` is present only on a terminal row and is the stored, sanitised copy (`deploys.snapshot_detail`), never the wire value, so a signed URL cannot ride the stream. The dashboard still only re-reads (`fleet.ts` rule 1); no frontend source change.

Waiter recipe: open `GET /v1/events` BEFORE the deploy POST (there is no replay), then wait for `type == "device.deploy" && cmd_id == X && is_terminal`. After a reconnect gap fall back to `GET /v1/devices` (`deploy.cmd_id`, `deploy.is_terminal`).

### R4-fe-2 as built

A `Device log` section sits on the Dashboard right after the fleet table (`DeviceLog.tsx`; the read and the pure helpers are in `deviceLog.ts`, the `profiles.ts` shape). It follows the board the status strip shows: the picked row, or the only board. Two or more boards with none picked shows `Pick a board in the Fleet table to read the log it sent.`; an arrival that is not a fleet row yet shows a muted line and does no fetch (the endpoint would 404). An empty fleet renders nothing. The `pick-board` button title now says it also shows the log.

**Live trigger, no new stream.** An `up/log` batch moves the board's `last_seen` (the ingestor touches it before decoding) and emits `device.seen`; `useFleet` re-reads `GET /v1/devices` 250 ms later. `useDeviceLog` re-reads `GET /v1/devices/{id}/logs?limit=200` whenever the selected board's `last_seen` string changes. No second EventSource (each costs an `sse_max_clients` slot), no poll, no change to `fleet.ts`, and no SSE body is ever read. A frame that leaves `last_seen` unchanged re-reads the fleet but not the log (tested).

**Rows.** The server's order is the order (`logged_at DESC, id DESC`); nothing sorts. A line's `dropped` is the loss immediately before it, so its gap row is drawn below it. A `text: null` row is a gap marker and renders as a gap only. Adjacent gaps are not merged. The level text is the raw level; `E` is bold and underline-free inside `.log`, `W` bold, `D`/`V` muted, `I` and unknown plain, so colour is never the only axis, and the title carries the label. Text is React text inside `<bdi>`, already sanitised server-side (no control characters, `<url>` for URLs, 200 characters). State is stored with its device id, so a board switch reads as `Loading…` and board A's lines never show under board B's heading.

**Why T2 publishes as a simulated board.** Nothing on a device sends `up/log` (R4-fw-3 is gated on the owner applying Patches A+B), and a simulator `--log-every` is that task's half. `just log-e2e` enrolls two real simulator boards and publishes `up/log` batches as the talker with its own credential, read from `.sim/<id>.json` by a python child, through the real broker, ingestor, DB and API.

**T2 (`just log-e2e`, dev stack on 8088).** Exit 0:
```
PASS tail-order: as expected
PASS levels: as expected
PASS untrusted-text: as expected
PASS live-line: as expected [publish 256 ms; visible 1133 ms after the publish returned]
PASS gap-marker: as expected
PASS switch-board: as expected
PASS api-agrees: as expected
PASS traps: as expected

8/8 pass
```
`E2E_BREAK=tail-order` and `E2E_BREAK=live-line` each exit 1 with the matching FAIL. API cross-check (curl, cookie jar): newest first, the `dropped:7` marker (`text: null`), `live`, the `<img> <url>` line, `boom` with `dropped: 3`, then W and I; the ingestor logged `log lines=… dropped=…` per batch and `evil.example` appears in none of its output.

**Named follow-ups, not built.** Paging beyond 200 (the API allows 5 000 but has no `before` cursor); a level filter and text search; a log tail in the status strip or the table row; the firmware sender and the simulator's own `up/log` (R4-fw-3); the end-to-end "a log line emitted on a device reaches the dashboard live" (R4-test-1); relative ages per line.
