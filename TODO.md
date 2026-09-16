# Fleetforge — TODO

**Goal:** Self-hosted OTA firmware management for embedded fleets (ESP32 first) — a bad
build is caught before the fleet, and any device that gets one recovers itself.
**Updated:** 2026-09-15

## Where this stands

**R0 is built, deployed and live at `bingo.tvaroska.sk`.** Every desk-bound task in the
release is done. What is left is five tasks, and **all five need a physical board** —
R0's remaining risk is not code, it is that nothing has yet proven the product works on
metal. R0 cannot close until R0-test-2 passes.

The whole backlog is therefore one bench session. Its running order is:

1. **S0-fw-3** (`- [!]`, below) — flash v0.2.0 and see whether the board escapes the
   brownout loop. Everything else assumes a board that reaches the fleet.
2. **R0-test-2** — the P0 that closes R0.
3. **S0-test-1** — the four serial-console properties that only a USB bridge chip can prove.
4. **S0-test-3** — the unaided run, which decides *Unaided onboarding*.

S0-test-2 is not in that order: it needs a C3/C6/S3, and no such board is on hand.

**Prod now serves the v0.2.0 agent** (`d705652` — `-Os`, 80 MHz, max modem sleep, the
TX-power ladder), shipped in v0.3.5. That is step 1 of the S0-fw-3 bench order, so a board
flashed from `bingo.tvaroska.sk` now tests the untried lever *by default* rather than the
160 MHz `-Og` build that every recorded brownout came from. No special build is needed —
just a flash.

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
  artifact tasks are done; the build engine itself stays R9, only its cache key changed.

<!-- Counters: spec=1 infra=7 db=1 be=6 fe=7 sec=1 fw=4 test=3 -->
<!-- Sprint 0 counters: fe=7 fw=4 infra=7 test=3 -->

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
**Versions:** V1 = R0–R5 (safe OTA, ~5 boards) · V2 = R6–R10 (VCS + compile + simulation)
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
      _(attempted 2026-09-12; negative result recorded 2026-09-13; second lever staged
      2026-09-13; hardware cause ruled out 2026-09-14, awaiting bench)_

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

- [ ] **S0-test-3**: Someone who has not seen the code onboards a board unaided (P1, 0.5d)
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

- [ ] **S0-test-2**: The native-USB re-acquire path, on a C3/C6/S3 (P2, 0.25d)
      Split from S0-test-1 on 2026-09-11: the only board on hand is an ESP32-DevKit v1,
      whose bridge chip keeps the port alive across `hard_reset`. That exercises the
      *easy* half. The 8 s `getPorts()` poll in `serialConsole.ts` exists for the parts
      that come back as a **different** `SerialPort`, and nothing has ever tested it on
      metal — a too-short window shows "No board is available to watch" on a board that
      is merely rebooting, which is the exact false negative the console exists to
      remove. **Blocked on acquiring a C3, C6 or S3.**
      Acceptance: on the Mac, `hard_reset` from the console on a native-USB board
      reconnects inside the window and streams the boot log without operator action.

---

## R0: Enroll a board (UI + recognition + flash + connect)

**Goal:** *I can register a board and see it online.* No code-deploy yet.
**Risk retired:** onboarding · board recognition · device↔server connection.
**Done when:** plug in a board, flash & register it from the browser, watch it come
online — no toolchain, no CLI.

**19 of 20 tasks are done and archived** in
[docs/features/enrollment.md](docs/features/enrollment.md) (the flow end to end) and
[docs/features/infrastructure.md](docs/features/infrastructure.md) (the stack and the
prod hand-off). One remains, and it is the one that defines the release.

Two non-obvious rules from [design/production.md](design/production.md) that survive into
every later release: the **ingestor is the only MQTT subscriber** (N API workers would
otherwise ingest N times and split the SSE audience), and **agent images are built
off-box** (the ESP-IDF builder is 2–3 G against 5.5 G of free disk on `prod`).

### Test

- [ ] **R0-test-2**: E2E on real hardware (P0, 1d)
      Flash → enroll → appears online in the dashboard. **This is R0's "Done when",
      restated as a task** — the release's whole risk is onboarding, and nothing has yet
      proven it on metal. Depends in practice on S0-fw-3: the one board on hand cannot
      currently get past RF calibration to enroll at all.

---

**Parallel spike (de-risks R2):** throwaway OTA + auto-rollback spike on real flaky
Wi-Fi. Tracked in [docs/features/ota-deploy.md](docs/features/ota-deploy.md).
