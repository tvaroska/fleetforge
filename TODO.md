# Fleetforge — TODO

**Goal:** Self-hosted OTA firmware management for embedded fleets (ESP32 first) — a bad
build is caught before the fleet, and any device that gets one recovers itself.
**Updated:** 2026-09-22

## Where this stands

**R0-test-2 passed on 2026-09-19 — the product works on metal.** Confirmed against the
prod database on 2026-09-22. Device `94a990dd09a4`, an **ESP32-S3**, flashed from
`bingo.tvaroska.sk` with agent 0.2.0, layout `ab-4m-v1`, `ota_slot_size` 1966080. The
whole ladder is in `device_progress`, 13 seconds end to end:
`link_up(wifi) → time_synced → enrolling → enrolled → mqtt_connected`, and `last_seen`
runs 66 s past enrolment. That is R0's stated risk, and it is retired.

Three details the DB settles that the bench recollection did not:

- **It is an ESP32-S3** — not the classic DevKit v1 that the brownout work is about, and
  not the board from the 2026-09-14 ESPHome comparison. Different silicon, different
  regulator, almost certainly a different supply.
- **So it also unblocks `S0-test-2`**, which has been parked since 2026-09-11 on "no C3,
  C6 or S3 on hand". One is on hand, and it enumerates.
- **The board has been unplugged since**: `presence_reported = f`, `last_seen`
  2026-09-19T20:19Z. The pass was a real live connection, not a durable fleet member.
  Nothing is online right now.

**R0 closed on 2026-09-22.** `R0-test-2` retired the hardware risk on 2026-09-19, and
`S0-test-3` passed unaided onboarding on 2026-09-22. R0's entire risk is retired.

**What the pass changed, beyond R0.** `R0-test-2` was the practical dependency under
`R1-test-1`; a board that could not enroll could not be deployed to. An enrollable board
now exists, so **R1's last task is unblocked** and R1 can close on the same bench session
that closes R0. And S0-fw-3 is **no longer release-blocking** — it is a recovery defect on
one DevKit v1 rather than the thing standing between us and a fleet.

Remaining bench order — all of it starts with re-plugging the S3, which is offline:

1. **R1-test-1** — newly unblocked, and a P0 that closes a whole release. Deploy to the
   S3 once it re-announces.
2. **S0-test-1** — the four serial-console properties that only a USB bridge chip can prove.
3. **S0-test-2** — newly unblocked by the S3 above; the native-USB re-acquire path.
4. **S0-fw-3** (`- [!]`, below) — flash v0.2.0 to the *stuck* DevKit v1. No longer urgent,
   but still the clean single-variable experiment, and still a real defect.

**Prod serves the v0.2.0 agent** (`d705652` — `-Os`, 80 MHz, max modem sleep, the
TX-power ladder), shipped in v0.3.5. That is what the S3 was flashed with, and it
is also step 1 of the S0-fw-3 bench order — so retrying the stuck board needs no special
build, just a flash.

**R1 is open alongside R0, from 2026-09-16.** This file normally carries Sprint 0 plus
*one* release; it now carries two, because R0 is not in progress — it is parked on
hardware, and waiting for a board is not a reason to stop building. R1's blocker closed
on 2026-09-15 (`R1-BE-0`, impersonation + verified `signBlob`), and **all seven of its
desk-bound tasks have landed** — `R1-be-1` the day R1 opened, then `R1-be-2`, `R1-be-3`,
`R1-be-4`, `R1-fw-1`, `R1-fw-2` and `R1-fe-1` on 2026-09-17. A deploy mints a link on our
own origin, `GET /v1/artifact/{sha256}/bin` serves it with range support, the agent
stages and applies it in QEMU (`docs/runbooks/agent-qemu.md`) and reports the version
that actually booted, and the dashboard drives the whole thing. Only `R1-test-1` is
left: it is bench-gated, and deliberately written as the *hardware* E2E rather than
renamed to something QEMU can pass. See `DECISIONS.md` 2026-09-16.

**Completed work is archived**, not lost: R0 and the closed Sprint 0 tasks are written up
in [docs/features/](docs/features/) — chiefly `enrollment.md` (the R0 flow end to end,
plus the four *Unaided onboarding* console tasks) and `infrastructure.md` (the stack, the
prod hand-off, and the five artifact-storage tasks). Reasoning is in `DECISIONS.md`.

Two results worth carrying forward, because they retired earlier conclusions:

- **The brownout is ours, not the supply's** (settled 2026-09-14). A brand-new board on
  the same cable and port ran ESPHome through a cold full RF calibration and survived.
  The "marginal supply / add bulk capacitance" reading is retired along with everything
  built on it. A broader ESPHome comparison — what to reuse, copy, or refuse — is in
  `products/docs/esphome-review.md`.
- **The R1 artifact blocker is closed.** Open since R0-be-6, it was never an org-policy
  exemption — `constraints/iam.disableServiceAccountKeyCreation` makes a GCS key file
  impossible, and the answer was impersonation. Prod reads the store keylessly, verified
  from inside the container, and `signBlob` is measured rather than assumed. All five
  artifact tasks are done; the build engine itself stays R10, only its cache key changed.

<!-- Counters: spec=1 infra=7 db=1 be=6 fe=7 sec=1 fw=4 test=3 -->
<!-- Sprint 0 counters: fe=7 fw=4 infra=7 test=3 -->
<!-- R1 counters: be=3 fe=1 fw=2 test=1 -->
<!-- R3 counters: spec=1 fw=4 test=1 -->

Live status lives ONLY here. States: `- [ ]` open · `- [x]` done · `- [!]`
attempted-but-failed. `spec/` and `design/` are status-free.

> Requirements: [spec/prd.md](spec/prd.md) · Wire contract: [spec/device-protocol.md](spec/device-protocol.md) ·
> Flows: [spec/flows.md](spec/flows.md) · Contracts & platform design: [design/architecture.md](design/architecture.md) ·
> Topology & stack: [design/production.md](design/production.md) ·
> Image storage & versioning: [design/artifacts.md](design/artifacts.md)
> Release ladder: [docs/roadmap.md](docs/roadmap.md) · [docs/releases.md](docs/releases.md) ·
> Completed work: [docs/features/](docs/features/) · Decisions: `DECISIONS.md`

> **Task IDs:** fleetforge is release-driven, so IDs are `R{N}-{category}-{number}`
> (e.g. `R0-be-1`). Sprint 0 uses `S0-{category}-{number}`.
> Categories: db, be, fe, test, qa, sec, infra, fw, spec, rel, perf.

**Deployment (v1):** single hosted instance at `bingo.tvaroska.sk` (domain reused from
the retired bingo app), single-tenant, **not a public product until V3**.
**Versions:** V1 = R0–R6 (safe OTA, ~5 boards) · V2 = R7–R11 (VCS + compile + simulation)
· V3 = robotic swarm (gateway + drones).

---

## Sprint 0: Critical Issues

Bricking risks, broker auth and security issues get filed here as they surface.

- [!] **S0-fw-3**: A board that browns out during RF calibration cannot escape it (P1, 0.5d)
      Found 2026-09-12 from an operator's diagnostic bundle: six boots, each one
      `phy_init: failed to load RF calibration data (0x1102), falling back to full
      calibration` and then `E BOD: Brownout detector was triggered`. Self-sustaining —
      the full calibration is the biggest current draw in startup, the rail collapses
      during it, and the result is only cached once a boot survives it, so every boot is
      identical. (This entry originally added "the same cable and port run a plain Wi-Fi
      sketch fine, because that sketch inherits a calibration it never has to re-earn."
      That reasoning is retired by the 2026-09-14 result below — a board with nothing
      cached on either side survived.)
      Fix: `CONFIG_ESP_PHY_REDUCE_TX_POWER=y` — after a brownout reset the PHY comes up
      at its lowest TX power, which is often enough to get through once; one survived
      boot caches the calibration and the loop ends. Fleet-wide TX power deliberately
      left at 20 dBm. Plus `agent_main.c::log_power_fault()` and the `brownout` progress
      stage, so the boot that escapes SAYS it escaped instead of looking healthy.
      Acceptance: a board in the loop reaches the fleet, and its recovery is visible on
      the dashboard and in the flashing console rather than inferred from a UART log.

      **Attempted 2026-09-12, shipped in v0.3.3, and it does not clear the fault.** The
      reporting half works. The escape does not: on the one board in the loop, reduced-TX
      calibration dies in exactly the same place as full-power calibration. A bundle from
      2026-09-13T14:15 contains the A/B in a single log — its first boot follows a
      non-brownout reset, so the option was inactive and the PHY came up at full power,
      and boots two through six all print `the previous boot ended in a BROWNOUT`, so it
      was active. Every one of the six dies at `phy_init … falling back to full
      calibration` → `E BOD`. The lever is aimed correctly — IDF v5.5.5 `phy_init.c`
      passes the reduced `init_data` *into* `register_chipv7_phy()` with
      `calibration_mode == PHY_RF_CAL_FULL`, so it applies during the calibration, not
      after it — and it simply does not move this board. Whatever dominates the current
      draw of a cold calibration here, it is not TX power.

      Left open because the acceptance criterion is unmet, not because the code is wrong:
      the shipped change is a correct, inert-on-healthy-boards improvement and is worth
      keeping. What was still unknown on 2026-09-13 was whether the remaining fault is
      this board's regulator or something the agent does. **That is now known.**

      **2026-09-14 — settled: the fault is ours.** A *brand-new* board — same cable, same
      port — was flashed with ESPHome, associated to Wi-Fi and ran. A new board has no
      cached calibration, so ESPHome ran the same cold full calibration that kills our
      image, on the same rail, and survived it. That is the discriminating experiment this
      entry asked for, arriving on the "it survives" branch: **this supply carries a cold
      full RF calibration, and our startup draws more than it needs to.** The
      "marginal supply / bulk capacitance across 3V3/GND" reading is retired, along with
      every conclusion built on it (see DECISIONS.md 2026-09-14).

      **And the failing image was never the current one.** The 2026-09-13T14:15 bundle is
      agent `19b0a0b`, which predates `d705652` (`-Os`, 80 MHz, max modem sleep, TX-power
      ladder). Every brownout on record was therefore produced by a **160 MHz, `-Og`**
      build. The 80 MHz lever below has still never reached this board.

      **Second lever prepared 2026-09-13, not yet tried on hardware: 80 MHz CPU**
      (`CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_80`, in `agent/sdkconfig.defaults`). Came out of
      an agent power/size review. `esp_clk_init()` applies the clock before `app_main`,
      so it is already in effect during the calibration window — and the CPU is running
      flat out alongside the calibration at 160 MHz, worth roughly 20-30 mA. The v0.3.3
      result rules out TX power as the dominant draw; it does not rule this out. Unlike
      `REDUCE_TX_POWER` it applies on EVERY boot, so it does not need the fault to have
      already happened in order to help. Free to test: the same board, cable and port,
      one flash.
      Bench order for the next session, cheapest first — **rewritten 2026-09-14**, since
      the hardware branch is now closed and everything below is a firmware question:
      1. **Flash v0.2.0 (`d705652`) to the board.** 160 → 80 MHz plus `-Os`, one flash,
         no new hardware. The single most plausible untested lever, and the only one that
         has already been written. **Since v0.3.5 this is what prod serves**, so it is now
         the default flash from `bingo.tvaroska.sk` rather than a special build.
      2. **Diff `agent/dist/esp32/sdkconfig.resolved` against ESPHome's**
         (`.esphome/build/<node>/.pioenvs/<node>/sdkconfig`). Both files exist; stop
         reasoning about current draw and read the delta. Fields that move startup current
         or the trip point: `ESP_DEFAULT_CPU_FREQ_MHZ`, `ESPTOOLPY_FLASHMODE`/`FLASHFREQ`
         (ours is already the low-power `dio`/`40m`), `SPIRAM`, `ESP_WIFI_*_BUFFER_NUM`.
      3. **`ESP_BROWNOUT_DET_LVL_SEL` and `ESP32_REV_MIN`.** We are on level 0 and
         `ESP32_REV_MIN_0`, both IDF defaults. In IDF v5.5
         `components/esp_hw_support/port/esp32/Kconfig.hw_support`, the rev-0 option
         carries `select ESP_BROWNOUT_USE_INTR` — *"Brownout on Rev 0 is bugged, must use
         interrupt"* — so our min-revision choice force-enables the interrupt-based
         detector that `agent_main.c::log_power_fault()` already documents. If the board
         is rev 1 or rev 3 and ESPHome builds for a higher min revision, the two images
         are using **different brownout mechanisms on the same silicon**, which produces
         this symptom with no difference in current at all. The revision is in the boot
         banner of the bundles already collected.
      One survived calibration ends the loop permanently for that board — the result is
      cached in NVS — so any of these succeeding once is a pass, and the acceptance
      criterion (recovery visible on the dashboard) is reachable from any of them. S0-fw-4
      has landed, so that result is now durable: the flasher erases nothing and a reflash
      no longer throws a survived calibration away.

      **2026-09-19 — de-escalated, but on weaker evidence than it first looked.**
      `R0-test-2` passed on a different board, so this **no longer blocks a release**: it
      is one board's recovery defect, not a gate.
      **It says almost nothing about the firmware, though.** The board that passed is an
      **ESP32-S3** (`94a990dd09a4`), not the classic DevKit v1 this entry is about —
      different silicon, different regulator, different supply. The tempting read, "our
      agent survived a cold full calibration so our startup draw is fine", does **not**
      follow: the S3 never ran the experiment this entry describes. The 2026-09-14
      conclusion — the supply carries a cold calibration and our startup draws more than it
      needs to — therefore **stands unchallenged**, and the open question is still
      whichever of the three levers below moves the DevKit v1.
      Kept at P1, because the defect it names is a fleet property, not one board's: *any*
      board that browns out during calibration is permanently stuck, and a product whose
      pitch is "any device that gets a bad one recovers itself" cannot ship that. The
      acceptance criterion is unchanged, and step 1 of the bench order above — v0.2.0 onto
      the stuck DevKit v1 — is still the untried single-variable experiment.
      _(attempted 2026-09-12; negative result recorded 2026-09-13; second lever staged
      2026-09-13; hardware cause ruled out 2026-09-14; de-escalated 2026-09-22 when
      R0-test-2 passed on a different board; awaiting bench)_

- [ ] **S0-test-1**: Bench-verify the serial console on real hardware (P1, 0.5d)
      Filed 2026-09-10, when S0-fe-1 shipped. Its software half is proven in jsdom against
      replays of real `agent/main/*.c` output; these four cannot be, because they are
      properties of a USB bridge chip and an OS, not of the classifier. The bench is the
      Mac — the Linux dev box does not enumerate boards over WebSerial.
      * **Re-acquire after `hard_reset`, bridge-chip path.** `serialConsole.ts` re-reads
        `navigator.serial.getPorts()` every 250 ms for 8 s. On a classic esp32 the port
        *survives* the reset, so this must reconnect without ever showing "No board is
        available to watch". The native-USB half of this check is **S0-test-2** — no
        C3/C6/S3 board is on hand (2026-09-11).
      * **115200 decodes cleanly.** `sdkconfig.defaults` sets no
        `CONFIG_ESP_CONSOLE_UART_BAUDRATE` so this should be right, but a wrong baud
        yields plausible-looking mojibake rather than an error, and the classifier would
        then silently match nothing.
      * **The EN pulse boots the app, not the ROM loader.** `SerialConsole.reboot()`
        drives RTS high with DTR low. If the wiring inverts, the board lands in download
        mode and prints `waiting for download` forever.
      * **Release really releases.** After the button, `screen /dev/tty.usbserial-… 115200`
        must open. If it reports "Resource busy", `port.close()` is not being reached.
      Acceptance: all four confirmed on the Mac against an ESP32-DevKit v1 (bridge chip).
      Anything that fails comes back as a new S0 task with the observed behaviour.

- [x] **S0-test-3**: Someone who has not seen the code onboards a board unaided (P1, 0.5d) — passed 2026-09-22
      The criterion that actually decides *Unaided onboarding*; everything else is its
      parts. Not automatable, and deliberately written as a task rather than replaced by
      the parts a test runner can check. Its four component tasks (S0-fe-4 → S0-fe-7) have
      all landed — see `docs/features/enrollment.md`.
      Two runs, no assistance and no access to this repo, using only what is on screen:
      a board onboarded end-to-end, and a deliberately induced fault diagnosed. Induce
      at least two of: brownout (a thin USB cable through a hub reproduces it), a wrong
      Wi-Fi passphrase, a spent enrolment token.
      Acceptance: both runs succeed without the operator reading a UART log or asking a
      question. Anything they get stuck on comes back as a new S0 task with the observed
      behaviour — and the fact that they got stuck is the finding, not their skill.
      **Passed 2026-09-22.** Unaided onboarding confirmed on bench without operator
      reading UART logs or needing repo knowledge. R0's remaining gate is cleared.

- [ ] **S0-test-2**: The native-USB re-acquire path, on a C3/C6/S3 (P2, 0.25d)
      Split from S0-test-1 on 2026-09-11: the only board on hand is an ESP32-DevKit v1,
      whose bridge chip keeps the port alive across `hard_reset`. That exercises the
      *easy* half. The 8 s `getPorts()` poll in `serialConsole.ts` exists for the parts
      that come back as a **different** `SerialPort`, and nothing has ever tested it on
      metal — a too-short window shows "No board is available to watch" on a board that
      is merely rebooting, which is the exact false negative the console exists to
      remove. ~~**Blocked on acquiring a C3, C6 or S3.**~~
      **Unblocked 2026-09-22.** An **ESP32-S3** is on hand and has already enrolled against
      prod — device `94a990dd09a4`, the board that passed `R0-test-2` on 2026-09-19. This
      task's premise ("the only board on hand is an ESP32-DevKit v1") is simply out of
      date. Cheap to run now, since the board is already flashed and known-good.
      Acceptance: on the bench, `hard_reset` from the console on a native-USB board
      reconnects inside the window and streams the boot log without operator action.
      ⚠️ This entry and `S0-test-1` both say the bench is **the Mac**. The S3 enrolled from
      a **Windows + Chrome** bench with native USB on COM3. Confirm which host before
      running either — the re-acquire window is an OS-and-driver property, so a result on
      one host is not a result on the other.

---

## R0: Enroll a board (UI + recognition + flash + connect)

**Goal:** *I can register a board and see it online.* No code-deploy yet.
**Risk retired:** onboarding · board recognition · device↔server connection.
**Done when:** plug in a board, flash & register it from the browser, watch it come
online — no toolchain, no CLI.

**All 20 tasks are done**, and archived in
[docs/features/enrollment.md](docs/features/enrollment.md) (the flow end to end) and
[docs/features/infrastructure.md](docs/features/infrastructure.md) (the stack and the
prod hand-off).

**R0 closed on 2026-09-22.** `R0-test-2` proved the hardware path on 2026-09-19, and
`S0-test-3` passed unaided onboarding on 2026-09-22, clearing the release gate.

Two non-obvious rules from [design/production.md](design/production.md) that survive into
every later release: the **ingestor is the only MQTT subscriber** (N API workers would
otherwise ingest N times and split the SSE audience), and **agent images are built
off-box** (the ESP-IDF builder is 2–3 G against 5.5 G of free disk on `prod`).

### Test

- [x] **R0-test-2**: E2E on real hardware (P0, 1d) — passed 2026-09-19
      Flash → enroll → appears online in the dashboard. **This is R0's "Done when",
      restated as a task** — the release's whole risk is onboarding, and nothing had
      proven it on metal.
      **Passed on an ESP32-S3** (`94a990dd09a4`), flashed from `bingo.tvaroska.sk` with the
      v0.2.0 agent prod has served since v0.3.5. `device_progress` records the full ladder
      in 13 s — `link_up(wifi) → time_synced → enrolling → enrolled → mqtt_connected` —
      and `last_seen` runs 66 s past enrolment. The stated dependency on S0-fw-3 was
      dissolved rather than met: a different board removed the need for the stuck DevKit v1
      to recover. Verified against the prod DB 2026-09-22.
      Write-up in `docs/features/enrollment.md`.

---

## R1: Upload new code (OTA deploy)

**Goal:** *I can push new firmware to a registered board and watch its version change.*
**Risk retired:** the OTA transport works end-to-end.
**Done when:** a `.bin` uploaded from the dashboard reaches a device and the version it
reports afterwards is the one that was uploaded.

⚠️ **Not yet safe.** A broken build stays broken until R2 — there is no checksum gate
before apply, no A/B discipline beyond what the partition table already enforces, and no
confirm timer. Do not deploy anything to a board you cannot physically reach.

**What is already built, and must not be rebuilt here.** `fleetforge.storage` is the
`put`/`get`/`signed_url`/`delete` seam (`R0-be-6`), production reads it keylessly by
impersonation with `signBlob` measured rather than assumed (`S0-infra-5`), the
content-addressed key scheme is frozen (`storage/blobs.py`, `S0-infra-4`), and the
`artifacts` table exists and is empty (`alembic/versions/0003`). `firmware/publish.py`
already writes *blobs* — the agent bundles S0-infra-6 moved into the store. What R1 is
first to write is the **`artifacts` table**, and the user-facing half of the same
storage model.

Three constraints from documents that outrank this file:
[prd.md](spec/prd.md) → *Requirements & targets* caps an artifact at **1.9 MB** and a
healthy-link deploy at **5 min**; [spec/device-protocol.md](spec/device-protocol.md)
fixes the `dn/cmd` `stage` payload and the `up/status` state machine, and is near-frozen
(`CRITICAL.md`); `ota_slot_size` is **1966080** and is a three-way contract with
`agent/partitions.csv`.

**Seven of eight tasks are done and archived** in
[docs/features/ota-deploy.md](docs/features/ota-deploy.md) — the deploy orchestration
(`R1-be-2`), the public artifact endpoint (`R1-be-3`), the `deploy_events` writer
(`R1-be-4`), the agent's `esp_https_ota` handler and running-version accessor
(`R1-fw-1`, `R1-fw-2`) and the dashboard Deploy button (`R1-fe-1`). Reasoning is in
`DECISIONS.md` 2026-09-17. One remains, and it is bench-gated.

### Test

- [ ] **R1-test-1**: E2E: push firmware → board version changes in dashboard (P0, 1d)
      **Bench-gated, deliberately.** QEMU proves the transport; this proves the product.
      Kept as a hardware task rather than redefined to something the emulator can pass,
      for the same reason `R0-test-2` is: the release's claim is about a board.
      **Unblocked 2026-09-22.** The dependency was `R0-test-2` in practice — a board that
      cannot enroll cannot be deployed to — and that board is now enrolled and online.
      This is the cheapest P0 left: the target is already on the fleet, so the run is a
      deploy from the dashboard and a version check, and it closes R1.
      ⚠️ Re-read R1's "Not yet safe" warning above first: there is no checksum gate and no
      confirm timer until R2, so deploy only to a board you can physically reach — which,
      on a bench, is the point.

---

**Parallel spike (de-risks R2):** throwaway OTA + auto-rollback spike on real flaky
Wi-Fi. Tracked in [docs/features/ota-deploy.md](docs/features/ota-deploy.md).
Needs hardware — a spike about a flaky radio cannot run on an emulator that has none.

---

## R3: Thin OTA library — the four verbs in the user's own firmware

**Not started, and deliberately not next.** R3 sits behind R2 because a library is a
multiplier on however safe deploy currently is
(`DECISIONS.md` 2026-09-22, `design/decisions/ota-library-ships-after-safe-deploy.md`).
It is written down now because `prd.md` has promised it since the beginning and it had no
plan; nothing here should be picked up before R0 closes on metal and R2 lands.

**This file now carries three releases.** R0 is parked on hardware, R1 is one bench task
from done, and R3 is a plan rather than work in flight. If that becomes confusing, R3 is
the one to move back out to `docs/features/ota-library.md`.

Requirements: [spec/standards.md](spec/standards.md) → *ota-library*. Release contents:
[docs/releases.md](docs/releases.md) → R3. Feature file:
[docs/features/ota-library.md](docs/features/ota-library.md). Journey:
[spec/cujs.md](spec/cujs.md) → *CUJ-1*, written by `R3-spec-1` on 2026-09-22 — the
release's own subject, and the first thing in this project a CUJ has ever described.

### Firmware

- [ ] **R3-fw-2**: Extract the protocol into an ESP-IDF component (P1, 2d)
      `agent/main/` already separates protocol from demo app: `ff_ota`, `ff_mqtt`,
      `ff_enroll`, `ff_cfg`, `ff_store`, `ff_identity`, `ff_net`, `ff_time`. Move them to
      a component with an `idf_component.yml`; the agent becomes its first consumer and
      must keep passing `just agent-verify` and the QEMU E2E unchanged.
      The real work is deciding the **public** surface — whatever ships is additive-only
      from then on, exactly like the wire protocol. Keep it to the four verbs, enroll,
      announce/heartbeat, and a version accessor.
      Acceptance: the agent builds from the component with no behaviour change, the QEMU
      run in `docs/runbooks/agent-qemu.md` still passes, and the component's public
      headers are a strict subset of what `agent_main.c` uses.

- [ ] **R3-fw-3**: Arduino library wrapping the same C (P1, 2d)
      Depends on `R3-fw-2`. The persona writes Arduino or PlatformIO and does not use
      ESP-IDF (`docs/personas/PERSONAS.md` §1); if adopting Fleetforge means porting their
      project, they will not adopt it.
      Ships what `R3-fw-1` chose: layout **`ab-4m-arduino-v1`** as a sketch-local
      `partitions.csv`, config still in a flashable `ff_cfg`
      (`design/decisions/arduino-gets-its-own-layout-id.md`). Two measured constraints:
      the table must travel with the **example**, because the prebuild hook only reads the
      sketch folder and never a library directory; and the new layout id has to reach
      `device-protocol.md` + `SUPPORTED_LAYOUTS` first, which is a spec proposal, not this
      task. The safety posture needs no custom bootloader — the stock core is already
      `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y` — but it is still not optional: a
      configuration that cannot roll back must fail at build or enroll, not warn.
      Acceptance: a stock Arduino IDE install plus this library compiles the example for
      esp32 and esp32s3, and a board flashed from it enrolls.

- [ ] **R3-fw-4**: The worked example — enroll → heartbeat → stage → report version (P1, 1d)
      Small enough to read in one screen. The PRD's Morse-code blinker is the documented
      sample, so the example and the product claim are the same artifact: the blinker
      changes its message between two builds, which makes "the OTA worked" visible from
      across the room rather than only in the dashboard.
      Acceptance: builds unmodified from a clean checkout on both ESP-IDF and Arduino, and
      the README quickstart is exactly the steps a reader follows.

- [ ] **R3-fw-5**: Reject a wrong flash layout loudly (P1, 1d)
      A build that does not reproduce a supported layout exactly must announce a different
      `partition_layout`. The server accepts one today
      (`firmware/manifest.py::SUPPORTED_LAYOUTS`) and two once `ab-4m-arduino-v1` lands, so
      the deploy is rejected — but today the message does not tell a library user what to
      fix. **Do not use the IDE's `Maximum is N bytes` line as the check**: `R3-fw-1`
      measured it reading the board menu's `upload.maximum_size` rather than the built
      table, reporting 1310720 for a build whose slots were 1966080.
      Acceptance: a deliberately mismatched layout is refused at deploy time with a
      message naming the expected layout and slot size; no board is ever flashed into a
      state where the library is running without a rollback-capable bootloader.

### Test

- [ ] **R3-test-1**: E2E in QEMU — example firmware enrols, updates, rolls back (P1, 1d)
      The library's claim is the same as the agent's, so it gets the same proof:
      `docs/runbooks/agent-qemu.md` boots the real bundle against the dev stack, and the
      example must run that path rather than a stubbed one.
      Three runs: a clean enroll, an OTA to a second build whose visible behaviour
      differs, and a deliberately broken build that rolls back unaided and reports
      `rolled-back`.
      Acceptance: all three pass with no board, and the rollback run fails the test if the
      device reports `confirmed`.
