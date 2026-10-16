# Self-Test (write once, gate everywhere)

**Priority:** P1
**Target:** R5 (default self-test defined at R0, custom entrypoint at R5, consumed by the V2 sim gate)
**Depends on:** OTA Deploy & Auto-Rollback (ota-deploy.md) — R2

## Overview

The **same self-test** is the simulation gate (pre-flight), the on-device confirm, and
later the canary assertion — *write once, run in sim and on device, no drift*
(design/architecture.md principle 3).

- **Default self-test** = "boots and reconnects to the broker" — catches boot-loops,
  zero user effort. Effectively active from R2 (confirm-on-reconnect).
- **Custom self-test** (R5) = an optional entrypoint baked into firmware. The agent
  calls it after boot. The simulator (R9, V2) calls it as its gate. Catches "boots but app
  logic broken," not just boot-loops.

## Phase 1: R5 — Custom self-test confirm

| ID | Task | Priority | Effort |
|----|------|----------|--------|
| R5-SPEC-1 | Propose the R5 wire and API semantics (proposal only; *R5 wire and API proposal* below) | P0 | 1d |
| R5-FW-1 | Self-test entrypoint contract (`ff_selftest_fn` / `Fleetforge.onSelfTest(fn)`) | P0 | 1d |
| R5-FW-2 | Hold slot confirmation until the self-test passes; fail rolls back with a reason | P0 | 1.5d |
| R5-FW-3 | Honour `dn/cmd.confirm_timeout_s` | P1 | 0.5d |
| R5-FW-4 | Implement `apply`, `reboot`, `rollback` and `cancel` | P0 | 2d |
| R5-FW-5 | Safe-window predicate gates apply and the rollback reboot | P0 | 1.5d |
| R5-FW-6 | Rapid-crash safe mode | P2 | 1d |
| R5-FW-7 | Sleepy lifecycle helpers | P2 | 1d |
| R5-BE-1 | Deploys honour the self-test result | P0 | 1d |
| R5-BE-2 | REST endpoints for `apply`, `reboot`, `rollback` and `cancel` | P1 | 1d |
| R5-FE-1 | Dashboard shows the self-test result, the rollback `detail` and Apply/Reboot controls | P1 | 1d |
| R5-TEST-1 | Failing self-test rolls back; `apply` waits for the safe window | P0 | 1d |

**Done when:** you can catch "boots but app logic broken," not just boot-loops.

## R5 wire and API proposal (R5-spec-1, proposed)

Proposal only; nothing is built. `spec/` is protected, so the patches are paste-ready here
(the `reset_reason`, `up/log`, `up/telemetry` and R4-spec-1 precedent).

- **Source:** TODO R5; focus-group items 3-4 (synthetic personas, DECISIONS 2026-10-15);
  `docs/SWARM.md` §2 (the rollback reboot with no safe-window check) and 5.5; `docs/HOBBYIST.md`
  §2; `spec/open-questions.md` → *Confirm semantics for sleepy / airborne devices* and *An OTA
  image that does not contain the agent*; `spec/flows.md` → the open sleepy-rollback bullet.
- **What the agent does today** (`agent/components/fleetforge/src/ff_mqtt.c`): `on_command()`
  handles only `stage`; any other type logs "not implemented by this agent — ignored" and
  sends no status. Dedupe remembers one id in RAM (`last_command_id`), lost at a reboot.
  `confirm_timeout_s` is parsed and ignored (compile-time 300 s). `confirm_timeout_cb` runs on
  the esp_timer task and reboots with no safe-window check; `s_rollback_decided` already
  latches the decision. The rollback detail lives in `s_txn.detail[64]`, so 63 usable bytes.
  The agent announces `capabilities: ["ota"]` only.
- **Wire (Patch A).**
  - **D1 Verb shape.** Each verb carries its own `id`, deduplicated like every command.
    `apply` and `cancel` also carry `cmd_id`, the stage's `id`, and are refused if it names any
    other transaction.
  - **D2 Answers on a new topic `up/ack`**, QoS 1, never retained:
    `{"id","type","result","cmd_id","detail"}`. `result` is `accepted` (acting now),
    `deferred` (accepted, acts at the next safe moment; a waiting `reboot` has no transaction,
    so this is the only place its wait shows) or `refused` (nothing changed, `detail` says why).
    `cmd_id` is the transaction acted on, or `null`. `detail` is at most 63 bytes of printable
    text, never a URL. The server keeps the first answer for an `id` and never makes it a
    `deploy_events` row. An R5 agent answers `refused` / `not implemented` to `identify` and
    `set_cfg`; older agents stay silent, so the server allows for no answer.
  - **D3 `apply`** releases a transaction at `staged` under `on_command`, which then walks
    exactly like `auto` (`awaiting_safe_window` while unsafe, then `applying → rebooting`). An
    `apply` for a transaction already past `staged` is a no-op answered `accepted`; with nothing
    staged under that `cmd_id` it is `refused` (`nothing staged for this cmd_id`).
  - **D4 `cancel`** is allowed in `staging`, `downloading`, `verifying`, `staged` and a
    pre-apply `awaiting_safe_window`. The device stops the transfer, puts the boot pointer back
    on the running image (the existing `esp_ota_set_boot_partition(running)` path in `ff_ota.c`),
    clears the `ff_txn` record and reports the new terminal state **`cancelled`**. Refused from
    `applying` on (use `rollback`) and against a decided rollback.
  - **D5 `rollback`.** While `confirming`, or while a deferred rollback waits, it ends that
    transaction: `rolling_back`, then `rolled_back` from the returned-to image with `detail`
    `rolled back by command; returned to <slot>`. With nothing in flight it is its own
    transaction under the rollback command's `id`: the device saves an `ff_txn` record
    `(rollback id, running slot)` and marks the running slot invalid. Compat bonus: an agent
    >= 0.4.0 that the board returns to already reports `rolled_back` for that record
    (`classify_txn`: running != target, last invalid == target), only in its old "did not
    confirm" words. It returns only to an image that booted and was confirmed in that slot, or
    was USB-flashed there; otherwise `refused` (`no confirmed image to return to`). Refused
    while an update is between `staging` and `rebooting` (`cancel first`).
  - **D6 `reboot`** restarts on the running image and opens no transaction; the next
    announce's `reset_reason: sw` is the evidence. Refused while a transaction is between
    `staging` and `confirmed`: once staged the boot pointer already names the new image, and a
    reset while `confirming` rolls it back.
  - **D7 Every agent-initiated reboot waits for the safe window**: apply (auto and commanded),
    the confirm-timeout or self-test rollback, the `rollback` verb and the `reboot` verb. Not
    crashes, watchdogs or power loss, which are physics. No `force` flag (principle 5).
  - **D8 Redelivery.** A reboot-causing verb is acted on only after its delivery returns, so
    the PUBACK has left, and the device keeps the id of the last command it rebooted for across
    the reboot (NVS or RTC; R5-fw-4 chooses), so a redelivery after the boot is a duplicate.
  - **D9 Capabilities vocabulary** (prose only): `apply`, `cancel`, `rollback`, `reboot` (each
    verb implemented), `selftest` (a custom self-test is registered in the running image),
    `safe_window` (a predicate is registered). A server sends a verb, or `apply: "on_command"`,
    only to a device that announces it. Otherwise a pre-R5 board ignores the verb, and an
    `on_command` board sits at `staged` until it next restarts, which then applies the image
    (the boot pointer already moved).
- **Device semantics (Patch A).**
  - **D10 Confirm deadline.** `confirm_timeout_s` comes from the `stage`, is kept with the
    transaction, and counts from the moment the new image starts. It covers broker, announce ack
    and custom self-test. Absent means 300 s. The device clamps it to **30-3600 s**: below 30 s a
    TLS+SNTP connect is a false rollback; above an hour a broken image runs too long. It is not
    paused while unsafe: only the reboot waits.
  - **D11 Custom self-test and the detail grammar.** It runs after the announce ack on a boot
    that needs confirming. Pass: mark valid, `confirmed` with `detail` `selftest passed`. Fail:
    rollback (safe-window gated); the returned-to image reports `rolled_back` with
    `selftest failed: <reason>; returned to <slot>`. No result before the deadline:
    `selftest timed out; returned to <slot>`. A timeout before the announce ack keeps today's
    `returned to <slot>; <slot> did not confirm`. `<reason>` is printable ASCII with `;`
    removed; the device shortens it, never the rest, so the whole `detail` fits **63** bytes.
    The TODO says "<= 64 B": 64 is the agent's buffer (`s_txn.detail[64]`), and one byte is the
    terminator. Worst-case fixed part: `selftest failed: ` (17) + `; returned to ota_1` (19) =
    36, which leaves 27 bytes of reason. The failing image stores the reason in `ff_txn` at
    decision time, before any deferral, so a crash during the deferral still reports it.
    R5-be-1 parses `^selftest (passed|failed: [^;]*|timed out)(; returned to \S+)?$`.
  - **D12 The safe window gates the rollback.** When the deadline passes or the self-test fails
    while the window is unsafe, the decision is made and latched (the `s_rollback_decided`
    idiom) and only the reboot waits. The transaction reports `awaiting_safe_window` with
    `detail` `rollback waits for a safe window` (`apply waits for a safe window` on the apply
    path), the board keeps running the unconfirmed image, and a later ack or self-test pass does
    not confirm. The wait has no deadline (a drone in the air). Any reset during it returns the
    board to the previous image anyway, because the bootloader ABORTs an image still
    `PENDING_VERIFY`. A device with no predicate is always safe (today's behaviour). This
    answers R5-fw-5's "what a board does when unsafe lasts longer than the confirm window".
  - **D13 `safe_mode`**, an announce field, prose only: `true` while held in recovery after
    three `panic`/`int_wdt`/`task_wdt`/`wdt` resets within 60 s on a confirmed image. Only the
    agent runs: it connects, announces, heartbeats and handles `stage`/`apply`/`rollback`/
    `reboot`; the firmware's own code never runs. It leaves safe mode on a successful update, a
    `reboot` command or a power cycle. Absent otherwise; absent and `false` alike.
  - **D14 A sleepy device stays awake through its confirm.** No sleep while a transaction is
    between `staging` and `rebooting`, nor while the running image is unconfirmed. The first
    wake after an apply lasts until `confirmed`/`rolled_back`, at most `confirm_timeout_s`.
- **API (this doc only, not spec).** The public surface is additive-only from release, so
  names and semantics are settled now (the R4-fw-2 precedent); R5-fw-1/5/7 settle the exact
  signatures.
  - **Self-test.** C: `typedef ff_selftest_result_t (*ff_selftest_fn)(void *ctx, char *reason, size_t reason_len);`
    returning `FF_SELFTEST_PASS`, `FF_SELFTEST_FAIL` or `FF_SELFTEST_PENDING`;
    `ff_set_selftest(fn, ctx)` registers it, and `ff_selftest_report(bool passed, const char *reason)`
    completes a pending one. Async matters: "armed, hovered, landed" takes minutes (SWARM.md
    §2). Arduino: `Fleetforge.onSelfTest(fn)` and `Fleetforge.selfTestDone(passed, reason)`.
    Register before `fleetforge_start()`/`begin()`. Called at most once per boot that needs
    confirming, in its own task: never the mqtt event handler (the PUBLISHED branch) and never
    the esp_timer task. A maker-API header under the R4-fw-2 rule (`MAKER_HEADERS` in
    `tests/test_ota_component.py`).
  - **Safe window.** `ff_set_safe_window_cb(bool (*fn)(void *ctx), void *ctx)` /
    `Fleetforge.onSafeWindow(bool (*fn)())`. Cheap, non-blocking, never called from an ISR.
    Polled every <= 1 s from an agent task while a reboot waits, and evaluated immediately
    before every agent reboot. Gotcha for R5-fw-5: `confirm_timeout_cb` is an esp_timer
    callback, so the poll moves to a task. Unregistered means safe.
  - **Sleepy helpers.** `Fleetforge.isConnected()`: a broker session is up and its announce
    has been acknowledged. `Fleetforge.isReadyForSleep()`: false while D14 forbids sleep, while
    a self-test or a deferred reboot is pending, or while the QoS 1 outbox is not empty. A
    code-set power class (`Fleetforge.setPowerClass(sleepy, wake_s)` before `begin()`)
    overrides `ff_cfg` `power`/`wake_s` and is announced, because the firmware knows how it
    behaves and the flasher only guesses. R5-fw-7 may contest this; flagged.
- **Facts it rests on** (IDF 5.5 sources under
  `~/.platformio-fleetforge-qemu/packages/framework-espidf/components/`):
  - The bootloader marks any otadata entry still `PENDING_VERIFY` as `ABORTED` at every boot
    (`bootloader_support/src/bootloader_utility.c`), and a deep-sleep wake is a boot: a sleepy
    board that sleeps on an unconfirmed image rolls back at wake (D14).
  - `esp_ota_mark_app_invalid_rollback()` does not check the running image's state, so an
    operator rollback of a confirmed image is mechanically possible, and IDF already refuses an
    INVALID/ABORTED other slot (`esp_ota_check_rollback_is_possible()`).
  - IDF does not refuse an other slot whose entry is NEW and never booted, so the spec states
    the stricter rule itself, and R5-fw-4 checks `esp_ota_get_state_partition(other)`.
  - esp-mqtt calls the event handler (`deliver_publish()`) before it sends the PUBACK, so a
    reboot from inside command delivery leaves the command unacknowledged and the persistent
    session redelivers it after every boot (D8).
  - **Acks are not `up/status`.** `ingestor/handlers.py` records every `up/status` with a
    `cmd_id` into `deploy_events`, even without a `requested` row, and `failed` is terminal: a
    refused `apply` answered on `up/status` would be a terminal `failed` deploy in both KPIs.
    An unknown `up/` channel moves `last_seen` and writes no row, and the ACL
    `pattern write ff/v1/d/%u/up/#` already covers `up/ack`, so a pre-R5 server and broker
    tolerate it. A pre-R5 server records `cancelled` as non-terminal, harmless because only an
    R5 server sends `cancel`.
- **Test shape (why each hunk is shaped as it is).** `test_ff_cfg.py::_spec_states` reads the
  second fence after the `up/status` heading, so A3 adds one line inside it and every new fence
  in that section (the `up/ack` example) comes after it; the firmware states stay a subset.
  No JSON example is touched: `TestAnnounceMatchesTheSpec` parses the announce example's keys
  (so `safe_mode` and the capabilities vocabulary are prose only), the library-marker and
  reset-reason tests pin its tail, and `test_stage_payload_is_exactly_the_spec_keys` pins the
  stage. The verb block in A2 is a plain fence (several objects are not one JSON document).
  `## Library marker` … `## Evolution rules` (`test_library_marker.py`) and the up/telemetry
  heading's first fence (`test_telemetry.py`) are not touched.
- **Order-independent with the pending owner patches** (`up/log` A+B, R4-spec-1 A+C,
  `up/telemetry` A, the R4-be-1 prd telemetry row; all in `health-telemetry.md`). A1 goes after
  the `up/status` Topics row (they replace the `up/log` and `up/telemetry` rows); A2 directly
  before the `### up/status` heading, never after the `artifact.sig` bullet (R4 A4's anchor);
  A6 after the "superseded layout." paragraph, never after the `reset_reason` paragraph (R4
  A1's anchor); A4 and A5 between "…waits for the `apply` command." and `## Enrolment`, which
  no other patch touches. C and D touch paragraphs no other patch touches.
- **Open for R5-fw-2, not resolved here:** an image the bootloader never armed (`TXN_BOOTED_NEW`,
  the R2b-fw-2 detail) with a self-test that fails. It has no `PENDING_VERIFY`, so R5-fw-2 must
  decide between an app-side `esp_ota_mark_app_invalid_rollback_and_reboot()` and `failed`, and
  which of the two details wins.
- **Named gaps:**
  - a board that returns to an agent that predates R5 reports `rolled_back` without the reason;
  - a reset during `awaiting_safe_window` before an apply boots the staged image, because the
    boot pointer moves at `staged` (`ff_ota.c`), even under `on_command` (true today; stated,
    not changed);
  - a predicate stuck at "unsafe" holds the board on an unconfirmed image until a reset; it
    stays visible as `awaiting_safe_window`, and a power cycle recovers it;
  - after `cancel` the old image re-confirms once: `esp_ota_set_boot_partition()` writes the
    entry NEW, so the next reset boots it in `PENDING_VERIFY` and `boot_ok` reads `false` until
    it confirms (harmless; the verify-failure put-back already does it).
- **Not proposed / rejected:** reusing the stage's id for `apply`/`cancel` (dropped as a
  duplicate when nothing came between); an `apply` without `cmd_id` meaning "whatever is
  staged" (a late apply from the persistent session could release an image nobody saw); acks on
  `up/status` (corrupts both KPIs); `failed` plus a detail for `cancel` (an operator cancel
  would count as a delivery failure); "reboot while staged applies the image" (two verbs for one
  action); a `force` flag or a server timeout on `awaiting_safe_window`; pausing the confirm
  deadline while unsafe; per-`power_class` confirm timeouts (`docs/HOBBYIST.md` "must differ by
  `power_class`" is made unnecessary by D14); a `dn/cfg` confirm timeout (nothing implements
  one); a separate `selftest` key on `up/status` (a second encoding of the same fact on a
  retained near-frozen topic); a `safe_mode` `up/status` state (a status without a `cmd_id` is
  dropped by design, and this is not a transaction); a recovery-partition safe mode (a new
  layout id, HOBBYIST (b); not R5); the capabilities vocabulary or `safe_mode` in the announce
  example (reddens the parity test; no agent sends them).
- **Server work this creates** (R5-be-1/2, R5-fe-1): `cancelled` into
  `TERMINAL_DEPLOY_STATES` and out of both KPIs; `up/ack` ingest (a new `UpChannel`, never a
  deploy row); the capability gate on verbs and on `apply: "on_command"`; REST rollback intent
  rows kept out of the deploy KPIs; parsing the detail grammar; the simulator learning the verbs.
- **Dry-run result:** throwaway worktrees, 2026-10-15, patch text taken from this section by script (`/tmp/r5spec1/dryrun.py`, never committed). Patches A+C+D alone: 9 anchors, each found exactly once; every hunk is a pure insertion (a line diff shows only inserts; D extends one line); the announce, hb, `dn/cmd` stage and `up/status` JSON examples are byte-identical; the machine block gains only `     ↘ cancelled`, and `_spec_states()` gives the old set plus `cancelled`; the `up/ack` Topics row directly follows `up/status`; the A4 paragraphs and `### up/ack` sit after the machine block and before `## Enrolment`, and the `up/ack` example parses with keys `id, type, result, cmd_id, detail`; the verb block lies between `### dn/cmd` and `### up/status` in a plain fence; A6 sits between the "superseded layout." paragraph and the `flash_chip_size` paragraph; *Library marker*, *Evolution rules* and *Open items for R0* are byte-identical; it printed `R5-SEMANTICS PATCH OK`. Order check: all four pending patch sets then R5, and R5 then all four: 22 anchors, each once, in both orders, and `spec/device-protocol.md`, `spec/open-questions.md`, `spec/prd.md` and `spec/flows.md` are byte-identical (`cmp`); message order `up/hb`, `up/log`, `up/telemetry`, `dn/cmd` (the `artifact.type` bullet, then the R5 verbs block last), `up/status`, `up/ack`. The 24 spec-reading test files (every `tests/*.py` that reads `spec/`): 938 passed unpatched, 938 with A+C+D, 938 on each fully patched tree, 0 failed.
- **Patch A** (owner, `spec:` commit), `spec/device-protocol.md`, six hunks, each in its own
  block.
- **Patch C** (owner, same commit), `spec/open-questions.md`, two sentences.
- **Patch D** (owner, same commit), `spec/flows.md`, one sentence.
- **No Patch B:** no JSON example changes, so nothing has to ride with a firmware commit.

**A1, Topics table.** Insert directly after the row `| `up/status` | 1 | **yes** | current update-transaction state |`:

```
| `up/ack` | 1 | no | the answer to a command other than `stage` (R5) |
```

**A2, `### dn/cmd — commands`.** Insert directly before the line `### `up/status` — the update transaction`, followed by one blank line:

````markdown
**`apply`, `cancel`, `rollback` and `reboot` (R5).** Each carries its own `id`, like every
command, and the device deduplicates on it. `apply` and `cancel` also carry `cmd_id`, the
`id` of the `stage` whose transaction they act on. The device refuses one that names any
other transaction, so a late `apply` never releases an image nobody chose.

```
{"id": "018f3b...", "type": "apply", "cmd_id": "018f3a..."}
{"id": "018f3c...", "type": "cancel", "cmd_id": "018f3a..."}
{"id": "018f3d...", "type": "rollback"}
{"id": "018f3e...", "type": "reboot"}
```

- `apply` releases a transaction that stopped at `staged` under `apply: "on_command"`. It
  then continues exactly as `apply: "auto"` does. An `apply` for a transaction already past
  `staged` changes nothing. One with nothing staged under that `cmd_id` is refused.
- `cancel` ends a transaction before its reboot: in `staging`, `downloading`, `verifying`,
  `staged`, or `awaiting_safe_window` before `applying`. The device stops the transfer and
  points its boot slot back at the image it is running, and the transaction ends
  `cancelled`. From `applying` on, and against a rollback the device has decided, it is
  refused.
- `rollback` returns the board to its other slot. While a transaction is `confirming` or
  waits to roll back, it ends that transaction. Otherwise the rollback is a transaction of
  its own under the command's `id`: `rolling_back`, then `rolled_back` from the image the
  board returned to. The device returns only to an image that booted and was confirmed in
  that slot, or was flashed there over USB. It never returns to one that failed
  verification, was rolled back or never booted. Without such an image, and while an update
  is between `staging` and `rebooting`, it refuses.
- `reboot` restarts the device on the image it runs. It opens no transaction. The next
  announce's `reset_reason`, `sw`, shows it happened. It is refused while a transaction is
  between `staging` and `confirmed`: once an image is staged, a reboot boots it, and a reset
  while `confirming` rolls the image back.
- **Every reboot waits for the device's safe window**: an apply, a rollback and a reboot,
  commanded or the device's own (*`up/status`*). The server requests, and it never forces.
- **A command that reboots the device is never repeated by its own redelivery.** The device
  acts on it only after it has acknowledged the command to the broker, never while that
  command is still being delivered. It also remembers, across the reboot, the `id` of the
  last command it rebooted for. Otherwise a persistent session would redeliver an
  unacknowledged `reboot` after every boot.
- The device answers each of these on `up/ack`. Their effect on a transaction appears on
  `up/status` under the transaction's `cmd_id`.
- A device lists the ones it implements in `capabilities` (*`up/announce`*). A server sends
  these commands, and `apply: "on_command"`, only to a device that lists them. An agent that
  predates them ignores the command and sends no answer, and an `on_command` stage then
  waits at `staged` until the board next restarts.
````

**A3, `up/status` state machine.** Inside the machine block, insert one line directly after `     ↘ failed`, before the closing fence:

```
     ↘ cancelled
```

**A4, `### up/status — the update transaction`.** Insert after the paragraph ending "…waits for the `apply` command." and its blank line, before A5:

```markdown
**The confirm deadline.** The image that boots into a transaction has `confirm_timeout_s`
seconds from the moment it starts to confirm. It must reach the broker, have its retained
announce acknowledged, and pass its custom self-test if it has one. The device keeps the
value from the `stage` with the transaction, uses 300 s when it is absent ([prd.md](prd.md)
→ *Timing*), and holds it within 30-3600 s. Agents that predate R5 always use 300 s.

**Custom self-test.** An image may register one self-test. The device runs it after the
announce is acknowledged and confirms only if it passes before the deadline. `confirmed`
then carries `detail` `selftest passed`. If it fails, the board rolls back (after the safe
window, below), and the image it returns to reports `rolled_back` with `detail`
`selftest failed: <reason>; returned to <slot>`. If no result came before the deadline,
that `detail` is `selftest timed out; returned to <slot>`. `<slot>` is the partition label,
for example `ota_0`. The whole `detail` is at most 63 bytes: the device shortens `<reason>`,
never the rest, and sends it as printable ASCII without `;`. An image without a custom
self-test confirms on the acknowledged announce, as before. A board that returns to an agent
that predates R5 reports `rolled_back` in that agent's words, without the reason.

**The safe window gates the rollback too.** If the deadline passes, or the self-test fails,
while the device judges the moment unsafe, the device decides to roll back and keeps that
decision, and only the reboot waits. The transaction reports `awaiting_safe_window` with
`detail` `rollback waits for a safe window`, and the board keeps running the unconfirmed
image until the moment is safe. An announce acknowledged or a self-test passed after that
decision does not confirm the image. A reset in the meantime still returns the board to the
previous image, because the bootloader never boots an unconfirmed image twice. Like the wait
before an apply, this wait has no deadline. A device without a safe-window rule is always
safe.

**`cancelled`** ends a transaction that the `cancel` command stopped before its reboot. The
board keeps the image it ran, and the staged image will not boot.

**A sleepy device stays awake through its confirm.** A deep-sleep wake is a reset, and the
bootloader rolls back an image that is still unconfirmed at a reset. A sleepy device
therefore does not sleep while a transaction is between `staging` and `rebooting`, nor while
the image it runs is unconfirmed. The first wake after an apply lasts until `confirmed` or
`rolled_back`, at most `confirm_timeout_s`. That is why `confirm_timeout_s` does not depend
on `power_class`.
```

**A5, new section.** Insert directly before `## Enrolment happens over HTTPS, not MQTT`, followed by one blank line:

````markdown
### `up/ack` — command answers

```json
{"id":"018f3b...","type":"apply","result":"accepted","cmd_id":"018f3a...","detail":null}
```

The device sends one answer per `apply`, `cancel`, `rollback` or `reboot` it receives. It is
QoS 1 and never retained, because an answer is an event, not state. `id` and `type` echo the
command. `result` is one of three values:

- `accepted`: the device is acting on the command now.
- `deferred`: the device accepted it and will act at its next safe moment. A `reboot` that
  waits shows the wait only here.
- `refused`: nothing changed, and `detail` says why.

`cmd_id` names the transaction the command acts on, or is `null` when there is none.
`detail` is at most 63 bytes of printable text and never a URL. A device that sends
`up/ack` answers `refused` with `detail` `not implemented` to any other command type it does
not carry out, except `stage`, which answers on `up/status`. An answer is never a
transaction state, so the server does not record it as one. It keeps the first answer for an
`id`, and it allows for an agent that never answers.
````

**A6, `### up/announce — identity`.** Insert these two paragraphs directly after the paragraph ending "…detect and quarantine boards flashed with a superseded layout." and its blank line, before the paragraph starting "`flash_chip_size`, `partition_table_sha256` and `rollback_capable`":

```markdown
`capabilities` lists what the device can be asked to do. `ota` means it carries out
`stage`. From R5, `apply`, `cancel`, `rollback` and `reboot` name the commands it
implements, `selftest` means the running image has registered a custom self-test, and
`safe_window` means it has registered a safe-window rule. The server stores a capability it
does not know as-is. A device lists only what is true of the image it runs.

`safe_mode` is `true` while the device holds itself in recovery after repeated crashes:
three `panic`, `int_wdt`, `task_wdt` or `wdt` resets within 60 s on a confirmed image. In
safe mode only the agent runs. It connects, announces and sends heartbeats, carries out
`stage`, `apply`, `rollback` and `reboot`, and never runs the firmware's own code. The device
leaves safe mode after a successful update, on a `reboot` command, or at a power cycle. The
field is absent otherwise, and the server treats absent and `false` alike. Both fields are
additive under *Evolution rules*, rule 2, and `proto` stays `1`.
```

(`capabilities` is already in the announce example; only `safe_mode` is new. Neither goes into
the example: `TestAnnounceMatchesTheSpec` would require `ff_identity.c` to emit it.)

**C1, *Confirm semantics for sleepy / airborne devices*** (append to the paragraph ending "…Needs a statement in `device-protocol.md`, not new fields."):

```markdown
Answered (R5-spec-1): `device-protocol.md` → `up/status` says a sleepy device stays awake
through its confirm, and that the safe window gates the rollback reboot as well as the apply.
```

**C2, *An OTA image that does not contain the agent*** (append to the paragraph ending "…that is the R5 custom self-test."):

```markdown
Its semantics: `device-protocol.md` → `up/status` → *Custom self-test* (R5-spec-1).
```

**D, `spec/flows.md`** (append to the end of the bullet "**Open, recorded not decided:** how a sleepy battery node avoids a false rollback from the confirm timer (beyond surfacing its sleepy wake window at pre-check).", on the same line after one space; that file keeps one bullet per line):

```markdown
Answered (R5-spec-1): it stays awake from `staging` until `confirmed` or `rolled_back` ([device-protocol.md](device-protocol.md) → `up/status`).
```

## Reuse note

The self-test entrypoint defined here moves by the simulation gate
(simulation.md, R9 — V2) with **no code change** — one test, three enforcement points.
