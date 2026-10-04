# Fleetforge — Core User Flows

*Companion to [prd.md](prd.md), [design/architecture.md](../design/architecture.md) and [device-protocol.md](device-protocol.md). The two tasks that define the product from the user's chair.*

## Flow 1 — Onboard the first board (and every board after it)

*Persona order for this flow (2026-10-04, [DECISIONS.md](../DECISIONS.md)): **Alex** (hobbyist) is primary and the browser flow is built for them; **Marcus** (OEM) is second — the browser flow is their pilot board, and batch flashing reuses the same session resource; **Siddharth** (CI) and **Sarah** (HIL lab) are served by that resource without a second design; **Elena** (swarm) is deferred except for the clock-source line in step 5.*

The operator sees **one flow with one result**, and a status strip that stays on screen for all of it:

```
[ UI 0.4.2 · API 0.4.2 ]  Board 94a990dd09a4 · ESP32-S3 · COM3 · fw 0.3.1 → 0.4.5 (ab-4m-arduino-v1) · Boot #1 · Waiting for clock (18 s)
```

```
1. CONNECT   Dashboard → "Add a board" → plug into USB → one click grants the port (Web Serial).
             A missing or wrong port gets the plain-language port help (charge-only USB cable,
             missing CP2102/CH340 driver link, COM1 system-port refusal), never a raw error.
2. IDENTIFY  esptool-js reads chip family + revision + flash size + PSRAM + MAC
             → confirm board from a narrowed shortlist (always incl. "enter manually").
             PRE-FLIGHT CARD, before anything is written: physical flash health, what is on the
             board now (enrolled? which firmware and partition layout?) and exactly what the
             flash will change. A board that is already enrolled is offered "re-flash, keep
             identity" next to "re-flash and re-enrol".
3. CONFIGURE Broker URL + link (Wi-Fi or Ethernet) + toolchain layout profile: Arduino /
             PlatformIO (`ab-4m-arduino-v1`, default for makers) or ESP-IDF (`ab-4m-v1`), so the
             initial flash lays down the exact partition table the operator's IDE builds against.
             Wi-Fi credentials are remembered in THIS BROWSER only (never sent to or stored by
             the server), so the operator types them once per browser, not once per flash.
4. FLASH     Dashboard mints a scoped, revocable, single-use ENROLLMENT TOKEN, flashes the
             matching prebuilt starter agent + baked config    [USB config flash]
             (config goes into the 4 KB `ff_cfg` partition — device-protocol.md
              → *Partition layouts*; NVS is never written by the flasher).
             One progress bar. After the write the operator touches nothing. A write or verify
             failure names whether USB dropped mid-write (loose cable / hub reset) or flash
             verification failed at a sector (defective flash chip).
5. WATCH     The console re-acquires the board by itself and watches Web Serial and the server
             enrollment stream in parallel (so a native-USB ESP32-S3/C3 port reset never causes a
             false failure), showing a MILESTONE TIMELINE:
             Agent running → Network up → Clock set → Enrolled → On the fleet,
             each with elapsed seconds. If the board reboots during watch, reached milestones
             retract, the boot counter increments with the reset reason (`power-on`, `brownout`,
             `panic`, `download-mode`), and hardware loops are named immediately (e.g. "Rebooted
             3× during Wi-Fi startup: Brownout / low USB power — swap USB cable or port"). A
             stalled milestone says, in plain language, what the board is doing and what happens
             next — e.g. "No time server reachable; the board gives up after 15 s and carries on"
             — and names the clock source. Reading a raw UART log is not a step in any outcome.
6. RESULT    One card, success or failure: device id, firmware, partition layout, link, clock
             source, enrolled, on the fleet, UI and API versions. A failure shows ONE cause (bad
             cable / power brownout, wrong Wi-Fi password, unreachable broker/clock, stuck in
             download mode), ONE in-place recovery action (retry, re-flash, mint fresh token,
             reboot), and ONE click to copy a redacted diagnostic bundle. The card is the same
             data an API client reads (see below).
→ Managed, online device. Name it and assign a group/tag from the card.
```

**On the board** (between steps 4 and 5; unchanged):

```
B1. Board boots → POSTs /v1/enroll over HTTPS: token + identity
    (device_id from eFuse MAC, platform_type, capabilities, link/power
     class, partition layout, slot size, fw version)
B2. Token valid → AUTO-ENROLL: registry entry created, per-device broker
    credential issued, TOKEN BURNED (single-use)
    → agent stores credential in NVS and connects to the broker
```

**Decisions:**
- **One flow, one result, one status strip.** The versions, the selected board's firmware and layout, the boot count and the onboarding state are read in one place. Today they are split across a footer, a table and the flasher, and an operator has to compare them by hand.
- **Pre-flight before anything destructive.** Step 2 says what will change before the flash. A flash that re-enrols a board ends its previous identity and baseline; the operator must be told first, not find out afterwards.
- **"Re-flash, keep identity" is a requirement, not yet a mechanism.** Until it is built, every flash mints a fresh token and the board re-enrols (as today). How it keeps the credential and `ff_cfg` without burning a token is a design question, tracked in `docs/features/enrollment.md`.
- **Toolchain layout profile chosen at onboarding (`ab-4m-arduino-v1` vs `ab-4m-v1`).** Alex writes sketches in Arduino IDE or PlatformIO, which hardcode app offsets (`0x10000`) that conflict with `ab-4m-v1` ([ota-library.md](../docs/features/ota-library.md) → `R3-fw-1`). Flashing a starter agent built for `ab-4m-arduino-v1` in step 4 (`ff_cfg` at `0x3D0000`) lets a maker onboard in the browser and immediately OTA their own Arduino `.bin` in Flow 2 without ever fighting USB offsets or wiping `ff_cfg` from the IDE.
- **Hardware fault triage, dual-source watch, and clean escalation.** Watching Web Serial and server enrollment in parallel avoids false negatives when native-USB parts (`ESP32-S3/C3/C6`) drop and re-enumerate their port on reset. Retracting milestones on reboot and surfacing the reset reason (`brownout`, `panic`, `download-mode`) plus boot count separates a bad board, bad power rail, or charge-only cable from a slow network. Every failed Result card pairs an in-place recovery button with a one-click redacted diagnostic bundle ([standards.md](standards.md) → *Unaided onboarding*).
- **Onboarding is an API resource.** An *onboarding session* carries `state`, the milestone list with timestamps, the boot count and last reset reason, the plain-language stall text, and the final result. The dashboard renders it; CI (Siddharth), a HIL rack (Sarah) and a batch CLI (Marcus, post-v1) read and drive the same resource. No second design for headless use.
- **Detection/flashing = in-dashboard Web Serial** (`esptool-js`, the ESP Web Tools stack). Plug in → one click to grant the port → chip auto-detected → flash. Zero install.
  - *Caveats:* Chrome/Edge only; needs `localhost`/HTTPS — **satisfied in v1 by the
    hosted public domain** (Let's Encrypt), and by `localhost` for the dev loop; one mandatory click (browsers forbid silent port enumeration); no batch enrollment in v1 — **CLI flasher for batch/CI is post-v1.**
- **ID granularity = chip-level auto + confirm board.** esptool reliably identifies the *silicon*; the exact dev board is a **heuristic shortlist** (chip + flash + PSRAM matched to a board DB) the user confirms — with "enter manually" always available. The running agent self-reports authoritative `platform_type` + capabilities anyway (B1), so detection only needs to pick the right binary + seed identity.
- **device_id = eFuse MAC** (stable, factory-unique) — satisfies the identity contract.
- **Provisioning = USB config flash (v1).** Creds baked in at flash time. *SoftAP captive portal is post-v1* — until then, a Wi-Fi change means re-flash (accepted trade-off for a lean v1).
- **Trust = auto-enroll via a single-use token, exchanged over HTTPS** — not over MQTT, so the broker never has to authenticate a client it has never heard of. Zero-friction and batch-friendly. Rationale, threat model and the exact exchange: [device-protocol.md](device-protocol.md) → *Enrolment happens over HTTPS*. Token lifetime and posture: [prd.md](prd.md) → *Security & data posture*.
- **Clock visibility, not a field gateway.** Elena's offline gateway (local broker, artifact cache, time source) stays out of v1. What v1 owes her persona is only that step 5 names the clock source and explains the SNTP stall.

## Flow 2 — Put code onto a registered board

*Persona order for this flow (2026-10-04, [DECISIONS.md](../DECISIONS.md)): **Alex** primary; **Marcus** second (audit record now, rings later); **Siddharth** and **Sarah** through the same API resource; **Elena** deferred — v1 only keeps "staged" distinct from "apply". v1 updates **one board at a time**; group and bulk deploy is V3.*

What the operator sees is **one flow with one result**, under the same status strip as Flow 1:

```
1. PICK       Drop a .bin into the dashboard, or choose an already-uploaded build. Chip target,
              version, image size and app descriptor are read from the file header where possible.
              No shell step.
2. PRE-CHECK  A card before anything is sent: current → target version; the board's chip,
              partition layout and slot size against the build's; link health (online, weak RSSI,
              or sleepy); and what happens if the update fails ("rolls back on its own").
              Refuses in plain language before any bytes move:
              • a chip, partition-layout or slot-size mismatch;
              • a full-flash merged binary (`*.merged.bin` at 0x0 instead of an app image —
                names the app `.bin` to pick instead);
              • a binary missing the Fleetforge OTA library marker ("would run once and orphan
                the board from future updates");
              • a board reporting `rollback_capable: false`.
3. SEND       One button. An offline or sleepy board is told so: "queued until it reconnects /
              wakes". Idempotent: a repeated send is deduplicated, never a second download.
4. WATCH      A timeline — sent → downloading → staged → rebooting → confirming → confirmed —
              with elapsed seconds and deadlines. A stall explains itself ("no data for 60 s; the
              board gives up at 80 s"). It reports only what the agent really reports: no
              invented percentage.
5. RESULT     One card: firmware before → after, GOOD, ROLLED BACK or FAILED BEFORE REBOOT, with
              the reason, UI and API versions, who sent it and when. A failure shows one cause
              and one next action (plus "Send again" on a pre-reboot transport failure). When a
              build rolls back, the card shows the failed boot's last-gasp crash breadcrumb
              reported by the surviving slot (panic/exception, task watchdog, brownout, custom
              self-test failure, or confirm timeout, plus the last milestone reached) so an
              installed board can be debugged without USB.
```

**The transaction** (what the board and server do between steps 3 and 5; unchanged):

```
1. Upload artifact (.bin) → declare version + platform_type
2. Server capability-checks: reject on chip / partition-size mismatch
3. SIMULATE (advisory, V2): boot artifact + run self-test → pass / warn+override
4. Select target: this device, or a group/tag
5. Deploy → device pulls (resumable) → verify checksum+sig →
   write OTA1 slot → reboot
6. CONFIRM (timeout): new fw reconnects to broker [+ optional self-test]
   → mark good, else AUTO-ROLLBACK to OTA0
7. Dashboard: per-device progress + final delivery-success / fleet-safety state
```

**Decisions (operator view):**
- **One flow, one result, one status strip.** Before and after firmware, the verdict and the versions are read in one place, not compared by hand across a table row and a footer.
- **Getting firmware in is a dashboard operation.** An upload form replaces `docs/runbooks/upload-artifact.sh` as the way in (`spec/standards.md` → *dashboard*).
- **Pre-check before send, including hobbyist binary traps.** Beyond the layout and slot-size refusal that CUJ-1 requires, step 2 catches the two mistakes that brick an Arduino maker's installed board: uploading `sketch.ino.merged.bin` (bootloader + partitions + app at `0x0`) instead of `sketch.ino.bin`, and uploading a sketch compiled without the Fleetforge library (which boots once and permanently orphans the board). It also checks `rollback_capable` ([board-profiles.md](../docs/features/board-profiles.md)).
- **Post-rollback "last-gasp" crash reason from the surviving slot.** An installed board has no USB cable attached. If `OTA1` crashes or fails to confirm, the server sees nothing from `OTA1` directly; once the bootloader rolls back to `OTA0` and the known-good image reconnects, it reports `rolled_back` together with the failed boot's reset reason (`esp_reset_reason()` / RTC breadcrumb: panic, watchdog, brownout, custom self-test failure, or network/confirm timeout) and the last milestone `OTA1` reached.
- **The deployment is an API resource.** The result card, an audit record (who, what, when) and a CI client read the same thing. This is the audit trail Marcus needs; rings and provenance build on it later.
- **The timeline reports only what the agent reports.** No progress bar: `downloading` is published once, so a bar would sit still and read as a hang.
- **Open, recorded not decided:** how a sleepy battery node avoids a false rollback from the confirm timer (beyond surfacing its sleepy wake window at pre-check), and how the Fleetforge library marker is encoded in the app binary header.

**Decisions (transaction):**
- **Artifact source = user's own toolchain (v1).** They build the `.bin` (idf.py / PlatformIO / Arduino); the server never builds in v1. *V2 adds a server-side compiler as one more producer — see [build-pipeline.md](../docs/features/build-pipeline.md).* Artifacts are opaque + versioned.
- **Self-test = default + optional custom.** Default self-test is **"boots and reconnects to the broker"** (catches boot-loops, zero user effort). Users may add a **custom self-test baked into firmware** — one entrypoint the agent calls after boot and the simulator calls as its gate: *write once, run in sim and on device, no drift.*
- **Targeting = device or group/tag** (v1). Staged/canary rollout is post-v1.

## Flow 2 (V2) — Deploy from version control
VCS integration and the server-side compiler are **automated artifact producers** feeding the same pipeline; steps 2–7 above are unchanged.

```
1. git tag / release in the user's repo
2. CI builds the .bin and PUSHes it to Fleetforge's upload API,
   with PROVENANCE: repo + commit SHA + tag + build URL     [push model]
   (or Fleetforge builds it itself from the repo ref — R10)
3. Fleetforge registers it as a deployable version
4. Per-group DEPLOY POLICY decides:
     • manual   → appears as deployable, human clicks deploy
     • auto     → deploys to that group on a matching tag
   → then the normal capability-check / sim / pull / confirm / rollback runs
```

**Decisions:**
- **Ingestion = push first.** User's CI POSTs the artifact (ship a ready **GitHub Action** + templates for GitLab/Gitea). Provider-agnostic, holds no repo secrets, air-gap-friendly. *Pull adapters (Fleetforge watches releases) come later.*
- **Provider scope = provider-agnostic API.** One generic upload endpoint + provenance schema; GitHub Action provided, but self-hosted GitLab/Gitea/Forgejo work with a few lines. Matches the self-host ethos.
- **Deploy policy = configurable per group.** Dev fleet can auto-deploy on tag; prod fleet stays manual.
  - *Sequencing:* auto-deploy is only as safe as its rollback. Per-device auto-rollback makes it *survivable* in early V2; **enable auto-deploy-per-group with confidence only once canary/staged rollout lands.**
- **Payoff = traceability.** Every device's firmware links to a commit: "what's running on device X?" and "roll back to tag v1.3" become first-class.

## Where the pieces line up
- The **self-test** appears in Flow 2 step 3 (sim gate) and step 6 (device confirm) — the same code, two enforcement points.
- **Identity** announced in Flow 1 step B1 is the `platform_type` + capabilities the server matches against in Flow 2 step 2.
