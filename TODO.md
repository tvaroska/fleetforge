# Fleetforge — TODO

**Goal:** Self-hosted OTA firmware management for embedded fleets (ESP32 first) — the gate catches a bad build before the fleet, and any device that gets one recovers itself.
**Updated:** 2026-10-01

## Where this stands

**Works on metal** — device `94a990dd09a4`, an ESP32-S3, against prod (`bingo.tvaroska.sk`):

- **Enroll** (R0, closed 2026-09-22). Browser flash → enrolled → live on the broker in
  13 s. `S0-test-3` passed an unaided run by someone who never saw the code.
  Write-up: [docs/features/enrollment.md](docs/features/enrollment.md).
- **OTA of the agent** (R1, `R1-test-1` passed 2026-09-23). A dashboard-driven deploy
  took the board `0.3.2 → 0.3.1` in ~25 s. Getting there fixed three prod defects that made deploy impossible (`../docs/ops-log.md` F-2026-09-23-001/002/003, all deployed).
  Write-up: [docs/features/ota-deploy.md](docs/features/ota-deploy.md).
- **Auto-rollback of "boots, joins, never confirms"** (2026-09-23). A deliberately broken
  `0.3.2-rbtest` came back on 0.3.1 in 71 s, unattended
  ([docs/runbooks/rollback-test.md](docs/runbooks/rollback-test.md)).

**Not yet, and why it matters:**

- Nothing reports confirm/rollback: every deploy parks at `rebooting` with
  `is_terminal: false` (R2-BE-1).
- An image that boots, gets its announce acked and is broken anyway confirms itself and
  **nothing recovers it**. Roll to **one board at a time**.
- No upload form in the dashboard (`docs/runbooks/upload-artifact.sh` is the only way in).
  Every device has `name: null`. Neither has a task yet.

**Next: R2 — safe deploy (check + auto-rollback)**. Not opened yet. The CUJ-1 T3 gate
blocked R1 → R2 on 2026-09-23 on segment 5. `S0-test-4` fixed the cause the same day and
the gate's reproduction now converges, but **the gate itself was not re-run**. First
step: `/replan`, which re-runs it and, on a pass, opens R2 here. R2's task list is in
[docs/features/ota-deploy.md](docs/features/ota-deploy.md) → *Phase 2* (R2-FW-3 and most
of R2-TEST-1 already landed in R1). The flaky-Wi-Fi rollback spike is still open — same
file, *De-risking*.

**Blocked:**

- `S0-test-1` needs a bridge-chip board (CP2102/CH340). `S0-test-2` needs the S3 on the
  bench. Both are hardware sessions, and the bench host is unsettled (see the ⚠️ notes).
- **R3 (thin OTA library)** waits on R2 by decision
  (`design/decisions/ota-library-ships-after-safe-deploy.md`). Its task list lives in
  [docs/features/ota-library.md](docs/features/ota-library.md) until it opens.
- **A dev box with pruned images cannot `just up`**: `minio/minio` and `minio/mc` no
  longer pull (`DECISIONS.md` 2026-10-01). No task filed yet.

<!-- Counters: spec=1 infra=7 db=1 be=6 fe=7 sec=1 fw=4 test=3 -->
<!-- Sprint 0 counters: fe=8 fw=4 infra=9 test=4 ops=1 -->
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
> (for example,`R0-be-1`). Sprint 0 uses `S0-{category}-{number}`.
> Categories: db, be, fe, test, qa, sec, infra, fw, spec, rel, perf, ops.

**Deployment (v1):** single hosted instance at `bingo.tvaroska.sk` (domain reused from
the retired bingo app), single-tenant, **not a public product until V3**.
**Versions:** V1 = R0–R6 (safe OTA, ~5 boards) · V2 = R7–R11 (VCS + compile + simulation)
· V3 = robotic swarm (gateway + drones).

---

## Sprint 0: Critical Issues

Bricking risks, broker auth and security issues get filed here as they surface.

- [ ] **S0-test-1**: Bench-check the serial console on real hardware (P1, 0.5d)
      Filed 2026-09-10, when S0-fe-1 shipped. Its software half is proven in jsdom against
      replays of real `agent/main/*.c` output. These four cannot be, because they are
      properties of a USB bridge chip and an OS, not of the classifier. The bench is
      Windows + Chrome (settled 2026-10-02). The Linux dev box does not enumerate boards
      over WebSerial.
      * **Re-acquire after `hard_reset`, bridge-chip path**. `serialConsole.ts` re-reads
        `navigator.serial.getPorts()` every 250 ms for 8 s. On a classic esp32 the port
        *survives* the reset, so this must reconnect without ever showing "No board is
        available to watch". The native-USB half of this check is **S0-test-2**. No
        C3/C6/S3 board is on hand (2026-09-11).
      * **115200 decodes cleanly**. `sdkconfig.defaults` sets no
        `CONFIG_ESP_CONSOLE_UART_BAUDRATE` so this must be right, but a wrong baud
        yields plausible-looking mojibake rather than an error. The classifier would
        then silently match nothing.
      * **The EN pulse boots the app, not the ROM loader**. `SerialConsole.reboot()`
        drives RTS high with DTR low. If the wiring inverts, the board lands in download
        mode and prints `waiting for download` forever.
      * **Release really releases**. After the button, the COM port must open in another
        terminal (for example,PuTTY, 115200). If it reports "Access denied" / port in use,
        `port.close()` is not being reached.
      Acceptance: all four confirmed against **any** bridge-chip board (CP2102 or CH340) —
      retargeted 2026-09-23, since the DevKit v1 is out of consideration and this task
      tests the bridge-chip *path*, not that board. Anything that fails comes back as a
      new S0 task with the observed behavior.
      The bench host is Windows + Chrome (settled 2026-10-02, earlier entries said the Mac).
      Re-acquire is an OS-and-driver property — record the driver and COM port used.
      * **Folded in from S0-fe-8 (accepted 2026-10-01 without a bench run)**. On Windows,
        with the board's VCP driver *not* installed, an operator who has never installed one
        reaches a working COM port using only "My board does not appear" on the flash page:
        no Device Manager, no asking. Also confirm that picking COM1 gets refused by name
        and that the Silicon Labs driver link resolves (the dev box gets a 403 from Akamai).

- [ ] **S0-test-2**: The native-USB re-acquire path, on a C3/C6/S3 (P2, 0.25d)
      Split from S0-test-1 on 2026-09-11: the only board on hand is an ESP32-DevKit v1,
      whose bridge chip keeps the port alive across `hard_reset`. That exercises the
      *easy* half. The 8 s `getPorts()` poll in `serialConsole.ts` exists for the parts
      that come back as a **different** `SerialPort`. Nothing has ever tested it on
      metal. A too-short window shows "No board is available to watch" on a board that
      is merely rebooting. This is the exact false negative the console exists to
      delete. ~~**Blocked on acquiring a C3, C6 or S3.**~~
      **Unblocked 2026-09-22**. An **ESP32-S3** is on hand and enrolled against
      prod — device `94a990dd09a4`, the board that passed `R0-test-2` on 2026-09-19. This
      task's premise ("the only board on hand is an ESP32-DevKit v1") is simply out of
      date. Cheap to run now, since the board is already flashed and known-good.
      Acceptance: on the bench, `hard_reset` from the console on a native-USB board
      reconnects inside the window and streams the boot log without operator action.
      The bench is **Windows + Chrome** (settled 2026-10-02). The S3 enrolled from it with
      native USB on COM3. Earlier entries said the Mac. That is obsolete. Record the
      driver and COM port used. The re-acquire window is an OS-and-driver property.

- [x] **S0-test-3**: Someone who did not see the code onboards a board unaided — passed 2026-09-22 → [enrollment.md](docs/features/enrollment.md)
- [x] **S0-fw-3**: A board that browns out during RF calibration cannot escape it — withdrawn 2026-09-23, not fixed → [enrollment.md](docs/features/enrollment.md)
