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
