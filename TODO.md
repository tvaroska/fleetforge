# Fleetforge — TODO

**Goal:** Self-hosted OTA firmware management for embedded fleets (ESP32 first) — a bad
build is caught before the fleet, and any device that gets one recovers itself.
**Updated:** 2026-09-14
**Focus:** R0 is deployed and live at `bingo.tvaroska.sk`; only R0-test-2 (E2E on real
hardware) remains. Device visibility is now covered from both ends — the serial console
(S0-fe-1) and boot/enrol stage reports (S0-fw-1, verified on an emulated board
2026-09-11) — and the arrivals list no longer duplicates offline boards (S0-fe-3).
**Bench session 2026-09-11**: v0.3.0 cut and deployed so the board is flashed with the
stage-reporting agent against a prod that actually serves `/v1/device-progress`.
S0-test-1 and R0-test-2 both run on the Mac against an ESP32-DevKit v1 (the Linux dev
box does not enumerate boards over WebSerial). Two things the board cannot cover are
split out: S0-test-2 (native-USB re-acquire, needs a C3/C6/S3) and S0-infra-2 (the
three stale agent bundles shipped in v0.3.0). The next desk-bound work after the bench
is the GCS blocker in `docs/runbooks/artifact-storage.md` — unfiled, and a prerequisite
for R1.

The bench itself found no server-side bug and three onboarding bugs. The board never
enrolled: it brownouts during Wi-Fi PHY calibration and resets, forever. **That fault is
ours, not the supply's** — settled 2026-09-14, see S0-fw-3 — but **the product could not
say so either way**, and diagnosing it took a serial log pasted by hand into a chat.
`S0-fe-4` is the P0 that came out of that, and it gates R0: a board that cannot be
onboarded without an engineer reading raw UART is not onboarded.

**2026-09-14, ESPHome comparison.** A *brand-new* board — same cable, same port, nothing
cached on either side — was flashed with ESPHome, associated to Wi-Fi and ran. That is
the experiment S0-fw-3 asked for, and it comes back on the "ours to fix" branch: this
rail carries a cold full RF calibration. Two consequences, both filed: S0-fw-3 is
reopened against a firmware cause rather than closed as a hardware one, and S0-fw-4
records a second, independent defect found while checking it — the flasher erases the
cached calibration on **every** flash, because the calibration is in `nvs`, not in
`phy_init`. A broader review of ESPHome — what to reuse, what to copy, what to refuse —
is in `products/docs/esphome-review.md`.

**2026-09-14, artifacts.** A review of how images are stored and versioned
(`design/artifacts.md`, DECISIONS.md same date) found the product running two firmware
distribution paths with different rules, and no identifier anywhere that says which
*build* produced a bundle — the gap that cost S0-fw-3 three sessions. It also found that
`design/decisions/infrastructure-agent-bundles-are-artifacts.md` has been Accepted since
2026-09-11 with nothing in this file pointing at it. Five tasks filed. **S0-infra-3**
(build identity in the manifest) **landed 2026-09-14** — every bundle now carries
`config_sha256` and `build_digest`, and the diagnostic bundle prints both, so step 2 of
the S0-fw-3 bench order (an `sdkconfig` diff against ESPHome's) is answerable from an
artifact. **S0-infra-4** (the content-addressed key scheme) **landed 2026-09-14** — the
layout is frozen in `src/fleetforge/storage/blobs.py` and the `artifacts`/`builds` tables
exist (migration `0003`), empty and unread, while no object has yet been written. The
remaining three:
**S0-infra-5** (a GCS credential that is not a key file — the actual blocker),
**S0-infra-6** (bundles served from the store, image ships none) and **S0-infra-7**
(catalog keyed by target *and* layout). The build engine itself stays R9; only its cache
key changed.

Designed up on 2026-09-11 as one feature — **Unaided onboarding: flash → on the fleet**
(`docs/features/enrollment.md`, requirements in `spec/standards.md`, reasoning in
`design/decisions/enrollment-console-is-the-diagnostic-surface.md`). Its tasks are
S0-fe-4 → S0-fe-5 → S0-fe-6 → S0-fe-7, closed by S0-test-3. Do them in that order:
nothing can be acted on before it is named, and nothing can be judged before a person
who has not seen the code tries it. **S0-fe-4 landed 2026-09-11** — the panel now names
tagless faults (brownout, panic, ROM reset reasons), keeps its milestone claims per-boot,
surfaces a reset loop, and puts a deadline on every milestone. **S0-fe-5 and S0-fe-6
landed the same day** — the console now resets the board itself so the boot is never
missed, and where a fault's remedy is software the panel offers it as one button
(a spent token is re-flashed in a click) rather than as an instruction. **S0-fe-7 landed
the same day** — one click copies a redacted diagnostic bundle, so a stuck operator can
hand the whole session to someone who can help. S0-test-3, the unaided run, is next.

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

- [x] **S0-fe-5**: The console misses the boot it exists to show (P1, 0.5d)
      Filed 2026-09-11, same session. `flash.ts` ends with `hard_reset` and releases the
      port; the console then attaches as a second session — by which time the entire boot
      log has been printed to nobody. The panel sat with zero events and no log box at
      all (the `<pre>` only renders when `events.length > 0`), which reads identically to
      a dead port. Pressing **Reboot the board** produced the log instantly, but nothing
      on screen suggests that.
      Fix: after the console acquires the port, pulse EN itself so the operator always
      sees a boot from the top; and render the log region with a heading and a *"waiting
      for the first line from the board…"* placeholder rather than nothing.
      Acceptance: flash a board and the boot log appears without the operator touching
      anything. Empty state is visibly an empty log, not an absent one.
      _(done 2026-09-11; see docs/features/enrollment.md)_

- [x] **S0-fe-6**: Recover in place — the fix is a button, not a instruction (P1, 1d)
      Part of *Unaided onboarding* (`docs/features/enrollment.md`, `spec/standards.md`).
      Depends on S0-fe-4: a fault must be named before it can be acted on. Where a
      fault's remedy is software rather than physical, the panel offers it directly —
      retry enrol, re-flash, mint a fresh token, reboot — and picks which applies from
      the fault, because the target operator does not know. `enroll 409` (token spent)
      is the clearest case: today it tells the operator to "flash the board again to
      mint a fresh one", which is three manual steps it could take itself.
      Acceptance: for each software-remediable fault in `hintFor`, the panel renders the
      matching action and the action resolves the fault on a real board. Faults with no
      software remedy — brownout, wrong PSK — must render *no* button rather than a
      button that cannot work.
      _(done 2026-09-11; see docs/features/enrollment.md)_

- [x] **S0-fe-7**: One click produces a diagnostic bundle (P1, 0.5d)
      Part of *Unaided onboarding*. The escalation path that did not exist on
      2026-09-11, when the only way to get the log out of the panel was to select text
      in an unlabelled `<pre>` — and the operator could not find it. Copies the full
      console log, the loaded `ff_cfg` summary, chip and flash identification, agent and
      server versions, and the current fault.
      **Secrets must be redacted**: the enrolment token, the Wi-Fi passphrase and the
      MQTT credentials. `ff-cfg` already declines to print the last two (`secrets token
      80 chars, passphrase 8 chars (never printed)`), but the bundle must not depend on
      the firmware's discretion for that.
      Acceptance: the bundle contains the brownout log from the 2026-09-11 session and
      identifies the fault; a test asserts no `ffe_` token, passphrase or MQTT password
      appears in the output, vacuity-checked by removing the redaction.
      _(done 2026-09-11; see docs/features/enrollment.md)_

- [ ] **S0-test-3**: Someone who has not seen the code onboards a board unaided (P1, 0.5d)
      The criterion that actually decides *Unaided onboarding*; everything else is its
      parts. Not automatable, and deliberately written as a task rather than replaced by
      the parts a test runner can check.
      Two runs, no assistance and no access to this repo, using only what is on screen:
      a board onboarded end-to-end, and a deliberately induced fault diagnosed. Induce
      at least two of: brownout (a thin USB cable through a hub reproduces it), a wrong
      Wi-Fi passphrase, a spent enrolment token.
      Acceptance: both runs succeed without the operator reading a UART log or asking a
      question. Anything they get stuck on comes back as a new S0 task with the observed
      behaviour — and the fact that they got stuck is the finding, not their skill.

- [x] **S0-fw-2**: `link_up` is reported before the clock is set, so it can never arrive (P2, 0.25d)
      Found 2026-09-11 reading `agent_main.c`: line 189 reports `link_up`, line 194 runs
      `ff_time_sync`. Against an `https://` api_base the POST happens at epoch 0, so TLS
      certificate validation rejects it and the stage is lost. Only harmless in a
      plaintext lab, which is where it was tested. The server-side arrivals list can
      therefore never show the first stage a board reports.
      Fix: report it after the clock is set, or buffer pre-clock stages and flush once
      TLS is usable — the second preserves the ordering the table is for.
      Acceptance: a board against prod produces a `link_up` row in `device_progress`.
      _(done 2026-09-11; see docs/features/enrollment.md)_

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
         has already been written.
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

- [x] **S0-fw-4**: The flasher erases the cached RF calibration on every flash (P1, 0.5d)
      Found 2026-09-14 while checking S0-fw-3. The 2026-09-13 change that replaced
      esptool-js's `eraseAll` with a targeted wipe **preserved the wrong partition.**
      `flash.ts::nvsWipe` states the premise it was built on — *"destroyed the `phy_init`
      partition where the RF calibration is cached"* — and RF calibration is not in
      `phy_init`. It is in **NVS**, under IDF's `phy` namespace, which `nvsWipe` fills
      with 0xFF on every flash. Four confirmations:
      * `agent/dist/esp32/sdkconfig.resolved` has `CONFIG_ESP_PHY_CALIBRATION_AND_DATA_STORAGE=y`
        (calibration → NVS) and `# CONFIG_ESP_PHY_INIT_DATA_IN_PARTITION is not set`, so
        the `phy_init` partition at `0x11000` **holds nothing at all** in this build — the
        init data is compiled into DROM.
      * ESP-IDF's RF-calibration guide lists "NVS does not exist" / "has been erased"
        among the triggers for full calibration, and ships
        `esp_phy_erase_cal_data_in_nvs()` documented as clearing *only* the PHY namespace
        rather than all of NVS — the exact inverse of what the flasher does.
      * The error in every bundle, `failed to load RF calibration data (0x1102)`, is
        `ESP_ERR_NVS_NOT_FOUND` (`ESP_ERR_NVS_BASE` = `0x1100`). The log line already said
        "NVS key not found".
      * S0-fw-3's own closing note above says the result is "cached in NVS". The two
        statements have been contradicting each other since 2026-09-13.
      Consequence: **no fleetforge-flashed board can ever retain a calibration.** Every
      one takes the maximum-current cold path on every boot after every flash, forever.
      Independent of S0-fw-3 — it does not explain the brand-new board, which had nothing
      cached either way — but it is what would throw away that board's recovery the moment
      it is reflashed.
      Fix: drop `nvsWipe` from the flasher and move credential invalidation into the
      agent, which can act per namespace where a flasher writing raw bytes cannot.
      `ff_store` already persists `api_base` alongside the credential; add a fingerprint
      of the `ff_cfg` enrolment token and, when it differs from the stored one, erase
      `FF_STORE_NAMESPACE` only. IDF's `phy` namespace survives. This also fixes the same
      problem for boards reflashed in the field, which a browser flasher cannot reach.
      No partition-table change, so nothing here is a flash-time immutable.
      Also correct the three comments built on the wrong premise —
      `frontend/src/flash.ts` (`nvsWipe` and the `wipeNvs` option),
      `frontend/src/esptoolFlasher.ts::write`, `frontend/src/partitionTable.ts` — and the
      two `DECISIONS.md` entries superseded on 2026-09-14.
      Acceptance: a board that has completed one calibration still logs a successful
      `esp_phy_load_cal_data_from_nvs` after a reflash (no `0x1102`), while a reflash
      carrying a *new* enrolment token still spends that token rather than reusing the
      stored credential. Both halves tested — the second is what `nvsWipe` existed for.
      _(done 2026-09-14; reviewed; see docs/features/enrollment.md and
      docs/runbooks/agent-qemu.md. `nvsWipe`/`wipeNvs`/the checkbox are gone;
      `planWrite` now asserts no part lands in `nvs` (`assertLeavesNvsAlone`, sector-rounded,
      offset read from the downloaded table) and a new test doctors a build so `ff_cfg`
      lands at 0x9000 and proves it throws. Device side: `ff_store_sync_token()` in
      `agent/main/ff_store.{c,h}`, called from `agent_main.c` after `ff_cfg_log`, stores
      `tok_fp` = first 8 bytes of sha256(token) as hex and erases ONLY the `ff` namespace
      when it differs; absent ⇒ adopt, never erase (an OTA at R2 would otherwise brick the
      fleet); tokenless config ⇒ no-op. T1: frontend typecheck + 203 vitest, ruff + mypy +
      650 pytest, `just agent-build esp32` clean under -Werror, `agent-verify` OK, all four
      size budgets ratcheted to measured bytes. T2 in QEMU against `just up`: boot 1 adopt +
      `enroll 200`; boot 2 same token → `reusing the stored credential`, no HTTP; boot 3 after
      `just agent-qemu-recfg` with a new token → loud erase line, `enroll 200`, MQTT online,
      both tokens `used` in the DB; `nvs_tool.py` still shows `phy/cal_data =
      ff-s0-fw-4-canary` beside the new credential. Bench confirmation on real hardware still
      owed, jointly with S0-fw-3.)_

- [x] **S0-infra-4**: Freeze the content-addressed key scheme before R1 writes an object (P1, 0.5d)
      Filed 2026-09-14. `storage/objectstore.py::put` already justifies overwrite-is-safe
      with *"R1-BE-1 content-addresses artifacts by sha256"*, and no `R1-` task exists
      yet — so the scheme is asserted in a docstring and implemented nowhere. Free to
      settle now, a migration over live objects later.
      Scope is deliberately narrow: name the layout (`fleetforge/blobs/sha256/<hex>`,
      write-once, `Cache-Control: immutable`), the `artifacts` and `builds` tables that
      hold what the bytes cannot, and the rule that a manifest is generated from digests
      rather than stored. No upload endpoint, no device-facing change.
      `spec/` is protected: **PROPOSE** the wire-visible half (artifact identity in
      `spec/device-protocol.md`, if it is wire-visible at all) rather than editing it.
      The prefix rule is untouched and non-negotiable — `resolve_key` rejects rather than
      normalises, because `gs://btvaroska` also holds `secrets/`.
      Acceptance: the scheme is written down in `design/artifacts.md` and the migration
      creating `artifacts`/`builds` is reviewed and merged, with a test that a blob key
      outside `fleetforge/blobs/` is refused.
      _(done 2026-09-14; reviewed; see docs/features/infrastructure.md, and the as-built
      record in design/artifacts.md + DECISIONS.md 2026-09-14 — `storage/blobs.py`
      holds the frozen scheme, migration `0003` the two empty tables. The key handed to
      `ObjectStore` is store-relative (`blobs/sha256/<hex>`); the `fleetforge/` half is the
      store's prefix, so a `fleetforge/`-prefixed key is refused. `spec/` was proposed
      against, not edited.)_

- [!] **S0-infra-5**: `storage/factory.py` accepts a credential that is not a key file (P1, 1d) _(⚠ attempted 2026-09-14, twice; NOT a code failure — both runs were halted
      before writing anything by a safety classifier refusing the IAM grant in AC0
      (`gcloud iam service-accounts add-iam-policy-binding … --role=roles/iam.serviceAccountTokenCreator`).
      The grant must be run by the owner; the plan survives at
      `.claude/plans/S0-infra-5-gcs-credential-that-is-not-a-key-file.md`. Re-run once
      `fleetforge-artifacts@btvaroska` has a tokenCreator binding for the calling identity.)_
      Filed 2026-09-14. Named as the real prerequisite in
      `design/decisions/infrastructure-agent-bundles-are-artifacts.md` (amended
      2026-09-11): the GCS blocker in `docs/runbooks/artifact-storage.md` is
      `constraints/iam.disableServiceAccountKeyCreation`, and `_gcs_store` raises unless
      `GCS_CREDENTIALS_FILE` names an existing file. So the blocker is not an org-policy
      exemption — it is a factory that only knows one credential shape. This is ordinary
      backend work and it unblocks both S0-infra-6 and R1.
      Add service-account **impersonation** (`impersonated_credentials` over the VM's
      attached identity, target `fleetforge-artifacts`). Impersonation is required for
      **containment** independently of signing: prod's attached identity is
      `mainsite@sites-470716`, the shared VM account for the whole estate, which can read
      `secrets/`, `podcasts/` and `backup/` in the same bucket.
      **Plain ADC must not be accepted as a fallback.** `_gcs_store`'s docstring already
      says why and it stays true: ADC carries no private key, so V4 signing silently
      becomes a `signBlob` round trip, and it picks up the wrong service account. Both
      failures first appear on a real device. Refuse at construction, as now.
      Verify `signBlob` actually works under impersonation — the ADR calls it "expected
      but not yet verified", and R1 depends on it where this task does not.
      Acceptance: prod reads a `fleetforge/`-prefixed object with no key file on disk and
      is refused an object under `secrets/`; `GCS_CREDENTIALS_FILE` still works unchanged;
      ambiguous configuration still raises rather than resolving. Record the `signBlob`
      result either way — a negative is what R1 needs to know.

- [ ] **S0-infra-6**: Agent bundles are served from the store, not baked into the image (P1, 2d)
      The unimplemented half of
      `design/decisions/infrastructure-agent-bundles-are-artifacts.md` (Accepted
      2026-09-11), finally filed. Depends on S0-infra-5 for the credential and S0-infra-4
      for the key scheme.
      **The application image ships zero agent bundles** — `Dockerfile:68`'s
      `COPY agent/dist /app/agent` goes away, and publishing a bundle stops requiring a
      redeploy. The ADR considered and rejected keeping a baked set as fallback: it
      preserves the defect rather than mitigating it, the image still grows with every
      new target, and "which tier answered?" becomes a new thing to diagnose during the
      one flow four tasks have just been spent making self-explanatory.
      Two things this buys beyond tidiness: **agent-version rollback**, which the S0-fw-3
      bench would use today (getting `19b0a0b` back currently means rebuild + redeploy),
      and making `S0-infra-2`-class staleness structurally impossible rather than a
      matter of remembering `just agent-build-all`.
      Verification moves with the bundles, it is not dropped: per-part sha256, plus
      `partition_layout`/`ota_slot_size` agreement with `spec/device-protocol.md`. Keep
      the current serving shape — the API reads from the store and streams through the
      authenticated `GET /v1/agent/{target}/{part}`, so no signed URL and no private key
      enters the onboarding path. Re-point `S0-infra-2`'s `just agent-check-fresh` guard
      from the image build to the publish step; that is a move, not a rewrite.
      An unreachable store must present as a **named fault** in the flasher, held to the
      *Unaided onboarding* standard: say why, in plain language, and do not offer a
      manifest that cannot be honoured.
      Acceptance: an image built with no `agent/dist` present flashes a board end to end;
      publishing a new bundle changes what the flasher offers with no redeploy; a
      previous agent version is still flashable; with the store unreachable the console
      names the fault instead of failing mid-write.

- [x] **S0-infra-7**: The firmware catalog is keyed by target alone (P2, 0.25d)
      Filed 2026-09-14. `firmware/catalog.py` indexes bundles by directory name, which is
      the chip target, while `partition_layout` lives inside the manifest where nothing
      can select on it. One layout exists (`ab-4m-v1`, frozen at R0) so nothing is broken
      today; the moment a second appears, two bundles for one target cannot coexist and
      the flasher has no way to ask for the right one.
      Fix: key on `(target, partition_layout)` and have the manifest endpoint expose the
      layout so the caller selects rather than guesses. A one-line change now against a
      migration and a wire change later. Land it with or before S0-infra-6, whose store
      keys should not encode the narrower assumption.
      Acceptance: two bundles for the same target with different layouts both load and
      are separately addressable; a request with no layout still resolves while exactly
      one exists, and names the ambiguity when more than one does.
      _(done 2026-09-14; see docs/features/infrastructure.md)_

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

- [x] **S0-infra-2**: Rebuild the c3/c6/s3 agent bundles before they ship again (P2, 0.5d)
      Found 2026-09-11 while preparing the v0.3.0 release. `Dockerfile:68` bakes
      `agent/dist` into the app image, and only `agent/dist/esp32` contains the S0-fw-1
      stage reporter — `esp32c3`, `esp32c6` and `esp32s3` predate it. Verified by
      grepping the built `app.bin` for `/v1/device-progress`, not by mtime.
      Shipped knowingly in v0.3.0 (owner's call: the only board on hand is an esp32, so
      nothing can flash the stale ones today), but prod's flasher will silently write a
      no-stage-reporting agent to any native-USB board until this lands — invisible
      exactly the way S0-fw-1 exists to prevent.
      Fix is `just agent-build-all` plus a release-time guard so a stale bundle cannot
      reach an image again; the guard is the real deliverable, since the rebuild is a
      one-liner that was simply forgotten.
      Acceptance: all four bundles contain `/v1/device-progress`; a check in the build
      path fails when any bundle is older than `agent/main/`, vacuity-checked by
      reverting one bundle.
      _(done 2026-09-11; see docs/features/infrastructure.md — all four targets rebuilt,
      esp32 was also stale (missing S0-fw-2); `just agent-check-fresh` gates `just build`;
      bundles remain gitignored so fresh firmware reaches prod only at next release)_

---

## R0: Enroll a board (UI + recognition + flash + connect)

**Goal:** *I can register a board and see it online.* No code-deploy yet.
**Risk retired:** onboarding · board recognition · device↔server connection.
**Done when:** plug in a board, flash & register it from the browser, watch it come
online — no toolchain, no CLI.

Decisions & guardrails: [docs/features/enrollment.md](docs/features/enrollment.md).
Targets these tasks must hit (intervals, timeouts, limits):
[spec/prd.md](spec/prd.md) → *Requirements & targets*.

Two non-obvious rules from [design/production.md](design/production.md): the **ingestor is
the only MQTT subscriber** (N API workers would otherwise ingest N times and split the SSE
audience), and **agent images are built off-box** (the ESP-IDF builder is 2–3 G against
5.5 G of free disk on `prod`).

### Protocol

- [x] **R0-spec-1**: Device protocol v1 — topic tree, QoS/retain, payload schemas, state machine (P0, 0.5d) ✅ 2026-09-08
      [spec/device-protocol.md](spec/device-protocol.md). Near-frozen: an R0 agent speaks
      this until someone physically retrieves the board.

### Infrastructure

- [x] **R0-infra-0**: Retire bingo (P0, 0.5d) ✅ 2026-09-08
      `services` repo: drop `bingo` + `bingo-frontend` and the route, remove `bingo.env`
      and `BINGO_PASSWORD`, drop from the deploy registry in `scripts/deploy.sh`.
      **Back up the bingo database before dropping the role** — the one irreversible step.
      Frees 384 M of declared limits and `bingo.tvaroska.sk`.
      Done: both containers stopped and removed (prod 12 → 10 containers); `bingo_db`
      dumped to `gs://btvaroska/retired/bingo/` (verified `pg_restore -l`, 95 TOC
      entries) then `bingo_db` + `bingo_user` dropped; compose blocks, Traefik router,
      `prod/bingo.env`, postgres init lines, deploy/validate registry entries and the
      bingo smoke endpoint all removed; prod memory 1913 → 1742 MB used, swap 1038 →
      783 MB (baseline for R0-infra-4); `bingo.tvaroska.sk` now returns 404 from
      Traefik's default backend and is free for R0-infra-3. Follow-ups: boris dashboard
      tile edited locally but image not rebuilt (Phase 4 deferred); `BINGO_PASSWORD`
      left in `services/prod/.env` (dead credential, owner's call).
      _(done 2026-09-08; reviewed; see docs/features/* — record lives in
      [design/production.md](design/production.md) → *Retiring bingo* and
      `services/DECISIONS.md` 2026-09-08)_

- [x] **R0-infra-1**: Standalone Compose stack (P0, 2d) ✅ 2026-09-08
      Dev loop *and* the V2 self-host promise: Traefik + Postgres + MinIO + Mosquitto +
      api + ingestor + frontend. API and dashboard on **one origin** (no CORS).
      This is the dev environment by design, so it cannot rot.
      Done: seven services up from wiped volumes; migrations run from the api
      entrypoint (`RUN_MIGRATIONS=true`); `/v1/healthz` + `/v1/readyz` served through
      nginx with no CORS header, no host port and no Traefik router for the api; MQTT
      publish through Traefik's `mqtt` entrypoint reaching the ingestor's
      `ff/v1/d/+/up/#` subscription; broker persistence and ingestor reconnect verified
      across a broker restart; MinIO bucket idempotent; `just up-prod` (production
      shape, non-root image) reproduces all of it. Dev-only anonymous broker access is
      quarantined in `mosquitto/conf.d/10-dev-anonymous.conf` for `R0-sec-1` to delete.
      Note: the mosquitto CLI clients force TLS on port 8883, so use `just mqtt-pub` /
      `just mqtt-sub` (paho) against the dev broker.
      _(done 2026-09-08; reviewed; see docs/features/infrastructure.md)_

- [x] **R0-infra-2**: Agent firmware build pipeline (P0, 1d) ✅ 2026-09-09
      Pinned ESP-IDF, per-target images, built **off-box** and pushed to Artifact
      Registry; binaries served by the API for the Web Serial flasher.
      `agent/` is now a real ESP-IDF project built inside
      `espressif/idf:v5.5.5` **pinned by digest**, one flashable bundle per target
      (`esp32`, `esp32s3`, `esp32c3`, `esp32c6`) via `just agent-build{,-all}`.
      The flash-time immutables are frozen: `ab-4m-v1` in `agent/partitions.csv`
      (`ota_0`/`ota_1` = `0x1E0000` = the `ota_slot_size` 1966080 that
      `spec/device-protocol.md` promises), no `factory`, `ff_cfg` reserved at
      `0x12000` for the flasher's per-board config, rollback on and every eFuse
      burn off. Offsets are read from ESP-IDF's `flasher_args.json`, never typed.
      `src/fleetforge/firmware/` sha256-verifies every bundle once at startup and
      `GET /v1/agent/manifest` + `/v1/agent/{target}/{part}` serve them behind the
      admin credential; `COPY agent/dist /app/agent` bakes them into the app image.
      _(done 2026-09-09; reviewed; see docs/features/infrastructure.md)_

- [x] **R0-infra-3**: Prod ingress — `services` repo (P0, 1d) ✅ 2026-09-09
      Traefik `mqtt` entrypoint on 8883 + TCP router (``HostSNI(`bingo.tvaroska.sk`)`` →
      `mosquitto:1883`), GCP firewall rule, and the prod service fragment.
      First non-HTTP port in this stack; touches shared ingress.
      **Built and deployed 2026-09-09; ONE step outstanding.** On prod: Traefik
      listens on 8883, terminates TLS with the Let's Encrypt cert for
      `bingo.tvaroska.sk` (`Verification: OK`) and forwards to `mosquitto:1883`;
      the broker is healthy with its dynsec store bootstrapped; an authenticated
      `$SYS/broker/uptime` subscribe through the full TLS path returned a value and
      an anonymous one was refused `not authorised`. All three existing apps still
      200. Prod credentials use non-hex usernames (`ff-admin`, `ff-ingestor`) per
      the R0-sec-1 reviewer note above.
      _(done 2026-09-09. The firewall rule that blocked this — `sites-allow-mqtt`,
      tcp:8883 → tag `https-server` on network `sites` — was created by the owner
      from Cloud Shell, since `devserver@btvaroska` has no `compute.firewalls.*` on
      `sites-470716`. Verified from off-box: TLSv1.3 with `Verification: OK`, an
      authenticated `$SYS/broker/uptime` subscribe returned a value, anonymous and
      wrong-password both `Not authorized`; the other three apps unaffected. Gotcha
      carried forward: the project has no `default` network, so
      `firewall-rules create` needs `--network=sites`. The door had nothing behind
      it until R0-infra-5 landed the app fragment on 2026-09-10.
      See docs/features/infrastructure.md → *Production MQTT ingress*.)_

- [x] **R0-infra-5**: App image pipeline + prod app fragment (P0, 1d) ✅ 2026-09-10
      R0-infra-3 shipped the broker and the 8883 door; nothing is behind it. No
      fleetforge **app** images exist in Artifact Registry, and no task owned that
      gap — `R0-infra-2` is the *firmware* pipeline, a different artifact.
      Two images (`fleetforge`, serving both api and ingestor, and
      `fleetforge-frontend`), pushed with `just build`; then the prod fragment:
      a `fleetforge` database + role, the api/ingestor/frontend services, and an
      HTTPS router so `bingo.tvaroska.sk` stops returning 404.
      **The ingestor must never be rolled out.** `docker rollout` runs two copies
      during the swap and the ingestor is the sole MQTT subscriber
      (design/production.md → *The single-subscriber rule*) — two would double
      every telemetry row. It belongs in `INFRA_SERVICES`, recreated in place.
      Acceptance: images pullable by digest on prod; `https://bingo.tvaroska.sk`
      serves the SPA; login with the prod admin password succeeds; a simulated
      board enrolls through the public API, connects over `mqtts://…:8883` and
      appears in the dashboard; the other three apps still 200.
      _(done 2026-09-10. Pipeline half shipped 2026-09-09; the prod fragment
      landed once the owner granted the `services/prod/**` and `services/scripts/**`
      edit rules. fleetforge is LIVE at `https://bingo.tvaroska.sk`. See
      docs/features/infrastructure.md → *The app on prod*, DECISIONS.md 2026-09-10.)_

- [x] **R0-infra-4**: Capacity check on `prod` before E2E (P0, 0.5d) ✅ 2026-09-10
      `scripts/capacity_snapshot.py` — stdlib-only, piped to prod over ssh.
      **Verdict: no resize needed.** 151 MiB measured footprint (dev box, production
      shape: `just up-prod`) fits in the 384 MiB bingo freed. Declared over-commit
      99.5% (3904 / 3924 MiB MemTotal) is normal — measured peaks matter, and the
      net add is negative. Swap *used* is a stock not a flow; `memory.events max` is
      the real under-provisioning signal. Follow-up: re-run `just capacity-check-prod`
      after R0-infra-5 lands to confirm the projection against live measurements.
      _(done 2026-09-10; see docs/features/infrastructure.md,
      docs/runbooks/capacity.md, design/production.md → Capacity, DECISIONS.md
      2026-09-10)_

### Backend

- [x] **R0-db-1**: Schema (P0, 0.5d) ✅ 2026-09-08
      devices (+ `link_type`, `power_class`, `parent_device_id`, `partition_layout`,
      `ota_slot_size`), groups, enrollment tokens, admin tokens, and **`deploy_events`**
      (KPI-ready from R1 — R5 needs the history, not just the table).
      _(done 2026-09-08; reviewed; see docs/features/*)_

- [x] **R0-be-1**: API scaffold + admin auth (P0, 2d)
      Public, versioned (`/v1`). Opaque bearer tokens hashed at rest (argon2id);
      `/v1/auth/login` returns the same token in an HttpOnly/Secure/SameSite=Strict
      cookie. `scopes`/`subject` columns reserved, always `admin` in v1.
      _(done 2026-09-08; reviewed; see docs/features/*)_

- [x] **R0-be-2**: Enrolment tokens (P0, 0.5d)
      Short-lived (24 h), group-scoped, revocable, **single-use**.
      `POST/GET /v1/enrollment-tokens` + `POST …/{id}/revoke`; the burn ships as the
      single importable `auth.enrollment.BURN_SQL` that R0-be-4 will call.
      _(done 2026-09-08; reviewed; see docs/features/*)_

- [x] **R0-be-3**: Ingestor process (P0, 1.5d)
      Single instance, **sole MQTT subscriber**. announce / presence / heartbeat →
      derived presence (LWT for always_on, `last_seen` for sleepy) → Postgres + `NOTIFY`.
      _(done 2026-09-08; see docs/features/*)_

- [x] **R0-be-4**: `POST /v1/enroll` over HTTPS, not MQTT (P0, 1.5d)
      Validate token → registry entry → provision broker credential (dynsec) → burn
      token. Keeps the broker from ever authenticating a client it has not heard of.
      _(done 2026-09-08; reviewed; see docs/features/*)_

- [x] **R0-be-5**: SSE event stream (P0, 0.5d)
      Fanned out from Postgres `LISTEN`, so it works with N API workers.
      `GET /v1/events` (SSE) + `GET /v1/devices` (the read model the stream tells every
      client to re-read; presence derived on read, never stored). One dedicated asyncpg
      `LISTEN` connection per API process (`application_name='fleetforge-events'`) into a
      per-app `EventHub`; a slow client and a listener reconnect both **end the stream**
      rather than degrade it, and the client resyncs. Payload validated then forwarded
      verbatim, `\r`/`\n` refused — a board's `fw_version` must not be able to forge a
      frame. Auth once at connect, so the stream is capped at 15 min
      (`sse_max_stream_s`); no token in the query string.
      _(done 2026-09-08; reviewed; verified through nginx on `just up-prod`: event on the
      stream ~0.2 s after publish, keepalives every 15 s, fan-out to two clients, both
      401 paths, `docker compose restart postgres` → every stream ended, listener
      reconnected and events flowed again with the api never restarting and `/v1/healthz`
      200 throughout, retained replay still did not move `last_seen`; 209 tests green.
      Gotcha carried forward: httpx's `ASGITransport` buffers, so SSE tests drive the ASGI
      app directly. See docs/features/*)_

- [x] **R0-be-6**: Object-store adapter (P0, 1d)
      `put` / `get` / `signed_url` / `delete`. **GCS** backend with a service-account key
      scoped to `gs://btvaroska/fleetforge/`; **MinIO** behind the same interface for the
      dev stack and V2 self-hosting. One `ObjectStore` Protocol, two adapters
      (`storage/s3.py`, `storage/gcs.py`), selection in `storage/factory.py`,
      `ObjectStoreDep` in `api/deps.py` (no R0 route uses it — R1 does), and
      `just storage-check` (`python -m fleetforge.storage selftest`) as both the T2
      harness and the ops answer to "can this container reach the artifact store?".
      Nothing in `spec/` touched; two proposals (signed-URL TTL 30 min, `get()` cap
      8 MiB) recorded in `DECISIONS.md` instead.
      _(done 2026-09-08; reviewed; verified: 289 tests green with
      `tests/test_object_store_minio.py` **running**, not skipped, plus ruff/format/mypy
      and `just stack-check` clean; `just storage-check` on the host → put / get
      (sha256 match) / signed URL fetched over HTTP / delete / `ObjectNotFound` /
      idempotent second delete, `SELFTEST OK`; `--ttl 5 --keep` URL → 200 then 403
      `Request has expired` after 7 s; `../escape.bin`, `/abs.bin`, `a/../../b.bin`,
      `a//b.bin` all rejected before any backend call and `ls -R /data` inside MinIO
      showed nothing outside the bucket prefix; inside the api container the selftest
      prints a URL signed against `localhost:9000` (not `minio:9000`) that returns 200 /
      4096 bytes when curled from the host. `fleetforge:dev` 320 MB after the two SDKs.
      Gotchas carried forward: (1) a presigned URL signs the Host header, hence the
      `S3_ENDPOINT_URL` / `S3_PUBLIC_ENDPOINT_URL` split — the container selftest cannot
      fetch the URL it prints and says so; (2) `objectAdmin` under a prefix IAM condition
      cannot `storage.objects.list` (list is bucket-level), so a 403 on
      `gcloud storage ls` is correct; (3) no ADC fallback — GCS needs a key file or it
      fails at construction. **GCS was never round-tripped**: `btvaroska` inherits
      `constraints/iam.disableServiceAccountKeyCreation`, so the key cannot be minted —
      the SA and its conditional binding exist, the credential does not, T2 AC5/AC6 are
      unexecuted, and closing this is a prerequisite for R1
      (`docs/runbooks/artifact-storage.md` → BLOCKED). See docs/features/*)_

### Security

- [x] **R0-sec-1**: Mosquitto dynamic-security (P0, 1d)
      Per-device credentials + the two pattern ACLs
      (`pattern write ff/v1/d/%u/up/#`, `pattern read ff/v1/d/%u/dn/#`).
      _(done 2026-09-09; reviewed; see docs/features/* — CRITICAL, so T1 and T2 were
      re-run independently by the reviewer before the commit. `allow_anonymous false`;
      `mosquitto/conf.d/10-dev-anonymous.conf` deleted. **The plugin has no `%u`
      substitution** (verified on 2.0.22), so the design split in two: dynsec =
      authentication (`dynamic-security.json`, written by `/v1/enroll`), `acl_file` =
      the two pattern rules in the new `mosquitto/acl`; both are consulted and allow
      wins, and the dynsec `device` role is deliberately EMPTY. New `mosquitto-init`
      one-shot runs `mosquitto/bootstrap.sh` (idempotent; throwaway broker on 1884
      because `dynsec init` is the only file-mode subcommand) creating the empty
      `device` role, a read-only `ingestor` role + client, deny-by-default, and
      `chmod 0600` + `chown` on the store; the broker `depends_on` it with
      `service_completed_successfully` and its healthcheck now authenticates as the
      dynsec admin. Ingestor got its own credential; `just mqtt-pub`/`mqtt-sub` take
      user/password. New `just broker-check`
      (`python -m fleetforge.broker selftest`) is the permanent live harness, plus
      `tests/test_broker_config.py` (23 file-level tripwires, no broker).
      Verified: 312 tests + ruff + mypy + `just stack-check` green; wiped-state
      bring-up in **both** the dev and the production shape; two real boards enrolled
      through `POST /v1/enroll`, both `broker_provisioned_at` set and present in
      `dynamic-security.json`; own `up/` PUBACK RC:0, another device's `up/` RC:135,
      own `dn/` RC:135, `$CONTROL` RC:135, wrong password and anonymous refused; a
      device subscribed to `#` received nothing of another board's; a forged LWT
      produced no ingestor event; credential survived `docker compose restart
      mosquitto`; bootstrap re-run tolerated "already exists"; no password in any log.
      Vacuity-checked by breaking `%u`→`+` in `mosquitto/acl` and watching the
      selftest fail. Gotchas carried forward: (1) an allowed publish reads RC:0, not
      RC:16, whenever the ingestor is subscribed; (2) `Denied PUBLISH` is
      `MOSQ_LOG_DEBUG` in 2.0.22 and is therefore **absent** from the log — the
      `-V 5` PUBACK is the only evidence; (3) the `:ro` acl bind mount always logs
      `chown: … Read-only file system` + permission warnings, expected; (4) devices
      enrolled during the `NullProvisioner` era cannot be reconciled and must
      re-enroll. Nothing in `spec/` touched; three proposals in the hand-off.
      Reviewer note for `R0-infra-3`: the `acl_file` patterns apply to **every**
      authenticated username, so a `MQTT_DYNSEC_USERNAME`/`MQTT_INGESTOR_USERNAME`
      that happened to be 12 lowercase hex digits could be re-keyed by enrolling
      that device_id (`ensure_client` does create → already-exists →
      `setClientPassword`). Safe today — `ff-admin`/`ff-ingestor` are not valid
      device ids and `POST /v1/enroll` 422s them — but prod credentials must keep
      a non-hex username.
      See docs/features/infrastructure.md, DECISIONS.md 2026-09-08.)_

### Firmware

- [x] **R0-fw-1**: ESP32 agent, connect-only (P0, 3d)
      Ships the **flash-time immutables**: A/B partition table, rollback-enabled
      bootloader, eFuse posture — none of which can be fixed by OTA later. Network via
      `esp_netif`; **SNTP before the first TLS handshake**; HTTPS enroll; announce /
      presence / heartbeat.
      _(done 2026-09-10; reviewed; see docs/features/enrollment.md,
      docs/runbooks/agent-qemu.md, DECISIONS.md 2026-09-09.
      T1: lint + mypy clean, 488 tests green, `BUNDLE OK` on all four targets.
      T2 re-run independently in QEMU on the shipped `agent/dist/esp32` bundle:
      first boot loaded `ff_cfg v1 (crc ok)` → `device_id 000000000000` →
      `eth link up, ip 10.0.2.15` → `sntp: 1970 → 2026` → `enroll 200` →
      `credential stored in NVS` → MQTT connect, `subscribe …/dn/#`, retained
      announce + presence, `hb` every 10 s unretained; no token, password or
      `ffe_` string anywhere in the transcript. Second boot on the same flash
      image logged `reusing the stored credential (no enrollment)` and burned no
      second token (token count and the api log's `enrolled device` lines both
      unchanged). An ungraceful kill flipped `/v1/devices` to `online: false`
      inside ~45 s off the retained LWT. A one-byte corruption of the blob
      produced exactly one `ff_cfg: crc32 mismatch` line and a halted board.
      Found on the way: `CONFIG_MBEDTLS_HAVE_TIME_DATE` is off by ESP-IDF
      default, so certificate validity dates went unchecked — now enabled and
      required by `verify_bundle.py` and `tests/test_agent_partitions.py`.
      CRITICAL: the confirm/rollback pair is guarded by
      `ESP_OTA_IMG_PENDING_VERIFY` on both branches and confirms only after the
      retained announce is acknowledged; no OTA write path exists; no `spec/`
      edits — proposals recorded in DECISIONS.md.)_

### Frontend

- [x] **R0-fe-1**: "Enroll a board" page — generate token (P0, 1d) ✅ 2026-09-09
      Ships the login gate too — the API is authenticated, so without one the page is
      unreachable in a browser. Plaintext shown once, held in component state only.
      T2: token minted through the page in Chromium enrolled a simulated board; the
      row flipped to `used`, and a replay from a second board was refused 409.
      _(done 2026-09-09; see docs/features/enrollment.md)_

- [x] **R0-fe-2**: Live device list via SSE — online/offline, version, last-seen (P0, 1.5d) ✅ 2026-09-10
      The event is only a hint — every row comes from `GET /v1/devices`. Three triggers:
      a coalesced frame, `onopen` resync, and a plain 10 s re-read (a sleepy board goes
      offline with no event at all). T2 through nginx: a board's row appeared 0.7 s after
      the enroll, flipped `offline` 1.4 s after the LWT, and the sleepy case flipped with
      nothing but `: keepalive` on the stream.
      _(done 2026-09-10; see docs/features/enrollment.md)_

- [x] **R0-fe-3**: Web Serial flasher (P0, 2.5d) ✅ 2026-09-10
      `esptool-js`: port select → chip detect → board-confirm shortlist → flash agent +
      baked config. Chromium-only; that limit is accepted in the spec. Every write
      address comes from `GET /v1/agent/manifest`; the single-use token is minted last,
      after chip/flash/sha256 checks, and revoked if the write fails.
      T2-A: the frontend's own TypeScript encoder (`scripts/emit-ffcfg.ts`) wrote a
      4096-byte 0600 blob that `agent/tools/ff_cfg.py::decode` accepts and that is
      byte-identical to the Python writer's; QEMU booted on it — `ff_cfg v1 loaded
      (crc ok), 209 byte payload from 0x12000`, `device_id 000000000000`, `enroll 200`,
      `mqtt connected` — and `GET /v1/devices` showed it `"online": true` with its
      token `used`.
      _(done 2026-09-10; reviewed; see docs/features/enrollment.md)_

### Test

- [x] **R0-test-1**: Python device simulator (P0, 0.5d)
      Speaks the R0-spec-1 protocol. Unblocks backend and frontend without hardware;
      sleepy and slow-link modes. Build this early.
      `src/fleetforge/simulator/` (`just sim`, `just sim-fleet`): enroll over HTTPS →
      persist the credential → connect → retained `up/announce` + retained
      `up/presence {"online":true}` → `up/hb` → retained goodbye. `always_on` (with
      capped-backoff reconnect), `sleepy` (a fresh client per wake, no goodbye between
      wakes) and a seeded `--link slow` profile; `fleet` issues its own single-use
      tokens through login + `POST /v1/enrollment-tokens`. **It imports nothing from the
      server but `fleetforge.identity`** — no `fleetforge.config`, so a fake board needs
      no `DATABASE_URL` — with an AST tripwire holding the line, and `urllib.request`
      rather than `httpx` because this package ships in the production image. Nothing in
      `spec/` touched; three additive `spec/device-protocol.md` proposals (the LWT is
      published retained; the agent's `client_id = device_id` + `clean_session = false`;
      a planned shutdown SHOULD publish a retained `{"online":false}`) are recorded in
      `DECISIONS.md` as proposed, not written.
      _(done 2026-09-09; reviewed; **CRITICAL** — it writes a live broker password to
      disk and burns real single-use tokens, so T1 and T2 were re-run independently by
      the reviewer before the commit. Verified: **364 tests green** (52 of them
      `tests/test_simulator.py`, up from 312 at R0-sec-1) plus ruff, `ruff format
      --check`, `mypy src/` and `just stack-check` clean. Live against `just up`: a fresh
      token enrolled `a604b80498eb` from nothing → announce/presence retained, `hb` every
      5 s not retained, retained goodbye, `.sim/` `drwx------` with one `-rw-------` file;
      **zero credential leak** — the `mqtt_password` appears 0 times in the transcript, 0
      times in any container log and 0 times in a full `pg_dump`, and `.sim/` is
      gitignored and unstaged. Token discipline: a second run printed `state reusing …
      (no enrollment)` and made no HTTP call; `--power-class sleepy` with no
      `--wake-interval` was refused **before** the POST and the token stayed `used_at
      NULL`; the same token on a different board got a readable 409; a bogus token a
      readable 401; no state and no token a readable refusal — every failure one
      `SIMULATOR FAILED:` line, no traceback. `--crash-after` (`os._exit(1)`) made the
      **broker** publish the will and the board read `online:false` ~0 s later; a sleepy
      board at `--wake-interval 10` flipped offline ~25 s after its last message with
      nothing published. Vacuity-checked by breaking three things and watching the right
      test fail: `hb` retained → the QoS/retain matrix test; the will's `retain=False` →
      the will tests; `import fleetforge.config` → the purity tripwire; all reverted.
      Gotchas carried forward: (1) a clean DISCONNECT never fires the LWT, hence the
      goodbye on every graceful exit and `--crash-after`'s `os._exit(1)`; (2)
      `aiomqtt.Client` is not re-enterable (`MqttReentrantError`), so a sleepy wake builds
      a fresh client and the client arrives as a factory; (3) a denied publish is
      invisible below MQTT v5 — "it published and nothing happened" is an ACL or
      credential symptom, not a publish bug. Not verified: `just up-prod` (no compose,
      Dockerfile or dependency change in this task; `just stack-check` covers both compose
      shapes). Follow-up raised separately: the `ingestor` healthcheck only touches its
      liveness file when a message arrives, so an idle fleet marks a healthy ingestor
      unhealthy — pre-existing from R0-be-3, made visible by this task.
      See docs/features/enrollment.md, docs/runbooks/dev-stack.md, DECISIONS.md
      2026-09-09.)_

- [ ] **R0-test-2**: E2E on real hardware (P0, 1d)
      Flash → enroll → appears online in the dashboard.

---

**Total: ~24.5d** (20 tasks). Build order:
`infra-0 → spec-1 → db-1, infra-1 → be-1…be-6, sec-1` (with `test-1` early, so frontend
work needs no hardware) `→ infra-3 → fw-1, infra-2 → fe-1…fe-3 → infra-4 → test-2`.

**Parallel spike (de-risks R2):** throwaway OTA + auto-rollback spike on real flaky
Wi-Fi. Tracked in [docs/features/ota-deploy.md](docs/features/ota-deploy.md).
