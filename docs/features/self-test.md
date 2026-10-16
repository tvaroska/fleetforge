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

### R5-fw-1 as built

- **What was built.** The self-test entrypoint, unreleased, ahead of R5-fw-2: the agent records
  and freezes a registration but does not run it yet. Firmware: `include/ff_selftest.h` (new,
  public, maker API), `src/ff_selftest.c` and `src/ff_selftest_internal.h` (new, private), one
  `(void)ff_selftest_freeze();` in `ff_mqtt_run()` after the client is created and before
  `ff_txn_init()`/`classify_txn()` and the first `esp_mqtt_client_start()`; nothing else in
  `ff_mqtt.c` changed (not the confirm timer, `rollback_now_cb`, `confirm_this_image`, the
  PUBLISHED branch, `on_connected` or `classify_txn`). Arduino: `Fleetforge.onSelfTest()` and
  `Fleetforge.selfTestDone()` in `Fleetforge.h`/`Fleetforge.cpp`, plus the `ff-selftest` log
  tag. Both worked examples register one; the library README has a *Self-test* section. No
  wire, server, simulator or spec change; no version bump.
- **Signatures.** C: `typedef enum { FF_SELFTEST_PASS = 0, FF_SELFTEST_FAIL = 1,
  FF_SELFTEST_PENDING = 2 } ff_selftest_result_t;`,
  `typedef ff_selftest_result_t (*ff_selftest_fn)(void *ctx, char *reason, size_t reason_len);`,
  `esp_err_t ff_set_selftest(ff_selftest_fn fn, void *ctx);`,
  `esp_err_t ff_selftest_report(bool passed, const char *reason);`. Arduino:
  `bool onSelfTest(void (*test)());`, `bool selfTestDone(bool passed, const char *reason = nullptr);`
  (the callback takes nothing and returns nothing; the sketch answers through `selfTestDone`,
  from inside the callback or later, so the trampoline always returns PENDING).
- **Semantics.** One self-test per image (`ESP_ERR_INVALID_STATE` on a second one, and once the
  session has started; `ESP_ERR_INVALID_ARG` for NULL; a refusal is the only log line). The
  runner (`ff_selftest_run(done)`, private, non-blocking) creates its own task, `ff_selftest`,
  6144 B, priority 1 (below `ff_hb`/`ff_tel` at 4 and esp-mqtt at 5), which calls the function
  once, then waits on a static binary semaphore for a pending answer, logs
  `custom self-test passed` or `custom self-test failed: <reason>`, calls `done`, and deletes
  itself. The first answer wins: a report made while the function runs beats its return; any
  return other than PASS/PENDING is FAIL. The reason (64 B buffer, zeroed, ff_txn's
  `detail[64]`) is sanitized to bytes `0x20..0x7e` without `;`, a report's scan bounded at 256
  B; it is `""` on a pass. `ff_selftest_report()` never logs, allocates or blocks (a spinlock
  around a few stores and one `xSemaphoreGive`). A static semaphore and not a task notification:
  a report may race the task's exit, and notifying a deleted task is a use-after-free.
- **Registration point and the freeze.** The TODO line says "register before
  `fleetforge_start()`/`begin()`", but `begin()`/`fleetforge_start()` FIRST is the CRITICAL
  rollback posture, pinned by `test_the_example_calls_begin_first` and
  `test_app_main_calls_fleetforge_start_first`. Read as "before the session": the examples
  register as the SECOND statement, and `ff_mqtt_run()` freezes the registration before its first
  connect (after link bring-up and SNTP, seconds later). A later registration is a
  deterministic refusal, logged. The freeze logs once, either
  `a custom self-test is registered for this image` or
  `no custom self-test registered: an OTA'd image confirms once its announce is acknowledged`
  (both true today and after fw-2). Precedent for fw-5 `onSafeWindow` and fw-7 `setPowerClass`.
- **Deliberately not here (fw-2).** The `selftest` capability (claiming it before the hold
  exists is a capability the image does not have), the hold itself, the `detail` grammar, NVS
  reason storage and the 27 B shortening. `ff_selftest_run()` has no caller.
- **The examples.** "Keep an update only if the firmware's loop still runs": the self-test
  answers pending, and the loop answers a pass after every whole blinked message
  (`ff_selftest_report(true, NULL)` / `Fleetforge.selfTestDone(true)`); before the agent asks
  and after it decided, the call is a harmless no-op. After fw-2, build B confirms within one
  message (~6.8 s) of its announce ack, and the `-rbtest` build R never acks, so it still times
  out: `just lib-quickstart` stays meaningful. `main.c` is exactly 70 lines (`ONE_SCREEN`
  unchanged; the header comment and the LED comment compressed), `Basic.ino` 67.
- **Tests.** `tests/test_selftest.py` (25): the header (two functions, exact prototypes and
  typedef, explicit enum values, `extern "C"`, three includes, private hooks in no public
  header), the runner (the registered function called once, only in `selftest_task`; its own
  `xTaskCreate`, no pinning, prio < 4, stack >= 4096, buffer 64; no log/alloc/block in
  `ff_selftest_report`; static semaphore, no task notification; the sanitizer's literals;
  `== FF_SELFTEST_PASS` and no `== FF_SELFTEST_FAIL`; first answer wins; the refusals; the TAG),
  the session wiring (one freeze in `ff_mqtt_run` before `ff_txn_init` and
  `esp_mqtt_client_start`, no `ff_selftest_run` yet, nothing in `confirm_timeout_cb`,
  `rollback_now_cb` or `ff_mqtt_arm_confirm_timer`, nothing in `agent_main.c`), the Arduino
  members (one ff call each, PENDING trampoline, the tag) and both examples (register second,
  answer from the loop after the blink). `test_ota_component.py`: `MAKER_HEADERS` gains
  `ff_selftest.h`, `INTERNAL_HEADERS` gains `selftest`, and `_declared_functions` now removes
  function-pointer typedefs (else `ff_selftest_result_t` would count as a "function").
  Mutation checks, each red: calling `s_fn` inside `ff_selftest_run`
  (`test_the_registered_function_is_called_from_its_task_only`), an `ESP_LOGI` in
  `ff_selftest_report` (`test_report_never_logs_allocates_or_blocks`), dropping the `';'`
  filter (`test_the_reason_is_printable_ascii_without_semicolon`).
- **T1.** `just agent-build-all`: four `BUNDLE OK`, 0 warnings. `APP_SIZE_BUDGET_BYTES`: +400 B
  each (esp32 1,032,688, esp32s3 1,012,704, esp32c3 1,072,864, esp32c6 1,122,432): the freeze
  and its two log strings; the rest of `ff_selftest.c` is garbage-collected in the stock agent.
  `just lib-quickstart --build-only`: PASS (Arduino Basic esp32 1,179,616 B / esp32s3
  1,172,944 B, build B, the own-project path, basic_idf esp32 1,107,280 B / esp32s3
  1,112,480 B; 0 warnings). `just lib-arduino-check`: PASS (arduino-cli 1.5.1, esp32:esp32@3.3.12,
  esp32 1,169,024 B / esp32s3 1,159,856 B, 0 warnings, `T verifyRollbackLater` in both).
  `just test`: 1986 passed; ruff, format, mypy clean.
- **T2 (local dev stack on 8088 only).**
  - (a) The Arduino Basic example in QEMU (`just lib-bundle esp32-qemu`, `just agent-cfg ...
    --token <fresh>`, `just lib-qemu --fresh`): `I (13268) ff-selftest: a custom self-test is
    registered for this image` at line 44, `I (13340) ff-mqtt: connecting to
    mqtt://10.0.2.2:8883 as 000000000000` at line 45, `announce acknowledged by the broker` at
    53, then `morse: SOS (firmware 1.0.0)` 4 times in the next 20 s; judge:
    `SELFTEST REGISTRATION OK (freeze@44 < connect@45, ack@53, morse-after-ack=4, boots=1)`
    (exactly one freeze line, no run/pass/fail line, no refusal, no panic, one `rst:`).
    `GET /v1/devices`: `000000000000` with `capabilities: ["ota"]` (no `selftest` yet).
  - (b) The stock agent (`just agent-qemu esp32 --fresh`, fresh token): `I (8228) ff-selftest:
    no custom self-test registered: an OTA'd image confirms once its announce is acknowledged`
    at line 100, before `connecting to` (101) and the ack (108); judge
    `SELFTEST REGISTRATION OK`. `just agent-qemu-smoke esp32`: `HARNESS OK`.
  - (c) The OTA confirm/rollback path is unchanged with a registered self-test: `just
    lib-quickstart` (api recreated with 10.0.2.2 origins, restored afterwards) `== PASS (1754 s)`:
    build B `1.1.0-qs1792114485` `deploy confirmed (is_terminal True), 77 s after the deploy`
    (`this image was written by OTA and is now CONFIRMED`: the announce ack, no hold), build R
    `-rbtest` `deploy rolled_back`, `detail 'returned to ota_1; ota_0 did not confirm'`, no
    confirmed step, the board back on B. A second run with `--keep --skip-idf` (`== PASS (1345
    s)`, the kept tree deleted afterwards: it held a live credential) kept the transcripts:
    `ff-selftest: a custom self-test is registered for this image` exactly once in each of
    `qemu-A.log`, `qemu-B.log`, `qemu-R.log` and `qemu-R2.log` (the boot after the rollback),
    each before `connecting to`, and 0 run/pass/fail/refusal lines in all four.
- **Not provable here.** The runner (the task, pending/report, first answer wins, the done
  callback) is not invoked on a device until R5-fw-2 wires it; its QEMU proof is fw-2's
  acceptance ("a build whose self-test fails comes back on the old slot"). Here it is covered
  by the source pins and a clean `-Werror` build. `basic_idf` is proven by compile plus the
  shared component, not booted (as for R4-fw-1/R4-fw-2).
- **Notes for R5-fw-2.** (1) Call `ff_selftest_run(done)` from the PUBLISHED branch on the
  announce ack of a boot that needs confirming, instead of `confirm_this_image()`, when
  `ff_selftest_freeze()` returned true; it only creates a task, so it is legal there. (2) `done`
  runs in the `ff_selftest` task (priority 1). It may `enqueue_status` (as
  `rollback_report_task` does) and call `esp_ota_mark_app_valid_cancel_rollback()`, but it must
  check `s_rollback_decided` first: a late pass after the deadline must not confirm (D12). (3)
  Store `ff_selftest_freeze()`'s return value to announce `selftest` (D9) and to choose the
  confirm path. (4) The reason that arrives is already printable ASCII without `;`, up to 63 B;
  shorten it to fit the 63-byte `detail` (27 B worst case) and write it to `ff_txn` at decision
  time. (5) Delete the "Until R5-fw-2" lines in `ff_selftest.h`, `Fleetforge.h` and the README.
- **Rejected.** (1) Registering before `begin()`/`fleetforge_start()` in the examples: breaks the
  CRITICAL first-statement posture and its pins for a pointer store; the freeze gives the same
  guarantee. (2) An Arduino callback returning a three-value enum: a second result path in a
  beginner API. (3) Running the function in the esp-mqtt event handler, on esp_timer, or
  synchronously in `ff_selftest_run()`: stalls the session/PUBACK or every timer, the confirm
  timer included. (4) A task notification to wake the runner: races the task's exit. (5)
  Logging or blocking in `ff_selftest_report()`: the R4-fw-3 recursion, and it is called on
  every loop. (6) Announcing `selftest` now. (7) Wiring the runner "advisory" after the confirm
  for on-device evidence now: transitional semantics in the CRITICAL confirm path. (8) A
  QEMU-only probe flag to run the self-test on a serial flash: test-only code in the component.
  (9) Raising `ONE_SCREEN`.

### R5-fw-2 as built

- **What was built.** The hold, unreleased (no version bump; the release waits for the owner's
  Patch A). `ff_mqtt.c` (CRITICAL): on the announce ack of a boot that needs confirming
  (`pending_verify()` or `TXN_BOOTED_NEW`), an image whose `ff_selftest_freeze()` returned
  true does not confirm. The ack starts `ff_selftest_run(selftest_done)`, and only
  `selftest_done(passed=true)` confirms. One new `else if` sits between the late-ack guard
  `if (s_rollback_decided)` and the R2-be-1 confirm line, which is unchanged. With no self-test
  registered, every line of the ack path is as before, so the stock agent behaves as before.
  `ff_txn` gains an optional third key, `detail`, and `ff_txn_set_detail(cmd_id, detail)`
  (cmd_id-checked like `ff_txn_clear_if`; never erases). `ff_identity` announces `selftest`
  in `capabilities` when one is registered (D9). Stale "Until R5-fw-2" text was removed from
  `ff_selftest.h`, `Fleetforge.h`, `ff_selftest.c` and the library README.
- **The decision latch.** Two parties can decide, and they race: the self-test result
  (`ff_selftest` task, priority 1, maybe on the other core) and the confirm deadline
  (`confirm_timeout_cb`, esp_timer). Each claims under `s_decide_lock` (a portMUX):
  `s_confirm_claimed` for a pass, `s_rollback_decided` for a fail or the deadline. The first
  claim wins. Mark-valid, mark-invalid and NVS writes happen after the claim, outside the lock.
  A late answer logs `the self-test answered after the confirm deadline — the rollback already
  decided stands; NOT confirming`. A deadline that finds the confirm claimed returns. A
  deadline that finds a failed self-test's rollback already claimed is the **backstop**: it
  calls `esp_ota_mark_app_invalid_rollback_and_reboot()` at once, which covers a starved
  priority-1 task.
- **The deadline guard.** It was `if (s_ctx.session_confirmed || !pending_verify()) return;`.
  It is now `if (!pending_verify()) return;` plus
  `if (s_ctx.session_confirmed && !s_selftest_registered) return;`. `s_selftest_registered`
  is set from the freeze in `ff_mqtt_run()`, before the first connect, never at the ack.
  Setting it at the ack would open a window where the image runs unconfirmed with no deadline.
  The log keeps `no working session %d s after an OTA boot — …` byte for byte when there is no
  session, and says `no self-test result %d s after an OTA boot — …` when a self-test is pending.
- **Where the reason lives.** The image under test stores a HEAD in `ff_txn`, and the image the
  board returns to appends `; returned to <its running label>`. At the ack (`TXN_CONFIRMING`
  only) `selftest timed out` is pre-stored STRICTLY BEFORE `ff_selftest_run()`, so the esp_timer
  path never writes NVS. A deadline, watchdog or power cut with the self-test pending reports
  `selftest timed out`, which is intended: no result arrived. On a fail, `selftest_done` stores
  `selftest failed: <reason>` after its claim and before the grace timer or reboot (D11:
  decision time). Only the reason is shortened: budget `63 - 17 - 14 - strlen(label)` = 27 for
  `ota_N`, and an empty reason becomes `no reason given`. `classify_txn` composes
  `<head>; returned to <slot>` for `TXN_ROLLED_BACK` when `rec.detail` starts with
  `selftest `. Otherwise it keeps `returned to X; Y did not confirm`, which is also what a
  timeout before the ack gives. For `TXN_ALREADY_CONFIRMED` with a stored head it repeats
  `selftest passed` (a reset between mark-valid and the PUBACK). `ff_txn_load` reads the detail
  only after the record is judged complete, so a missing detail never makes a record torn.
- **A pass.** For `TXN_CONFIRMING`: `confirm_this_image()`, then detail `selftest passed`, then
  `TXN_ALREADY_CONFIRMED`, then `confirmed` is queued (a reconnect repeats it). For
  `TXN_NONE` + PENDING_VERIFY (an image OTA'd before 0.4.0): confirm, no report.
- **A fail.** Claim, store the head, log `the custom self-test failed — marking this image
  invalid and rolling back to the previous slot`. Then the 2 s grace timer (created on demand
  when NULL) plus a direct `rolling_back` when connected. With no timer, or no transaction,
  `esp_ota_mark_app_invalid_rollback_and_reboot()` is called at once.
- **DECIDED: `TXN_BOOTED_NEW` plus a self-test.** The self-test runs. On a fail, the app side
  calls `esp_ota_mark_app_invalid_rollback_and_reboot()`, not `failed`: IDF 5.5's
  `bootloader_common_ota_select_valid()` never boots an INVALID/ABORTED entry, armed or not,
  and `esp_ota_current_ota_is_workable(false)` checks `esp_ota_check_rollback_is_possible()`.
  The image it returns to reports `rolled_back` with the self-test detail, so **the self-test
  detail wins on a fail**. On a pass, `confirmed` keeps `the bootloader never armed rollback
  for this image`, so **DETAIL_BOOTED_NEW wins on a pass**: it is the one explicit sign that
  this board cannot roll back by time, and a pass is implied by `confirmed` from a board that
  announces `selftest`. This deviates from Patch A's sentence "`confirmed` then carries
  `detail` `selftest passed`".
  **Proposed addendum (paste-ready, for the owner, Patch A4's *Custom self-test* paragraph):**
  "An image the bootloader never put in pending-verify keeps its `confirmed` detail
  `the bootloader never armed rollback for this image` when its self-test passes, and rolls
  itself back when it fails."
- **The capability.** `ff_identity_note_selftest(registered)` (private, a HAZARD in
  `test_ota_component.py`) is called right after the freeze. `announce_object()` appends
  `"selftest"` after `"ota"`. The enroll body is built before the freeze and stays `["ota"]`;
  the first announce updates the row.
- **Accepted races.** On a pass, `s_txn.kind`/`terminal_msg_id` are written from the self-test
  task while the mqtt task may read them. These are aligned int stores. The worst case is a
  missed `ff_txn_clear_if` or a doubled `confirmed` on a reconnect, and both say the same
  outcome again (the server deduplicates). The result is not routed through
  `esp_mqtt_dispatch_custom_event()`, which is drained only when another mqtt event is
  dispatched (queue size 1), so it would stall while disconnected.
- **Named gaps.** (1) BOOTED_NEW has no confirm deadline (R2b-fw-2). A never-armed image whose
  self-test never answers stays unconfirmed and unreported until it does. R5-fw-3 may arm an
  app-side deadline. (R5-fw-3 did not arm one: see *R5-fw-3 as built*.) (2) A crash or reset mid-self-test reports `selftest timed out`. (3) A
  board returning to a pre-R5 agent loses the reason (R5-spec-1). (4) A pass whose mark-valid
  fails leaves PENDING_VERIFY with the confirm claimed: the bootloader rolls back at the next
  reset and reports `selftest timed out` (the pre-store). That is the same as today's ack-path
  mark-valid failure.
- **Tests.** `tests/test_selftest.py::TestTheHold` (13): one run, in the PUBLISHED slice,
  between the late-ack guard and the pinned confirm; the pre-store before the run; a failed
  start keeps the hold; the guard and both log lines; the timer claims under the lock, with no
  mqtt/NVS/self-test/txn calls in the timer, `rollback_now_cb` or `rollback_report_task`; the
  done callback claims before any confirm, a late answer returns first, and it repeats no
  pinned literal; a pass sets `selftest passed` before the kind and the enqueue; a fail stores
  the head before the grace timer and the reboot; `ff_txn_set_detail` is called only by the
  session; the grammar constants and the 63/27 budget; the head helper cuts only the reason;
  classify composes only under the prefix; the capability only behind its flag.
  `test_one_freeze_before_the_first_connect` now pins
  `s_selftest_registered = ff_selftest_freeze();` then `ff_identity_note_selftest(` before
  `ff_txn_init`/`classify_txn`/`esp_mqtt_client_start`. `tests/test_agent_txn.py` (+3): the
  detail is stored only for the recorded cmd_id (strcmp, commit, no erase); a missing detail
  is never torn; only the session annotates. `tests/test_worked_example.py::TestSelftestRun`
  (9): the 63-byte F detail, `;`/non-ASCII dropped, the empty reason, a short reason intact,
  the twin matches the firmware's constants, each replacement hits `Basic.ino` once, the
  versions are run-unique and marked. **Mutation checks**, each red and then reverted: the
  pre-store moved after `ff_selftest_run(` (`test_the_timed_out_head_is_stored_before_the_run`);
  `!s_selftest_registered` dropped from the guard (`test_the_deadline_still_applies_to_a_held_image`);
  the claim and lock dropped from `selftest_done` (`test_the_done_callback_claims_before_it_confirms`);
  the classify ROLLED_BACK branch ignoring `rec.detail` (`test_classify_composes_only_under_the_prefix`).
- **T1.** `just test`: 2010 passed; ruff, format and mypy clean. `just agent-build-all`: four
  `BUNDLE OK`, 0 warnings. `APP_SIZE_BUDGET_BYTES` was raised to the measured byte: esp32
  1,036,176 (+3,488), esp32s3 1,016,144 (+3,440), esp32c3 1,076,752 (+3,888), esp32c6
  1,126,320 (+3,888). The ack branch now links the runner into the stock agent.
  `just lib-quickstart --build-only`: `== PASS (583 s)` (Arduino A esp32 1,183,136 B /
  esp32s3 1,176,432 B, build B, own project, basic_idf esp32 1,110,736 B / esp32s3
  1,115,952 B; 0 library warnings).
- **T2 (local dev stack on 8088; api recreated with 10.0.2.2 origins, restored afterwards).**
  `just lib-quickstart --keep`: `== PASS (2308 s)`. The kept tree was deleted afterwards
  because it held a live credential.
  - 2a: `capabilities ['ota', 'selftest']` at 1.0.0.
  - 2b: B `1.1.0-qs1792123592`: `I (12311) announce acknowledged` → `I (12600) ff-selftest:
    running the custom self-test` → `I (20181) custom self-test passed` → `W (20463) … is now
    CONFIRMED`. The row: `deploy confirmed (is_terminal True), detail 'selftest passed'`.
  - 2c: R `-rbtest` is unchanged: `no working session 60 s after an OTA boot`,
    `rolled_back (returned to ota_1; ota_0 did not confirm)`, no run line in R's log.
  - 2d (the acceptance): F `1.3.0-qs1792123592-stfail` acks at 27773. `I (28589) ff-txn:
    … detail stored` (the pre-store) comes before `I (28609) running the custom self-test`, then
    `W (28616) custom self-test failed: i2c sensor 0x48 did not answer tried 3 times`,
    `I (29356) ff-txn: … detail stored` (the fail head), `E (29407) the custom self-test failed
    — marking this image invalid…`, the queued `state=rolling_back`, and then
    `rst:0xc (SW_CPU_RESET)`. No CONFIRMED. After the power cycle, B logs `W (26657)
    transaction 096a2fd2…: rolled_back (selftest failed: i2c sensor 0x48 did not ans; returned
    to ota_1)` and `morse: HELLO (firmware 1.1.0-qs1792123592)`, with no run line. The row:
    `rolled_back (is_terminal True), fw_version 1.1.0-qs1792123592`, detail exactly
    `selftest failed: i2c sensor 0x48 did not ans; returned to ota_1` (63 bytes), steps
    `… confirming, rolling_back, rolled_back`, never confirmed. 117 s after the deploy.
  - 2e: T `1.4.0-qs1792123592-sttimeout`: the pre-store at 16737, the run at 16745, `E (310100)
    no self-test result 300 s after an OTA boot — …`, then `SW_CPU_RESET`. The row:
    `rolled_back`, detail exactly `selftest timed out; returned to ota_1` (37 bytes), never
    confirmed.
  - Token 0 occurrences and mqtt_password 0 in 29 files; LWT seen.
  - The stock agent: `just agent-qemu-smoke esp32` gives `HARNESS OK`. `just agent-qemu esp32
    --fresh` (fresh token) logs `no custom self-test registered: …` (line 100) before
    `connecting to` (101) and the ack (108). `GET /v1/devices`: `000000000000`,
    `capabilities: ['ota']`.
- **Not provable here.** BOOTED_NEW (QEMU's bootloader always arms rollback), the late-answer
  race, and the backstop. These are covered by the source pins only. The Arduino bootloader
  does not log `Loaded app from partition`, so "back on B" rests on B's firmware string in
  the morse line and the row's `fw_version`, as in phase 2c.

### R5-fw-3 as built

- **What was built.** The agent honours `dn/cmd.confirm_timeout_s`, unreleased (no version
  bump; the release waits for the owner's Patch A). `on_stage()` (the command seam) resolves
  the value: a number is clamped to 30-3600 s, an absent or non-number field gives
  `CONFIRM_TIMEOUT_S` (300). The result travels in `ff_ota_cmd_t.confirm_timeout_s`;
  `ff_ota.c` passes it to `ff_txn_save()` unread. `ff_txn` gains an optional u32 key
  `confirm_s`. On the OTA boot, the first-statement arm still starts at `CONFIRM_TIMEOUT_S`
  with no NVS; once `classify_txn()` has read the record and found `TXN_CONFIRMING`, the new
  `rearm_confirm_deadline()` moves the running timer to the stage's deadline counted from the
  same image start. No spec, server, `Fleetforge.cpp`, `fleetforge_start.c`, `agent_main.c`,
  partition or sdkconfig change.
- **Decisions.**
  - (1) **Where the value lives.** `FF_TXN_KEY_CONFIRM "confirm_s"`, `ff_txn_t.confirm_timeout_s`,
    0 when not stored (a record from before R5-fw-3). It follows the `detail` rule: it is read
    only after the record has been judged complete, and a missing key is never a torn record.
  - (2) **Who clamps.** `ff_mqtt.c`, twice. `on_stage()` with the stager's limits, and
    `classify_txn()` again with the NEW image's limits (`0` means `CONFIRM_TIMEOUT_S`). The second
    clamp is what lets a rollback-test image cap a value a plain stager stored.
    `confirm_timeout_clamp(double)` compares as a double before any cast: NaN, negatives and 0
    give 30, anything above the maximum (inf too) gives the maximum, a fraction truncates.
    `on_stage` warns (never with `%f`) only when it clamped (`confirm_timeout_s is outside
    30-3600 s — using N s`) or the field is not a number. The R1 "parsed and IGNORED" comment
    and the `this firmware uses %d s` line are gone.
  - (3) **The boot arm is unchanged.** `ff_mqtt_arm_confirm_timer()` still arms
    `CONFIRM_TIMEOUT_S` with nothing but esp_timer and otadata. It now also keeps the handle
    (`s_confirm_timer`), the origin (`s_confirm_origin_us = esp_timer_get_time()`, taken
    immediately before `esp_timer_start_once`) and the deadline in force
    (`s_confirm_deadline_s`). Its log line `OTA boot: %d s from now …` still prints
    `CONFIRM_TIMEOUT_S`, which is true at that moment.
  - (4) **The re-arm.** Only in the `TXN_CONFIRMING` branch, after its log line. TXN_NONE with
    PENDING_VERIFY (an image OTA'd before 0.4.0, no record) keeps 300 s; BOOTED_NEW gets no
    timer. The body: no timer → log E and return (the boot arm's outcome stands); the same
    deadline → `confirm deadline stays N s` and return, the timer untouched (so the default
    300 s path and the rbtest 60 s path are timer-identical to R5-fw-2); otherwise
    `remaining = deadline − (now − origin)`, clamped at 0 (an overdue deadline fires at once,
    in the esp_timer task; the callback is never called directly); `esp_timer_stop()` — a
    failure means the timer has fired or is firing, so the decision stands, log W, return;
    store the new deadline (race-free: the timer is stopped); `ESP_ERROR_CHECK(
    esp_timer_start_once(...))`; log W `transaction <id>: confirm deadline N s from image start
    (M s left)`.
  - (5) **`FF_ROLLBACK_TEST` caps the deadline at 60 s** (`CONFIRM_TIMEOUT_MAX_S 60` inside the
    existing fault-injection block; 3600 in the `#else`; `CONFIRM_TIMEOUT_MIN_S 30` outside).
    The server always sends 300, so a rollback-test image still rolls back at 60 s and the
    quickstart's phase 2c and the runbooks are unchanged, while a shorter command gives its
    own value. A `_Static_assert` keeps the default inside the clamp. This extends the one
    documented fault-injection block; it is not a new test-only override.
  - (6) **The callback reports the deadline in force.** `confirm_timeout_cb`'s two lines keep
    their format strings byte for byte; only the argument changed, to `(int)s_confirm_deadline_s`
    (`static volatile uint32_t`, an atomic aligned store). The `FF_ROLLBACK_TEST` hook line in
    `on_connected` prints the same value (it runs after classify). Nothing from
    `TIMER_FORBIDDEN` entered the timer-context functions.
  - (7) **Save order.** erase → target → `confirm_s` → `cmd_id` → commit. `cmd_id` stays the
    completion marker, so a complete record always carries the deadline, and a torn save
    reads back as no record (a lost report, never a lost rollback; the image then keeps 300 s).
  - (8) **Not paused while unsafe (D10).** Written into `rearm_confirm_deadline`'s comment: the
    safe-window wait (R5-fw-5) must never stop, extend or re-arm this timer; only the reboot
    waits.
- **Rejected.** `esp_timer_restart()`: IDF 5.5 checks `timer_armed()` before it takes the list
  lock, so it can re-insert a timer that has just been dispatched and run the callback twice
  (the second run takes the backstop branch and reboots without the `rolling_back` report);
  `esp_timer_stop()` checks under the lock. A re-arm in `agent_main.c`, `fleetforge_start.c` or
  `Fleetforge.cpp` (CRITICAL files, and the R2-fw-4 parity tests pin the arm as a no-NVS first
  statement). Arming an app-side deadline for BOOTED_NEW (it needs its own decision). An
  uncapped rbtest image (the server's 300 would break phase 2c and the runbooks). Clamping in
  `ff_ota.c` (the seam is `on_stage`; `ff_ota.c` never interprets the wire).
- **Named gaps.** (a) An image that hangs BEFORE `ff_mqtt_run()` (net bring-up, enrollment, the
  `-hangtest` fault image) keeps the boot arm's 300 s, because only `classify_txn()` reads the
  record: a shorter command waits 300 s there, and a longer one rolls back at 300 s, earlier
  than asked, which is the safe direction. The agent-qemu runbook's *Hang before the session*
  row stays at 300 s and is still correct. (b) BOOTED_NEW still has no deadline. (c) The
  simulator keeps its own `--confirm-timeout`; only its stale comment changed (teaching it the
  command is left to R5-be-2/R5-test-1).
- **Proposed spec addendum (paste-ready, for the owner; `spec/` is not edited).** One sentence
  after Patch A4's *The confirm deadline* paragraph: "Until the device has read its transaction
  record, which happens once its network is up, it uses 300 s."
- **Tests.** `tests/test_agent_txn.py` (+11, the `R5-fw-3` section): the header key, field and
  3-arg prototype; the save order erase < target < confirm < cmd_id < commit; the confirm read
  after the torn check and outside it, reset to 0 on error; `ff_ota.c` passes
  `cmd->confirm_timeout_s` and holds no `cJSON`/`CONFIRM_TIMEOUT`; the MIN/MAX defines, the 60
  cap only inside `#if FF_ROLLBACK_TEST`, 3600 in its `#else`, `_Static_assert`, 300 still the
  default; the clamp compares before it casts; `on_stage` sets the default before the clamp and
  both before `ff_ota_start`, no `this firmware uses`, no `%f`; the arm keeps the handle,
  deadline and origin (before the start), still 300 s; the re-arm short-circuits, then stops,
  stores, starts, uses the origin, never `esp_timer_restart` anywhere, and calls no
  callback/NVS/txn/mqtt/enqueue; exactly one re-arm call, in the `TXN_CONFIRMING` branch after
  the kind and before its `return;`, through the clamp, absent from every timer-context
  function and `on_stage`; the callback's two lines and the hook line print
  `s_confirm_deadline_s`. **Mutation checks**, each red and then reverted: (i) the record used
  raw, without the load-time clamp (`test_only_a_confirming_transaction_rearms`); (ii) stop+start
  swapped for `esp_timer_restart` (`test_the_rearm_stops_then_starts_and_never_restarts`); (iii)
  the remaining time from `esp_timer_get_time()` alone, without the origin (same test); (iv) the
  confirm key written after `cmd_id` (`test_the_deadline_is_saved_before_the_completion_marker`).
- **T1.** `just test`: 2021 passed; ruff, format and mypy clean. `just agent-build-all`: four
  `BUNDLE OK`, 0 warnings. `APP_SIZE_BUDGET_BYTES` raised to the measured byte: esp32
  1,037,360 (+1,184), esp32s3 1,017,344 (+1,200), esp32c3 1,078,080 (+1,328), esp32c6
  1,127,648 (+1,328); mostly the new log strings. `just lib-quickstart --build-only`:
  `== PASS (832 s)` (Arduino A esp32 1,184,336 B / esp32s3 1,177,616 B, own project, build B,
  basic_idf esp32 1,111,984 B / esp32s3 1,117,184 B; 0 library warnings).
- **T2 (local dev stack on 8088; api recreated with 10.0.2.2 origins, restored afterwards).**
  Board A = `agent/dist/esp32` from `agent-build-all` (agent 0.5.0); R = the same source with
  `FF_ROLLBACK_TEST=1` in `/tmp/ff-fw3-R` (`agent 0.5.0-rbtest`, 1,037,488 B, 0 warnings).
  - (a) The acceptance, a 30 s command. R uploaded as `0.5.0-rbtest-fw3-1792133121` (201); the
    hand-built stage `fw3-30s-1792133121` (`apply: on_command`, `confirm_timeout_s: 30`) published
    as `ff-commander`. A: `I (108238) ff-txn: transaction fw3-30s-1792133121 recorded (target
    slot at 0x00200000, confirm 30 s)` → `I (108258) … ota_1 is staged and bootable`. After the
    power cycle, R: `W (2899) OTA boot: 60 s from now …` → `W (12609) transaction …: confirming
    on ota_1` → `W (12619) transaction …: confirm deadline 30 s from image start (20 s left)` →
    `E (12849) FF_ROLLBACK_TEST: … roll back in 30 s` → `E (33039) no working session 30 s after
    an OTA boot — …` → `rolling_back` queued → `SW_CPU_RESET`. No `no working session 60 s`, no
    CONFIRMED. The deadline fired 30.14 s after the boot arm. otadata: ota_1 **`INVALID`**
    (our timer), ota_0 VALID. A on ota_0: `Loaded app from partition at offset 0x20000`,
    `transaction fw3-30s-1792133121: rolled_back (returned to ota_0; ota_1 did not confirm)`,
    then the record cleared.
  - The lower clamp, `confirm_timeout_s: 5` (`fw3-5s-1792133356`): A logs `confirm_timeout_s is
    outside 30-3600 s — using 30 s` and `recorded (… confirm 30 s)`; R arms at 3305, re-arms
    `30 s from image start (19 s left)` at 13425 and fires `no working session 30 s` at 33495
    (30.19 s after the arm); ota_1 `INVALID`; `rolled_back (returned to ota_0; …)`.
  - **About the plan's T window (29500-33000 ms).** The log timestamp counts from power-on, and
    QEMU spends 2.4-2.6 s in the bootloader before `app_main`, where the deadline starts (the
    boot arm is its first statement). The fires at 33039 and 33495 ms are 30.14 s and 30.19 s
    after the arm, so the deadline is exact; the window should have read "30 s after the
    `OTA boot:` line, ±0.5 s".
  - (b) Regression, `just lib-quickstart`: `== PASS (2303 s)`. 2a `capabilities ['ota',
    'selftest']`, agent 0.5.0. 2b B `confirmed`, detail `selftest passed`. 2c R logs `OTA boot:
    60 s` and `E (70434) no working session 60 s after an OTA boot`, ends `rolled_back`, detail
    `returned to ota_1; ota_0 did not confirm` (the server sent 300: the cap holds). 2d F's
    detail is exactly `selftest failed: i2c sensor 0x48 did not ans; returned to ota_1`. 2e T
    logs `E (310806) no self-test result 300 s after an OTA boot`, detail `selftest timed out;
    returned to ota_1` (the default path, no re-arm).
  - (c) `just agent-qemu-smoke esp32`: `HARNESS OK`.
  - Gotcha: in both runs QEMU survived R's `esp_restart()` and booted ota_0 on its own
    (`rst:0xc (SW_CPU_RESET)`, then `Loaded app … 0x20000`), so no stop/start was needed for the
    rollback; the otadata was read after a stop.

### R5-fw-4 as built

- **What was built.** The agent carries out `apply`, `cancel`, `rollback` and `reboot`, and
  answers every verb once on a new topic `up/ack`, unreleased (no version bump; the release
  waits for the owner's Patch A). `ff_mqtt.c` (CRITICAL): `topic_ack`, `enqueue_ack()`, the ack
  vocabulary, `on_apply`/`on_cancel`/`on_rollback`/`on_reboot`, the `not implemented` answer for
  every other id'd type, `schedule_command_reboot()` and its static reboot task, the persisted
  reboot-id dedupe, `other_slot_is_returnable()`, and `classify_txn` composing the command head.
  `ff_ota.c`/`ff_ota.h` (CRITICAL): `ff_ota_txn_phase()`, `ff_ota_cancel()`, a cancel handshake
  under `s_cancel_lock` with five checkpoints in `ota_task`, and `restore_boot_partition(why)`
  returning whether the pointer names the running slot. `ff_txn`: the reboot-id store in its
  own NVS namespace `ff_cmd`. `ff_mqtt_internal.h`: `FF_STATUS_CANCELLED` (ahead of Patch A3).
  `ff_identity.c`: `capabilities` `["ota","apply","cancel","rollback","reboot"]` (plus
  `"selftest"` when registered). No spec, server, simulator, frontend, `Fleetforge.cpp`,
  `fleetforge_start.c`, `agent_main.c`, partition or sdkconfig change.
- **Decisions.**
  - (1) **`up/ack`**, QoS 1, retain 0, enqueued (handler context), keys in A5's order
    `id, type, result, cmd_id, detail` with explicit nulls (`type` null when the command had
    none). Every `detail` is a `#define` of at most 63 printable bytes, each `_Static_assert`ed
    (`ACK_FITS`) and logged with the ack (never a URL). Never `up/status`.
  - (2) **The ack `cmd_id`** is the transaction acted on (apply/cancel: the named one; rollback
    while confirming: the stage's; rollback on a confirmed image: its own `id`; reboot: null) or,
    on a refusal, the transaction the device holds that caused it; otherwise null.
  - (3) **Vocabulary** (the first four are R5-spec-1's, `not implemented` from A5; the rest are
    this agent's words, text for a server to show, never to parse):

    | Constant | Text | When |
    |---|---|---|
    | `ACK_NOTHING_STAGED` | `nothing staged for this cmd_id` | apply: no staged transaction under that `cmd_id` (also before `staged`) |
    | `ACK_NO_IMAGE` | `no confirmed image to return to` | rollback: the other slot is not VALID/UNDEFINED, or IDF says no |
    | `ACK_CANCEL_FIRST` | `cancel first` | rollback while an update is between `staging` and `rebooting` |
    | `ACK_NOT_IMPLEMENTED` | `not implemented` | any id'd type but `stage` and the four verbs |
    | `ACK_NOTHING_TO_CANCEL` | `nothing to cancel for this cmd_id` | cancel: no such transaction before its reboot |
    | `ACK_ALREADY_APPLYING` | `already applying; use rollback` | cancel from `applying` on, or of a booted transaction |
    | `ACK_ROLLBACK_DECIDED` | `a rollback is already decided` | cancel/reboot against a decided rollback |
    | `ACK_UPDATE_IN_PROGRESS` | `an update is in progress` | reboot between `staging` and `rebooting` |
    | `ACK_NOT_CONFIRMED` | `the running image is not confirmed yet` | reboot while confirming (or BOOTED_NEW) |
    | `ACK_REBOOT_PENDING` | `a reboot is already pending` | a second reboot-causing verb inside the 1.5 s drain |
    | `ACK_CMD_ID_MISSING` | `cmd_id missing` | apply/cancel without `cmd_id` |
    | `ACK_ID_TOO_LONG` | `command id too long` | an id of 64 bytes or more |
    | `ACK_CONFIRMING_NOW` | `the image is confirming right now` | rollback after a passing self-test claimed the confirm |
    | `ACK_CANNOT_MARK` | `cannot mark this image invalid` | rollback of a confirmed image whose mark-invalid failed (record reverted) |
    | `ACK_CANNOT_PUT_BACK` | `cannot put the boot slot back` | cancel of a staged image whose otadata write failed |

    `refused` means nothing changed: every refusal is decided before any status, record write,
    mark-invalid or reboot (the one exception, `cannot mark`, reverts its record first).
  - (4) **D8.** No verb reboots in the handler. `schedule_command_reboot(id)` runs last: it
    stores the id with `ff_txn_note_reboot_cmd()` (failure logged W), copies it to
    `s_reboot_cmd_id`, and starts `ff_reboot` with `xTaskCreateStatic` (static 4 KB stack and TCB,
    priority 3), which waits `COMMAND_REBOOT_DRAIN_MS` 1500, logs `rebooting for command <id>` and
    calls `esp_restart()`. The comment `R5-fw-5: the safe-window poll goes here` marks the spot.
    `ff_mqtt_run()` loads the id after `ff_txn_init()` and before `classify_txn()`; `on_command()`
    drops a command with that id (`duplicate command id=… — ignored (this board already rebooted
    for it)`), unanswered.
  - (5) **NVS, not RTC**, for the reboot id: RTC memory dies at a power cut and at every QEMU
    stop/start. Its own namespace (`FF_TXN_CMD_NAMESPACE "ff_cmd"`, key `reboot_id`), because
    `ff_txn_save()` erases `ff_txn` at every stage; overwritten in place, never erased.
  - (6) **ff_ota owns otadata for a stage.** `ff_ota_txn_phase(cmd_id, held, len)` answers NONE,
    TRANSFER, STAGED or APPLYING (read-only). `ff_ota_cancel(cmd_id, &reported)` decides under
    `s_cancel_lock`: in flight → set `s_cancel` (the task reports `cancelled` itself), APPLYING →
    `ESP_ERR_INVALID_STATE`; ota task gone and the image staged → the staged-cancel sequence
    inline, and the caller enqueues `cancelled`. The checkpoints: before `esp_https_ota_begin()`;
    the top of every perform pass (abort after the loop, before the stall branch); before
    `finish()` (abort); after a successful `finish()` and before the record (decided under the
    lock: either the put-back and `cancelled`, never the record, or the phase moves to
    STAGED/APPLYING); and at `done:`, where a cancel that came during the staged exit is acted on
    and `s_running` goes false in the same critical section as the decision. So a cancel is never
    accepted and dropped, never acted on twice, never accepted once APPLYING.
  - (7) **Staged cancel order**: `restore_boot_partition()` first, `ff_txn_clear_if()` only if it
    worked. Nothing else is erased.
  - (8) **Rollback** (`on_rollback`): a rollback already on its way (this verb's, or the
    deadline's/self-test's latch) is `accepted` again; a pending reboot is refused; an update in
    hand is `cancel first`; then `other_slot_is_returnable()` (other slot VALID or UNDEFINED, and
    `esp_ota_check_rollback_is_possible()`; NEW, PENDING_VERIFY, INVALID, ABORTED and no entry are
    refused). An unconfirmed image (`pending_verify()` or BOOTED_NEW) claims the R5-fw-2 latch
    under `s_decide_lock` (a passing self-test that claimed first wins: `confirming right now`),
    stores `rolled back by command` over the transaction's head (or opens its own record when
    there is none), marks invalid (no reboot variant; a failure is logged and the reboot goes
    ahead), enqueues `rolling_back` and schedules the reboot. A confirmed image opens its own
    transaction: `ff_txn_save(id, running, 0)`, the head, mark invalid (failure: record cleared,
    `cannot mark`), `rolling_back`, reboot. The returned-to image composes `rolled back by
    command; returned to ota_0` (40 B), outside the self-test prefix.
  - (9) **Reboot** is refused while a rollback is decided, while an update is in hand, and while
    the image is unconfirmed; an owed `confirmed`/`rolled_back` does not block it.
  - (10) **Apply**: a repeat or a transaction already past `staged` is an `accepted` no-op;
    `STAGED` → `accepted`, `applying`, `rebooting` (enqueued, D3: exactly like auto), reboot;
    anything else, including an apply before `staged`, is `nothing staged for this cmd_id`.
- **Deviations from the plan.** (a) `ff_ota_txn_phase()` takes an out-parameter for the held
  `cmd_id` (the refusals echo it) and, outside the ota task, decides STAGED from otadata plus
  the ff_txn record (its slot and `cmd_id`), not from a RAM phase: a refused second `stage`
  reuses `s_running_cmd_id`, and the staged transaction must stay addressable. (b) A NULL query
  keeps answering TRANSFER while the ota task runs, even with a cancel pending: the boot pointer
  may still be moving, so `reboot`/`rollback` wait for the task to end. (c) A put-back that fails
  at checkpoint 4 does not report `cancelled` (the image would boot at the next reset): the
  transaction carries on, record and all. (d) UNDEFINED in `other_slot_is_returnable` is IDF's
  "state never set" entry; a slot USB-flashed with blank otadata has NO entry, which IDF itself
  cannot roll back to (`esp_ota_check_rollback_is_possible()` needs the inactive entry).
- **Rejected.** Rebooting from the handler or from an esp_timer callback; RTC memory for the
  reboot id; `xTaskCreate` for the reboot task (a heap failure after mark-invalid would leave an
  INVALID image running); an "apply on arrival" latch; acks on `up/status`; erasing the cancelled
  slot or the inactive otadata sector; `esp_ota_mark_app_invalid_rollback_and_reboot()` in a verb;
  announcing `safe_window`; verbs behind a build flag.
- **Named gaps.** (a) After a staged cancel the old image boots `pending_verify` once and
  re-confirms (IDF writes its entry NEW); if that boot fails, the bootloader may fall back to the
  cancelled image, which then confirms itself with no record (in QEMU the next stage's
  `esp_ota_begin()` wiped that entry anyway). (b) A `stage` inside the 1.5 s before a command
  reboot is not refused (R5-fw-5 must refuse it once the wait is unbounded). (c) A redelivered
  `stage` with a cancelled transaction's id runs again (the server must not resend it, R5-be-2).
  (d) An own-transaction `rolled_back` lands in `deploy_events` with no `requested` row (R5-be-2
  keeps it out of the KPIs). (e) The ack is lost if the board is disconnected when it is queued
  and then reboots (the server allows for no answer, A5). (f) A rollback of a confirmed image
  overwrites a record whose `confirmed`/`rolled_back` was still owed; the queued report usually
  leaves inside the drain. (g) The staged cancel's two flash writes run in the esp-mqtt handler:
  7.8 s in QEMU (otadata validates the running image), well inside the 30 s keepalive.
- **Proposed spec addenda (paste-ready, for the owner; `spec/` is not edited).**
  - Patch A2, `apply` bullet, append: "An `apply` that arrives before its transaction reaches
    `staged` is refused; a server sends it after `staged`."
  - Patch A2, `cancel` bullet, append: "A `cancel` that arrives while the device is verifying is
    carried out even if the boot slot has just moved; the device moves it back before `staged`."
  - Patch A5, after the `detail` sentence: "The refusal `detail` texts are not a contract; a
    server shows them as text."
- **Tests.** New `tests/test_agent_verbs.py` (41): the wire (topic, key order, nulls, retain 0,
  never `up/status`, no immediate publish in the handler, the three spec refusals, every detail
  <= 63 printable bytes with its `ACK_FITS`, the command head), dispatch, D8 (no reboot on the
  handler path, the static task after the drain, persist before start, load before connect, the
  duplicate), refusal before change in every verb, rollback (returnable states, the latch, the
  own-transaction order and revert, one head store, the classify compose, reboot refusals),
  ff_ota (the API, the read-only phase query, the five checkpoints, put-back before clear,
  cancel decided under the lock, one boot switch, the fresh handshake), capabilities, the
  reboot-id store. Updated: `test_ff_cfg.py` (`_spec_states` unions Patch A3's line from this
  doc while the spec lacks `cancelled`, switching itself off once pasted; `cancelled` in the
  declared walk; a check that A3 adds only `cancelled`), `test_selftest.py`
  (`ff_txn_set_detail(` count 2 → 3), `test_agent_power_and_size.py` (budgets). **Mutation
  checks**, each red and then reverted: (i) `esp_restart()` in `on_reboot`
  (`test_nothing_on_the_handler_path_reboots`); (ii) the staged cancel clearing before the
  put-back (`test_the_staged_cancel_puts_back_before_it_clears`); (iii) NEW accepted as
  returnable (`test_only_a_valid_or_undefined_slot_is_returnable`); (iv) the pre-finish cancel
  check dropped (`test_a_cancel_before_finish_aborts`); (v) the ack retained
  (`test_queued_qos1_never_retained`).
- **T1.** `just test`: 2063 passed; ruff, format and mypy clean. `just agent-build-all`: four
  `BUNDLE OK`, 0 warnings. `APP_SIZE_BUDGET_BYTES` raised to the measured byte: esp32 1,043,328
  (+5,968), esp32s3 1,023,248 (+5,904), esp32c3 1,084,688 (+6,608), esp32c6 1,134,256 (+6,608).
  `just lib-quickstart --build-only`: `== PASS (728 s)` (Arduino A esp32 1,190,352 B / esp32s3 1,183,504 B, own project, build B, basic_idf esp32 1,118,704 B / esp32s3 1,123,792 B; 0 library warnings). `just agent-qemu-smoke esp32`: `HARNESS OK`.
- **T2 (local dev stack on 8088; api recreated with 10.0.2.2 origins, restored afterwards).**
  Board A = `agent/dist/esp32` (agent 0.5.0, `--fresh`); B = the same source with version 0.5.9
  (uploaded as `0.5.9-fw4-1792143537`); R = `FF_ROLLBACK_TEST=1` (`0.5.0-rbtest-fw4-1792143537`).
  Every command hand-built and published as `ff-commander`; the wire captured with
  `PYTHONUNBUFFERED=1 just mqtt-sub 'ff/v1/d/+/up/#'`.
  - (0) `GET /v1/devices`: `capabilities ["ota","apply","cancel","rollback","reboot"]`.
  - (1) THE acceptance: `{"id":"fw4-ap0-…","type":"apply","cmd_id":"nope-…"}` →
    `up/ack {"id":"fw4-ap0-1792143537","type":"apply","result":"refused","cmd_id":null,"detail":"nothing staged for this cmd_id"}`
    (board: `I (23324) … publish …/up/ack (qos 1, no retain, queued …)`); no `up/status`, no
    reboot. A fresh subscriber to `up/ack` afterwards received nothing: not retained.
  - (2) `identify` → `refused` / `not implemented`.
  - (3) `reboot` `fw4-rb1-…`: `dn/cmd` at 42044, ack `accepted` (`cmd_id` null) at 42444,
    `ff-txn: command … remembered across the reboot` 42704, `W (44214) rebooting for command
    fw4-rb1-…`, then `rst:0xc (SW_CPU_RESET)` and `Loaded app from partition at offset 0x20000`;
    no `up/status`. The emulator then hit the known `esp_timer_impl_init` panic loop; after a
    stop/start the same message logged `duplicate command id=fw4-rb1-… — ignored (this board
    already rebooted for it)`, no ack, no reset.
  - (4) Cancel in transfer: stage B `fw4-c1-…`; at `downloading` the cancel → ack `accepted`
    (`cmd_id` `fw4-c1-…`), `W (403323) update …: cancelled by command`, `up/status cancelled`;
    never `staged`, no put-back line; otadata (stopped): `sector0 ota_0 VALID`, sector1 empty.
  - (5) Cancel of a staged image: `fw4-c2-…` staged (`recorded` 144382, `is staged and bootable`
    144402); the cancel at 144982 → `E (149232) boot partition put back to ota_0: the update was
    cancelled by command and will NOT be booted`, `transaction fw4-c2-… closed — record cleared`,
    ack `accepted`, `up/status cancelled` (enqueued by the session). A new stage `fw4-c3-…` was
    NOT refused (it reached `downloading`; cancelled too). Power cycle: `Loaded app … 0x20000`,
    `ota state pending_verify`, `CONFIRMED`, no `transaction` line, no new `fw4-c2` rows.
  - (6) Apply: `fw4-a1-…` staged; `reboot` → `refused`, `an update is in progress`, `cmd_id`
    `fw4-a1-…`; `rollback` → `refused`, `cancel first`, `cmd_id` `fw4-a1-…`; `apply` →
    `accepted` (`fw4-a1-…`), `applying`, `rebooting`, `W (216879) rebooting for command
    fw4-ap1-…`, `rst:0xc`, `Loaded app from partition at offset 0x200000` (B's slot). QEMU then
    panicked in `esp_timer_impl_init` while PENDING_VERIFY, so the bootloader aborted it (ota_1
    `ABORTED`) and A reported `rolled_back (returned to ota_0; ota_1 did not confirm)`: the
    emulator limit, not the verb. The repeated apply id was a `duplicate … already rebooted for
    it`. B was then confirmed by stage `fw4-a2-…` + power cycle (`confirming on ota_1`,
    `CONFIRMED`).
  - (7) Rollback on confirmed B: `fw4-rl1-…` → `recorded (target slot at 0x00200000, confirm 0
    s)`, `detail stored`, ack `accepted` with `cmd_id` `fw4-rl1-…`, `rolling_back`, `rebooting for
    command fw4-rl1-…`, `rst:0xc`, `Loaded app … 0x20000`. On A: `transaction fw4-rl1-…:
    rolled_back (rolled back by command; returned to ota_0)`, and on the wire
    `{"cmd_id":"fw4-rl1-1792143537","state":"rolled_back","pct":null,"detail":"rolled back by command; returned to ota_0"}`.
    otadata: ota_1 **INVALID**, ota_0 VALID.
  - (8) A second rollback on A → `refused`, `no confirmed image to return to`, no reset.
  - (9) Rollback while confirming: R staged as `fw4-r1-…`, power-cycled: `OTA boot: 60 s`
    (4679), `confirming on ota_1` (15919). `reboot` → `refused`, `the running image is not
    confirmed yet`, `cmd_id` `fw4-r1-…`; `rollback` `fw4-rl3-…` at 21969 → `detail stored`,
    ack `accepted` with `cmd_id` `fw4-r1-…`, `rolling_back` for `fw4-r1-…`, `rebooting for command
    fw4-rl3-…` (28689), `rst:0xc`, `Loaded app … 0x20000`; never `no working session`, never
    `confirmed`. On A: `transaction fw4-r1-…: rolled_back (rolled back by command; returned to
    ota_0)`, the same detail on the wire; otadata ota_1 INVALID.
  - (10) Regression: `just lib-quickstart` `== PASS (2383 s)`: 2a `capabilities ['ota', 'apply', 'cancel', 'rollback', 'reboot', 'selftest']`; 2b `confirmed`, detail `selftest passed`; 2c `no working session 60 s`, `rolled_back` `returned to ota_1; ota_0 did not confirm`; 2d `selftest failed: i2c sensor 0x48 did not ans; returned to ota_1` (63 B); 2e `selftest timed out; returned to ota_1`; credentials in 0 of 29 transcripts.
  - Gotchas: QEMU's soft reset panicked in `esp_timer_impl_init` after EVERY command reboot in
    this run (R5-fw-3 saw it survive), so the proof is the `rebooting for command` line, one
    `rst:0xc` and the bootloader's `Loaded app` offset, and results were read after a stop/start.
    `esp_ota_begin()` invalidates the inactive otadata sector, so a stage after a staged cancel
    wipes the cancelled image's NEW entry.

### R5-fw-5 as built

- **What was built.** The firmware's safe-window rule gates every reboot the agent makes,
  unreleased (no version bump; the release waits for the owner's Patch A). New maker header
  `include/ff_safe_window.h` (`typedef bool (*ff_safe_window_fn)(void *ctx)`,
  `esp_err_t ff_set_safe_window_cb(ff_safe_window_fn fn, void *ctx)`), `src/ff_safe_window.c`
  (TAG `ff-safe`) and `src/ff_safe_window_internal.h` (`ff_safe_window_freeze()`,
  `ff_safe_window_registered()`, `ff_safe_window_is_safe()`, `FF_SAFE_WINDOW_POLL_MS 250`).
  `ff_mqtt.c` (CRITICAL): the freeze and the `safe_window` capability, the rollback gate task,
  the command reboot task's window wait, the `deferred` answers, the cancel of a deferred
  apply, a stage refused while a reboot is on its way. `ff_ota.c`/`ff_ota.h` (CRITICAL): the
  apply:auto wait in a new phase `FF_OTA_TXN_AWAITING` and a sixth cancel checkpoint.
  `Fleetforge.cpp`/`.h` (CRITICAL): `bool onSafeWindow(bool (*isSafe)())` and the `ff-safe`
  log tag. `ff_mqtt_internal.h`: `FF_STATUS_AWAITING_SAFE_WINDOW` and the two details. Both
  worked examples register a rule ("reboot only between two messages"). No spec, server,
  simulator, frontend, `fleetforge_start.c`, `agent_main.c`, partition or sdkconfig change.
- **Decisions.**
  - (1) **API.** C: `ff_set_safe_window_cb(fn, ctx)`, ESP_OK / ESP_ERR_INVALID_ARG (NULL) /
    ESP_ERR_INVALID_STATE (already registered, or the session has frozen the registration; one
    E log line per refusal). One rule per image, never removed. Arduino:
    `Fleetforge.onSafeWindow(isSafe)`, false for nullptr, a second call or after the freeze.
    Unregistered = always safe. The header's contract: true when a reboot right now is safe;
    cheap, non-blocking, reentrant; called from the agent's own tasks (MQTT, OTA, the reboot
    tasks), possibly at once, every 250 ms while a reboot waits and immediately before every
    agent reboot; never from an ISR or the esp_timer task. What waits: apply (auto and
    commanded), the rollback after a failed confirm or self-test, the `rollback` and `reboot`
    commands. What does not: crashes, watchdogs, brownouts, power loss, and the confirm
    deadline itself (decided on time; only the reboot waits).
  - (2) **Frozen at session start**, the self-test's precedent (R5-fw-1): `ff_mqtt_run()` calls
    `ff_identity_note_safe_window(ff_safe_window_freeze())` right after the self-test freeze,
    before `ff_txn_init()`, `classify_txn()` and the first connect. The freeze logs once:
    `a safe-window rule is registered: every reboot this agent makes waits for it` / `no
    safe-window rule registered: this agent reboots as soon as it must`.
  - (3) **Capability** `safe_window` (D9), after `selftest`, only when registered.
  - (4) **Poll 250 ms** (`_Static_assert` <= 1000): the examples' window is the 800 ms gap
    between two messages, and a 1 s poll could phase-lock past it.
  - (5) **`awaiting_safe_window` only when the first evaluation says unsafe**; a board that
    judges the moment safe goes straight on. Details `apply waits for a safe window` (pct 100)
    and `rollback waits for a safe window` (pct null), `FF_DETAIL_*` (not `FF_STATUS_*`: a test
    regex reads those as states), each `_Static_assert`ed <= 63 B.
  - (6) **The deadline / self-test rollback (D12): a static gate task, used ONLY with a rule.**
    `confirm_timeout_cb` (after its claim and its two unchanged log lines), the backstop branch
    and `selftest_done`'s fail (after the head store) call `start_rollback_gate()` when
    `ff_safe_window_registered()`; with no rule every line of the R5-fw-4 esp_timer path is
    unchanged. `start_rollback_gate()` claims `s_gate_started` under `s_decide_lock` and creates
    `ff_rbgate` with `xTaskCreateStatic` (4 KB, priority 6: above every agent task and the
    Arduino loop, below esp_timer/wifi); it never calls the rule, writes flash or takes the
    client lock. `rollback_gate_task`: unsafe → `s_rollback_waiting`, W `the rollback is decided
    and waits for a safe window — this image keeps running until then; a reset rolls it back
    too`, `awaiting_safe_window` + rollback detail if owed and connected, poll, W `a safe window:
    rolling back now`; then `rolling_back` + the 2 s grace (only when a report was queued), the
    rule asked again (a window that closed in the grace is waited for silently),
    `esp_ota_mark_app_invalid_rollback_and_reboot()`. It never touches the confirm timer (D10).
    `report_txn_on_connect()` repeats `awaiting_safe_window` while the gate waits.
  - (7) **Command reboots (D7, D8): `ff_reboot` is persistent**, created once and woken with
    `xTaskNotifyGive` (a static TCB is never re-created). Loop: notify → drain 1.5 s →
    `wait_for_the_window()` (abort → idle; the rule asked outside `s_reboot_lock`; commit under
    the lock only without hold or abort; W `command <id>: the reboot waits for a safe window`
    once) → for a verb answered `deferred`, the release statuses (`applying`, `rebooting` /
    `rolling_back`), drain again, the rule asked again → `rebooting for command <id>` →
    `esp_restart()`.
  - (8) **Verb answers (D2).** The rule is asked once, after every refusal (and after
    rollback's mark-invalid, the D12 shape): `apply` → `accepted` + `applying`, `rebooting`, or
    `deferred` + `awaiting_safe_window` `apply waits for a safe window`; `rollback` → `accepted` +
    `rolling_back`, or `deferred` + `awaiting_safe_window` `rollback waits for a safe window`;
    `reboot` → `accepted` or `deferred`, never a status. Repeats and no-ops keep `accepted`;
    `apply` on an apply:auto in AWAITING is an `accepted` no-op.
  - (9) **`cancel` of a deferred commanded apply (D4).** On a released apply whose answer was
    `deferred` and which the window has not committed, `on_cancel` sets `s_reboot_hold` under
    `s_reboot_lock`, makes its one `ff_ota_cancel()` call, then lifts the hold and sets
    `s_reboot_abort` only on ESP_OK; the reboot task goes idle (`the deferred apply was
    cancelled — no reboot`). An apply answered `accepted` stays uncancellable.
  - (10) **apply:auto waits in `ota_task`.** Checkpoint 4 moves to AWAITING (not APPLYING); after
    `staged`, if the rule says unsafe, `awaiting_safe_window` is published and the task polls
    until safe or cancelled; checkpoint 6 commits APPLYING under `s_cancel_lock`; `done:` treats
    AWAITING as staged (put back, record cleared, `cancelled`); after the drain the rule is asked
    again before `esp_restart()`. An `on_command` stage still stops at `staged`.
  - (11) **A `stage` is refused while a reboot is on its way** (closes R5-fw-4 gap (b)):
    `on_stage`, after the re-delivery drop, publishes `failed` `a reboot is already pending` /
    `a rollback is already decided`.
  - (12) **Examples** register the rule as the third statement: a `volatile` flag set false at
    the top of each message and true after its last letter; `basic_idf/main/main.c` stays at 70
    lines.
- **Deviations from the plan.** (a) A `rollback` command's claim now also sets
  `s_rollback_commanded` (under `s_decide_lock`), and the backstop starts no gate for it: its
  own reboot task already waits for the window, and a second party would repeat the statuses
  and call mark-invalid on an image already marked (IDF then targets the active otadata entry,
  which is the previous image's). (b) The gate waits the 2 s grace only when it queued
  `rolling_back`; with nothing reported the reboot follows the window at once, as the R5-fw-4
  path does. (c) A deferred plain `reboot` has no second drain (nothing to report). (d) T2 used
  a fourth artifact D for apply:auto (the server does not know `cancelled` as terminal until
  R5-be-2, so redeploying C would reuse its cancelled `cmd_id`) and re-confirmed B before the
  rollback (after the staged cancel B's entry was NEW, which a rollback refuses: R5-fw-4 gap
  (a)). (e) `scripts/lib_quickstart.py` phase 2a now also requires `safe_window`.
- **Rejected.** Calling the rule from esp_timer; one shared static task for the command reboot
  and the gate (two parties could create it); a dynamic gate task (a heap failure would skip
  the window or the rollback); pausing or extending the confirm deadline; a deadline or timeout
  on the wait; `force`; routing the unregistered path through the gate; answering repeats
  `deferred`; announcing `awaiting_safe_window` when the first evaluation is safe; marking
  invalid at the deadline's decision time; a test-only build flag for the window.
- **Named gaps.** A verb answered `accepted` that finds the window closed after its 1.5 s drain
  waits with `applying`/`rebooting` (or `rolling_back`) already reported, logged only; the same
  after the final drain/grace re-checks. A rule stuck at unsafe holds the board (an unconfirmed
  or INVALID image) until a reset, which returns it. A hog above priority 6 starves the gate
  the same way. The backstop can start the gate before a starved self-test task stored its fail
  head (reports `selftest timed out`). A reset during a pre-apply wait boots the staged image
  (R5-spec-1, unchanged). A cancelled deferred apply's id stays the remembered reboot id (its
  redelivery is dropped with the "already rebooted" wording). The deadline's backstop without
  a rule still reboots at once inside a `rollback` command's 1.5 s drain (R5-fw-4 behaviour,
  its second mark-invalid fails harmlessly).
- **Proposed spec addenda (paste-ready, for the owner; `spec/` is not edited).**
  - Patch A2, after the *Every reboot waits…* bullet: "A `stage` that arrives while a reboot
    waits for the safe window, or after the device has decided to roll back, is refused."
  - Patch A4, *The safe window gates the rollback too*, append: "The device reports
    `awaiting_safe_window` only when it judges the moment unsafe; otherwise it goes straight on."
  - Patch A5, after the `deferred` bullet: "A command that repeats one already accepted or
    deferred is answered `accepted`; only the command that starts a wait is answered `deferred`."
- **Tests.** New `tests/test_safe_window.py` (45): the header, the registration (refusals, the
  freeze logs once, the rule called once outside the lock, `registered` never calls it, no log
  in either hook), the poll (<= 1000, every wait loop sleeps it), the timer side (no rule call
  in esp_timer context, the three hand-offs and their positions, the static gate started once,
  wait → report → grace → re-check → reboot, no confirm-timer touch, the reconnect repeat, every
  hand-off behind the registered check), the command task (created once, notify, drain < window
  < log < restart, commit under the lock without hold/abort, release statuses, idle on abort),
  the verbs' deferred branches, the cancel hold, ff_ota (AWAITING, checkpoint 6, the re-check,
  the staged exit), the stage refusal, the capability, the Arduino member, both examples.
  Updated on purpose: `test_ff_cfg.py` (`awaiting_safe_window` in the declared walk),
  `test_agent_verbs.py` (checkpoint 4 assigns AWAITING, the staged exit takes AWAITING, the
  enum order, `safe_window` from a negative to a positive pin), `test_selftest.py`
  (`ff_safe_window_is_safe` in `TIMER_FORBIDDEN`, `start_rollback_gate` a timer-context
  function, the freeze-order pin), `test_ota_component.py` (`MAKER_HEADERS`,
  `INTERNAL_HEADERS`, `HAZARDS`), `test_agent_power_and_size.py` (budgets). **Mutation
  checks**, each red and then reverted: (i) the rule called in `confirm_timeout_cb` (4 red);
  (ii) the backstop's hand-off dropped (2); (iii) checkpoint 4 back to APPLYING (2); (iv) the
  commit without checking `s_reboot_hold` (1); (v) the `on_stage` refusal removed (1); (vi)
  `esp_timer_stop(s_confirm_timer)` in the gate (1).
- **T1.** `just test`: 2108 passed; ruff, format and mypy clean. `just agent-build-all`: four
  `BUNDLE OK`, 0 warnings. `APP_SIZE_BUDGET_BYTES` raised to the measured byte: esp32 1,046,288
  (+2,960), esp32s3 1,026,240 (+2,992), esp32c3 1,088,048 (+3,360), esp32c6 1,137,632 (+3,376).
  `just lib-quickstart --build-only`: `== PASS (554 s)` (Arduino A esp32
  1,193,744 B / esp32s3 1,186,800 B, own project, build B, basic_idf esp32 1,121,856 B / esp32s3
  1,127,184 B; 0 library warnings). `just lib-arduino-check`: `== PASS (241 s)`, 0 warnings, `T verifyRollbackLater` in both ELFs (esp32 1,183,088 B, esp32s3 1,173,760 B). `just
  agent-qemu-smoke esp32`: `HARNESS OK`.
- **T2 (local dev stack on 8088; api recreated with 10.0.2.2 origins, restored afterwards).**
  Arduino Basic variants, never committed: W (rule `millis() % 80000 >= 60000`: unsafe 60 s,
  safe 20 s) as A `1.0.0-fw5w`, B `1.5.0-fw5-1792152873`, C `1.6.0-…`, D `1.6.1-…`; L (rule
  `millis() > 400000`, the loop answers the self-test only after 320 s) as
  `1.7.0-fw5-1792152873-late`. Verbs published as `ff-commander`; the wire captured with
  `just mqtt-sub 'ff/v1/d/+/up/#'`. Timing: the log timestamps are FreeRTOS ticks, which run
  5-8 s ahead of `millis()` (esp_timer) in QEMU, so `millis()` was estimated from the wall clock
  since the ROM reset line (minus 1.2 s); each release below landed 250-260 ms after the window
  opened by that estimate, one poll.
  - (1) A `--fresh`: `ff-safe: a safe-window rule is registered…` (12652) before `connecting to`
    (12702); `GET /v1/devices` `capabilities ["ota","apply","cancel","rollback","reboot","selftest","safe_window"]`.
  - (2) B staged `on_command` (`bf6be9fc…`); `apply` `fw5-ap1-…` at millis ~121 s →
    `{"id":"fw5-ap1-1792152873","type":"apply","result":"deferred","cmd_id":"bf6be9fc…","detail":null}`,
    `{"state":"awaiting_safe_window","pct":100,"detail":"apply waits for a safe window"}`,
    `command fw5-ap1-…: the reboot waits for a safe window`; nothing for 18 s; at millis ~140.25 s
    `applying`, `rebooting`, and 1.5 s later `rebooting for command fw5-ap1-…`. QEMU was killed
    226 ms after that line (before the soft reset), and the cold boot ran B: `confirming on
    ota_1`, `custom self-test passed`, `CONFIRMED`, `confirmed` `selftest passed`.
  - (3) C staged (`a295a513…`); `apply` → `deferred` + `awaiting_safe_window`; `cancel`
    `fw5-cx2-…` 4 s later in the same unsafe stretch → `boot partition put back to ota_1: the
    update was cancelled by command…`, `record cleared`, ack `accepted`, `cancelled`, `command
    fw5-ap2-…: the deferred apply was cancelled — no reboot`; through the whole safe stretch
    140-160 s (checked at ~172 s): no `rebooting for command`, no `rst:`, still `morse: HI`.
  - (4) D deployed `apply: auto` (`e47f9e08…`): `staged` → W `ota_0 is staged; the reboot into it
    waits for a safe window` → `awaiting_safe_window` `apply waits for a safe window` at millis
    ~245 s → `applying`, `rebooting` at ~300.25 s; killed in the drain, cold boot D: `confirming on
    ota_0`, `CONFIRMED`, `confirmed` `selftest passed`.
  - (5) On D, `reboot` `fw5-rb5-…` at ~80 s → `{"result":"deferred","cmd_id":null,"detail":null}`,
    no `up/status`; a deploy of B meanwhile → `stage id=e40b347b… refused: a reboot is already
    pending`, `up/status failed` `a reboot is already pending`, no `staging`; `rebooting for
    command fw5-rb5-…` at ~140.25 s, `rst:0xc`. The soft reset then hit the emulator's
    `esp_timer_impl_init` panic loop, so `reset_reason sw` could not be observed; the power cycle
    announced `poweron`.
  - (6) After re-confirming B (`c07f654c…`, staged + power cycle), `rollback` `fw5-rl6-…` at ~80 s
    → record `fw5-rl6-…` + head stored, `this image is marked invalid — the return waits for a
    safe window`, `{"result":"deferred","cmd_id":"fw5-rl6-1792152873"}`, `awaiting_safe_window`
    `rollback waits for a safe window`; at ~140.25 s `rolling_back`, `rebooting for command
    fw5-rl6-…`, `rst:0xc` (panic loop); power cycle: D reports `rolled_back` `rolled back by
    command; returned to ota_0`.
  - (7) THE D12 case. L staged from D (`793c6f0f…`), power-cycled: `OTA boot: 300 s`, `confirming
    on ota_1`, `running the custom self-test`; at millis ~303 s `no self-test result 300 s after
    an OTA boot — marking this image invalid…`, `the rollback is decided and waits for a safe
    window — this image keeps running until then; a reset rolls it back too`, `up/status
    awaiting_safe_window` `rollback waits for a safe window` (L's `cmd_id`); at ~323 s `the
    self-test answered after the confirm deadline — the rollback already decided stands; NOT
    confirming`; at ~400.26 s `a safe window: rolling back now`, `rolling_back`, `rst:0xc`; the
    emulator booted D on its own: `transaction 793c6f0f…: rolled_back (selftest timed out;
    returned to ota_0)`. Never `CONFIRMED` (0 lines), never `confirmed` on the wire; otadata
    ota_1 `INVALID`, ota_0 `VALID`; the API row: `rolled_back`, terminal, `fw_version` D, steps
    `… staged, confirming, awaiting_safe_window, rolling_back, rolled_back`.
  - (8) Regressions. `just lib-quickstart` (the committed example now has a rule): `== PASS (2375 s)`;
    2a `capabilities ['ota', 'apply', 'cancel', 'rollback', 'reboot', 'selftest', 'safe_window']`;
    2b `confirmed`, detail `selftest passed`; 2c `no working session 60 s`, `rolled_back`
    `returned to ota_1; ota_0 did not confirm`, steps `… confirming, awaiting_safe_window,
    rolling_back, rolled_back` (the gate, released in the gap between messages); 2d `selftest
    failed: i2c sensor 0x48 did not ans; returned to ota_1` (63 B), the same steps; 2e `no
    self-test result 300 s`, `selftest timed out; returned to ota_1`, the same steps; no
    `confirmed` anywhere; credentials in 0 of 29 transcripts. The stock agent (`agent-qemu esp32
    --fresh`): `no safe-window rule registered: this agent reboots as soon as it must` before
    `connecting to`, `capabilities ["ota","apply","cancel","rollback","reboot"]` (no
    `safe_window`); `just agent-qemu-smoke esp32`: `HARNESS OK`.
  - Gotchas: the first `lib-qemu --fresh` (run under `script` for a TTY) panicked in
    `task_wdt_isr` before `app_main` (the known boot-loop panic, *What we know about the
    boot-loop panic*); the same bundle booted cleanly without the TTY. `agent/tools/otadata.py`
    needs a `flash_size` the library bundle's manifest lacks (patched copy used).

### R5-be-1 as built

- **What was built.** The server reads the firmware's self-test result out of a deploy's
  `detail` and gives clients a structured `selftest`. New pure module
  `src/fleetforge/selftest_outcome.py` (`SELFTEST_DETAIL_RE` with `.fullmatch()`,
  `SelfTestOutcome`, `parse_selftest_detail`, `selftest_outcome(state, detail)`).
  `DeploySnapshot.selftest` (`deploys.py`, read from the newest row), `DeploySelfTest` +
  `DeploySummary.selftest` on `GET /v1/devices`, `DeviceEvent.selftest` on the terminal
  `device.deploy` frame (`events.py`, `ingestor/handlers.py`). The simulator learns a
  self-test (`selftest` capability, `--selftest-fail-marker`, slot labels).
- **Decisions.** Parsed on read, never stored (no migration, no backfill, `deploy_events`
  writes unchanged). The grammar counts only on its own state: `passed` on `confirmed` with
  no tail, `failed` / `timed_out` on `rolled_back`; anything else is null. KPIs and the
  terminal vocabulary do not change: a self-test rollback is a `rolled_back`, no
  `selftest_failed` state. Outcome names are `passed`, `failed`, `timed_out`. Details:
  `DECISIONS.md` -> R5-be-1.
- **Not provable here.** The real agent's wire (covered by R5-fw-2/4/5 T2 and the firmware
  parity test, which reads the `#define`s from `ff_mqtt.c`), and the dashboard rendering
  (R5-fe-1, which mirrors `DeploySelfTest` into `frontend/src/api.ts` as an optional field).
  The simulator does not gate the rollback on a safe window (D12).
- **Tests.** `tests/test_selftest_outcome.py` (grammar table, firmware `#define` parity,
  state-consistency table); `test_api_devices` (seven states and the `requested` blob),
  `test_ingestor` (terminal frame, walking row, replay, redacted reason), `test_events_stream`
  (key set gains `selftest`, nested object forwarded verbatim), `test_simulator` (pass, fail,
  hang, legacy wording, slot flips, flag validation, every composed detail parsed by the
  server's parser). Mutation checks (state rule, `.match`, frame guard) are in DECISIONS.
- **T1.** `just test`: 2174 passed, ruff, format and mypy clean.
- **T2.** Dev stack, simulated boards `r5be1-1792163058-01` (self-test registered) and a control:
  good build `confirmed` / `selftest passed` / `passed`; `-stfail` `rolled_back` /
  `selftest failed: simulated self-test failure; returned to ota_1` (63 B) / `failed` with
  the reason and `ota_1`; `-hang` `rolled_back` after 9.6 s / `selftest timed out; returned to
  ota_1` / `timed_out`; control `confirmed`, detail and selftest null. One terminal frame per
  transaction, equal to the API; `selftest` null on every other frame; no URL on the stream;
  no token in the logs.

### R5-be-2 as built

- **What was built.** The server half of the four verbs. `POST /v1/devices/{id}/deploys/{cmd_id}/apply`,
  `…/deploys/{cmd_id}/cancel`, `POST /v1/devices/{id}/rollback`, `POST /v1/devices/{id}/reboot`
  (admin-only, no body, 202 `CommandAccepted`) and `GET /v1/devices/{id}/commands/{command_id}`
  (`CommandRecord`), in a new router `api/routers/commands.py`. New table `device_commands`
  (migration 0010, `DeviceCommand`), written and read only by the new `device_commands.py`: one
  row per verb, before the publish, plus the board's first `up/ack`. `broker/commands.py`:
  `COMMAND_APPLY/CANCEL/ROLLBACK/REBOOT`, `VERB_COMMANDS`, `verb_payload()` (Patch A2 shapes).
  `DeployState.CANCELLED`, terminal. `deploys.py`: `counts_in_deploy_kpis()` /
  `KPI_EXCLUDED_TERMINAL_STATES` and `deploy_transaction()`. `deploy_precheck.apply_refusals()`
  (`no_apply_capability`) on `/deploy` and `/deploy/precheck`. Ingestor: `UpChannel.ACK`,
  `AckPayload`, `ACK_MAX_BYTES` 1024, `_store_ack`, `EventType.DEVICE_COMMAND` and
  `DeviceEvent.command_id`. `GET /v1/devices` (and PATCH): `DeviceSummary.last_command`
  (`CommandSummary`), directly before `deploy`. The simulator carries out and answers the verbs.
  No spec, firmware, frontend, ACL, dynsec or env change.
- **Decisions** (details in `DECISIONS.md` -> R5-be-2).
  - (1) **The server gates only what it alone knows**, in order: device (404), the verb in
    `capabilities` (409, D9), for apply/cancel a plausible `cmd_id` (400), a transaction the server
    recorded for this board (404), not ended (409 naming the terminal state), and for apply one sent
    `on_command` (409). Whether anything is staged, in flight or confirmed is the board's answer.
  - (2) **Record, commit, publish.** A refused publish sets `publish_failed` and answers 503.
    **No verb ever writes `deploy_events`**, not `requested`, not `failed`.
  - (3) **First answer wins**, matched on `(id, device_id, type)` and `answered_at IS NULL`; an
    ack for an id the server never sent creates no row (INFO with the id's length). `result` is
    an open vocabulary (printable, at most 16 characters); `detail` is stored through
    `sanitise_device_text` and never logged.
  - (4) **`cancelled` is terminal**, so a re-deploy of a cancelled version mints a new `cmd_id`
    (closes R5-fw-4 gap (c)).
  - (5) **KPI rule** (`counts_in_deploy_kpis`): a transaction counts only with a `requested` row
    and not ended `cancelled`. A rollback of a confirmed image is its own transaction under the
    command's id, recorded from the board's `up/status` with NULL versions and no `requested`
    row, so it counts nowhere (closes gap (d)). A rollback during `confirming` ends the stage's
    transaction, which keeps counting as a `rolled_back`.
  - (6) **`apply: "on_command"` needs the `apply` capability** (`no_apply_capability`, 409 on
    `/deploy`, a refusal in the pre-check).
  - (7) **The simulator learns the verbs** only when it announces them; a board with none ignores
    them silently (a pre-R5 agent). `_release` is the one walk for `auto` and the `apply` verb
    (D3); `returnable` models the confirmed other slot; `--safe-window hold` answers `deferred`.
    An `auto` stage parked by `hold` is now held as `staged` (cancellable, and a second stage is
    refused, as on metal).
- **Deviations from the plan.** The verbs live in a new `api/routers/commands.py`, not in
  `deploys.py`/`devices.py`, which keep their deploy-scoped tripwires. `DeployTransaction` has
  `ended_state` (the first terminal state, for the 409 sentence) with `ended` as a property. The
  ack's `type` must match in the `UPDATE` itself. `--capabilities` takes several values per flag
  (`action="extend"`), as the acceptance criteria spell it; repeated flags still work.
- **Named gaps.** (a) `CommandSummary` does not surface the answer's `cmd_id` (stored as
  `answer_cmd_id`): R5-fe-1 may want it to show which transaction a refusal names. (b) An ack
  that arrives before the API's commit cannot happen in practice (record, commit, publish), but
  an ack for a row whose publish was marked failed after the board received it (a lost PUBACK)
  is not recorded. (c) The simulator's download is inline, so a `cancel` only ever meets a
  staged image; it does not read `confirm_timeout_s` (R5-test-1).
- **Tests.** New `tests/test_api_commands.py`; `test_ingestor` (ack recording, first answer,
  other board, unknown id, type mismatch, retained, oversized, malformed, open vocabulary,
  sanitised and unlogged detail, publish-failed, no deploy row; `cancelled` terminal; the
  rollback-own-transaction and the KPI table); `test_simulator` (every verb, hold, legacy,
  `not implemented`, vocabulary tripwires, detail sizes, `selftest_outcome` None for the command
  head); `test_broker_commands` (`verb_payload`); `test_deploy_precheck` (`apply_refusals`);
  `test_api_deploy` (on_command gate, precheck, cancelled not reused, the no-timer tripwire
  extended to the two new modules); `test_schema` (table, defaults, cascade); `test_invariants`
  (single writer); `test_events_stream` and `test_api_devices` (pinned key sets, on purpose).
- **T1.** `just test`: 2301 passed; ruff, format and mypy clean. Six mutation checks red, then
  reverted (DECISIONS).
- **T2.** Dev stack on 8088, api and ingestor restarted (0009 -> 0010 applied), boards
  `r5be2-1792165060-v` (R5 verbs), `-old` (`ota` only) and `-h` (R5 verbs, `--safe-window hold`),
  labels `1.x.0-r5be2-1792165060`. Legacy: 409 on reboot, 409 `no_apply_capability`, pre-check
  `deployable: false`, nothing published. Apply: `staged` → 202 → ack `accepted` 0.6 s later,
  `confirmed` on B in 2.6 s, one `device.command` frame with the command id. Apply again: 409
  `already ended (confirmed)`. Cancel: `cancelled`, `is_terminal: true`; re-deploy minted a new
  `cmd_id`. Rollback: ack `cmd_id` = its own id, `rolling_back`, `rolled_back` `rolled back by
  command; returned to ota_0`, back on A, `sent_by`/`selftest` null, 0 `requested` rows, NULL
  version, `counts_in_deploy_kpis` False; again → `no confirmed image to return to`. Reboot:
  `accepted`, next announce `reset_reason: sw`, no status. Hold: reboot `refused` `an update is
  in progress` (the stage's `cmd_id`), apply `deferred` + `awaiting_safe_window`, unchanged after
  65 s, cancel `accepted` → `cancelled`. A hand-published late `refused` for the first command
  changed nothing; an unknown id created no row. Only the rollback's two rows sit under any
  command id in `deploy_events`. No token or signed URL on the SSE stream; `late` never in the
  api or ingestor logs.

## Reuse note

The self-test entrypoint defined here moves by the simulation gate
(simulation.md, R9 — V2) with **no code change** — one test, three enforcement points.
