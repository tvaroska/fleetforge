# Fleetforge for the electronic hobbyist

**Date:** 2026-09-21
**Status:** product review and recommendation — not a spec, not a plan.
**Audience:** the operator of this instance, and anyone deciding what to build next
for makers rather than for a swarm.

This is a reading of the current aim, documentation and feature set against one
question: *what would actually get used by someone with a DevKit, a sketch, and a
board they cannot easily walk to?* Requirements stay in `spec/`; live tasks stay in
`TODO.md`. Landing any of the recommendations below is a `/new-feature` (or a
release), not an edit of this file.

---

## 1. Aim — what this actually is

**One sentence:** a self-hosted control plane that lets a maker push firmware to
boards they cannot easily reach, without handing the fleet to a vendor cloud, and
without a bad build bricking them.

The problem statement in [`spec/prd.md`](../spec/prd.md) is precise and correct:

> USB flashing doesn't scale past the bench; a bad push with no recovery bricks
> devices and kills trust.

The architectural bet is two thin waists:

| Waist | Contract | Why it matters |
|---|---|---|
| Device-facing | Opaque versioned blob + four verbs (`stage → apply → confirm → rollback`) | ESP32 today, Pi/FPGA later, without rewriting the core |
| User-facing | Headless API + SSE; every UI is a client | Dashboard, HA, CLI, MCP all equal |

Two authority rules that almost nobody else in this space gets right:

1. **The device owns the reboot.** A drone must not apply mid-flight; a vehicle
   must not reboot in motion; `awaiting_safe_window` may last indefinitely.
2. **The device owns the rollback.** A board that cannot reach the broker is
   exactly the board that must roll back — so the server never sends a rollback
   command.

That is a real product, not a dashboard over `esp_https_ota`.

**Who it is for (stated):** solo makers / small teams, a handful to a few dozen
ESP32s, who value owning fleet data and a fast path from USB to first update.

**Who it is also for (implied by the ladder):** a robotic swarm — ground vehicle
as gateway, many drones, Thread, delta updates, FPGA bitstreams. V3 is where the
architecture is aimed. V1 is five heterogeneous boards for one operator, **not a
public product until V3**.

That split is the first strategic tension. The *user* is a hobbyist. The
*roadmap* is a swarm OS. Both can be true, but they pull feature order in
opposite directions.

---

## 2. Documentation — excellent for the builder, incomplete for the user

### What is unusually good

The gen-3 layout is actually followed, not just declared:

- `spec/` is THE WHAT and protected
- `design/` is THE HOW and status-free
- `TODO.md` is the only live status
- `DECISIONS.md` is append-only and dense with *why*
- `CRITICAL.md` exists for paths that OTA cannot fix (partition table,
  bootloader rollback bit, eFuse)
- The device protocol is marked near-frozen *and treated that way*

The load-bearing documents earn it:

- [`spec/device-protocol.md`](../spec/device-protocol.md) — topic namespace *is*
  the ACL design; retain vs persistent-session rule prevents a whole class of
  replay bugs; SNTP-before-TLS is written down before anyone hits “certificate
  not yet valid”
- [`design/architecture.md`](../design/architecture.md) — flash-time immutables,
  transport split (MQTT control / HTTPS bytes), capability reporting
  (`partition_layout` + `ota_slot_size`)
- [`spec/prd.md`](../spec/prd.md) — two KPIs that cannot be gamed against each
  other (delivery success vs fleet safety)
- [`spec/standards.md`](../spec/standards.md) — *Unaided onboarding* is a real
  product standard, not a slogan

Decision quality is high. Examples: enrollment over HTTPS not MQTT; ingestor as
sole MQTT subscriber; agent bundles as artifacts not image contents;
impersonation instead of a GCS key file; `fw_version` is the version that
*booted*.

### What is weak or stale

**README is a lie.** It still says the dashboard is a skeleton and “the agent,
the flasher and OTA itself do not exist.” R0 is live at `bingo.tvaroska.sk`.
R1’s backend, firmware, and dashboard button have landed. A hobbyist (or a
future-you in six months) will bounce.

**The product surface in the PRD is not planned.** This sentence is in
`prd.md`:

> Device side: (a) a prebuilt agent to flash for instant wow, and (b) **a thin
> OTA library (ESP-IDF/Arduino) to embed in custom firmware**.

(b) has no feature file, no release, no task, no contract. For a hobbyist this
is not a later nicety — it is the product. The prebuilt agent is a demo that
connects and heartbeats. Nobody deploys a Morse-code blinker they cannot write.

**CUJs do not exist.** [`spec/open-questions.md`](../spec/open-questions.md)
says so. *Unaided onboarding* has acceptance criteria hanging in air. There is
no “I have an Arduino sketch and a DevKit on the desk” journey anywhere.

**Feature files are two different products.** `enrollment.md` /
`infrastructure.md` / `ota-deploy.md` are 50–100 KB of as-built archive.
`health-telemetry.md` is 1.3 KB of stub table. Status lines are stale
(`ota-deploy.md` still says Planned). Fine for the implementer; useless as a
map.

**No operator manual.** Runbooks cover the *stack* (dev-stack, artifact-storage,
agent-qemu, agent-build). Nothing covers “flash a board, upload a `.bin`,
recover from a brownout” as a user. The dashboard *is* supposed to be that
manual (`enrollment-console-is-the-diagnostic-surface`), but that only works
once onboarding is proven on metal — and it is not.

**Hobbyist vs swarm is not resolved in writing.** v1 = 5 boards, one operator,
hosted. V2 = source-to-artifact. V3 = swarm, and *also* “public product.”
Groups, bulk deploy, HA, CLI, SoftAP, Arduino library, self-host TLS — all
deferred, many of them the things a hobbyist actually opens the app for.

**Known spec bugs sitting in `open-questions.md`:** `prd.md` “≤ 1.9 MB” vs
`ota_slot_size` 1966080; sleepy-device `confirm_timeout_s` will roll back a good
image (called out in the ESPHome review at `products/docs/esphome-review.md`,
not yet in spec).

Verdict on docs: **A- as an engineering system, C as a product description.**
The architecture will survive contact with a second platform. A hobbyist cannot
currently tell what to *do*.

---

## 3. Feature set — as built, as planned, as missing

### What is actually there (R0 + most of R1)

| Capability | State |
|---|---|
| Hosted Compose stack, one origin, admin auth | Live |
| Enrollment tokens, `POST /v1/enroll`, per-device MQTT creds + pattern ACLs | Live |
| Web Serial flasher, chip detect, baked Wi-Fi + token | Live (Chromium only) |
| Serial console as diagnostic surface, fault naming, diagnostic bundle | Live (software; hardware unverified) |
| Prebuilt agent (esp32/s3/c3/c6), A/B layout, rollback-enabled bootloader | Shipped as artifacts |
| Live fleet list over SSE | Live |
| Artifact upload, signed-URL download with Range, `stage → apply` orchestration | Built, QEMU-proven |
| Per-device Deploy button, version-change feedback | Built |
| `deploy_events` written forever | Built |
| Device simulator | Built |

### What is gated on a board

R0 is not closed. The one board on hand brownouts during RF calibration
(`S0-fw-3`). `R0-test-2` (the release’s “done when”) and `R1-test-1` cannot run.
QEMU covers the transport; it does not cover the radio, the power rail, or a
USB-CDC re-acquire. The product’s stated risk is onboarding, and onboarding is
unproven on metal.

### The v1 ladder (R2–R5) — the actual product

| Release | What a hobbyist gets | Why it matters |
|---|---|---|
| **R2** ⭐ | Checksum + A/B auto-rollback, device-armed confirm | The reason to OTA a board in the attic |
| R4 | Heartbeat metrics, last-seen, boot-ok | “Is it alive?” |
| R5 | Custom self-test at confirm | “Boots but the display is garbage” |
| R6 | App-level signing, resumable download, two KPIs | Production-grade, still one layer of defense |

*(Numbering as of the 2026-09-22 renumber: R3 is now the OTA library this review
recommends, and the rest of the v1 ladder shifted by one. `docs/releases.md` is the
current ladder.)*

R2 is correctly identified as the whole gamble. Until it ships, the dashboard’s
Deploy button is a brick factory. `TODO.md` already warns this; a hobbyist will
click it anyway.

### Explicitly out of scope for v1 — and this is where hobbyists live

From `prd.md` and the feature files:

- Thin OTA **library** (promised, unplanned)
- Arduino / PlatformIO as first-class (IDF agent only)
- SoftAP / Improv Wi-Fi change without USB
- Home Assistant
- CLI flasher / batch enroll
- Self-hosting (TLS without public DNS)
- Groups & bulk deploy (moved to V3)
- Application config (`dn/cfg` is agent-only, by design)
- Remote shell, logs beyond 7 days, alerting

Some of those refusals are load-bearing (config-management would eat the
product; ESPHome is the existence proof). Some are sequencing. Some are a
hobbyist-shaped hole.

### Competitive position

ESPHome owns the **build**. Fleetforge refuses the build in v1 and treats the
builder as one more producer in V2. That is the wedge: N devices per artifact,
target-version-per-group, no “update all recompiles 377 nodes for 10 hours.”
The comparison is in [`products/docs/esphome-review.md`](../../docs/esphome-review.md).

ESPHome is stronger on: onboarding UX, a decade of ESP32 OTA edge cases,
`safe_mode` (both slots bad), Improv, HA, sleepy-device confirm. Fleetforge has
none of those in v1 except a hand-rolled flasher.

ArduinoOTA is LAN-only, no inventory, no rollback. Particle/Balena/Arduino Cloud
are vendor clouds. Mender is Linux. The gap Fleetforge claims is real. The
on-ramp into that gap is not built.

---

## 4. Top 3 features for an electronic hobbyist

Ranked by *unlock*, not by roadmap order. “Unlock” = a hobbyist can use this on
*their* project, on a board they cannot walk to, without becoming an ESP-IDF
expert.

### 1. Thin OTA library — Arduino, ESP-IDF component, PlatformIO

**This is the on-ramp. It is named in the PRD and does not exist as a plan.**

A hobbyist’s unit of work is a sketch, not a fleetforge agent. The prebuilt
agent proves the wire. It does not water the plants, drive the frame, or fly
the quad. Until the four-verb contract is a library they drop into *their*
firmware, Fleetforge is a very good demo of itself.

**Prerequisite, not a step: where does a library user's config live?** The agent
keeps broker URL, Wi-Fi creds and enrollment token in a dedicated flash
partition (`ff_cfg, data, 0x40, 0x12000, 0x1000` in `agent/partitions.csv`),
written by the browser flasher. A hobbyist who drops the library into a sketch
and flashes from the Arduino IDE **has no `ff_cfg` partition** — the config has
nowhere to land, and overwriting a custom partition table is the classic
Arduino-IDE failure. So the release opens with a decision, not with extraction:

- **(a) Ship a packaged partition table + board definition** and make it
  mandatory (A/B, `APP_ROLLBACK_ENABLE`, 4 MB minimum). Cheapest, but the
  library only works for users who adopt our flash layout, and flash-time
  immutables mean getting it wrong is unrecoverable.
- **(b) Add an NVS-backed config path** so the library works on a stock Arduino
  partition scheme. This is a real agent change and touches `ff_cfg` +
  enrollment — not packaging.

Pick one before estimating. Then:

1. **ESP-IDF component** (`idf_component.yml`) — the agent already has the
   modules (`ff_ota`, `ff_mqtt`, `ff_enroll`, `ff_cfg`). Split “the protocol”
   from “the demo app.” This part really is mostly extraction.
2. **Arduino library** wrapping the same C — this is the actual hobbyist
   population. If they have to migrate a project to ESP-IDF to get OTA, they
   will not.
3. **A 30-line example:** enroll → heartbeat → handle `stage` → report version.
   Morse blinker as the documented sample, matching the PRD.

Why this beats everything else: R1–R5 are features *of the agent*. Without the
library they only update a device that does nothing the hobbyist cares about.
It also forces the protocol to stay a library-shaped contract (small, additive,
no implicit dashboard coupling) — which is what the thin waist claimed to be.

Effort shape: R0-fw already wrote most of the C. The missing work is packaging,
Arduino glue, docs, and a “first custom firmware” CUJ. That is a release. It
should have a feature file.

### 2. Safe deploy: auto-rollback **plus** safe-mode (R2, extended)

R2 as specified is the product’s reason to exist. A hobbyist will OTA a board
in a roof weather station, a crawlspace sensor, or a frame behind glass **only**
if a bad `.bin` comes back by itself.

Ship R2 as planned (checksum, device-armed confirm,
`esp_ota_mark_app_valid_cancel_rollback`, dashboard `good` vs `rolled-back`).
Then add the layer ESPHome has and Fleetforge does not:

**`safe_mode`** — but it is *two* features with very different costs, and they
must not be scoped as one:

- **(a) The app bricks itself** — boots, then loops or crashes before MQTT. A/B
  does not cover this, and it is the common case. ESPHome's answer needs no new
  partition: an NVS flag plus a boot counter reboots **the same image** into a
  reduced mode — serial + net + OTA only, held open for a few minutes,
  enterable by mashing reset. New `up/status` state: reachable, degraded, still
  updatable. Today that board looks dead. **Cheap, touches no flash-time
  immutable — ship it with R2.**
- **(b) Both slots are bad** — cannot be solved inside the image, by
  definition. It needs a recovery app partition, and `ab-4m-v1` refuses one on
  purpose (`agent/partitions.csv`: *“No `factory` partition on purpose: a
  factory-only board can never OTA its way to A/B”*). The map is also full:
  `0x20000 + 2 × 0x1E0000 = 0x3E0000`, ~128 KB spare on a 4 MB part. Per
  `CRITICAL.md` a layout change is **a new layout id, never an edit** — so every
  board already flashed as `ab-4m-v1` can never gain this. That makes it an
  `ab-8m-v2` / next-layout decision for `DECISIONS.md`, **not an R2 scope-add.**

Two spec fixes that belong in the same release, cheap now, recall-level later:

- **`confirm_timeout_s` must differ by `power_class`.** A sleepy e-paper that
  wakes, refreshes, and sleeps will roll back a perfect image against a 300 s
  wall clock. ESPHome already burned this (`boot_is_good_on_shutdown`). The PRD
  lists the e-paper as a v1 board. The protocol as written will fail that board.
- **Physical “I mashed reset on purpose”** as an on-ramp into safe-mode, because
  the hobbyist recovery tool is a finger, not a dashboard, when Wi-Fi is the
  thing that broke.

Without R2, Deploy is unsafe and the problem statement is unanswered. Without
safe-mode, R2 still loses the double-fault that every long-lived hobbyist fleet
eventually hits.

### 3. Field Wi-Fi provisioning — Improv (serial + BLE), no USB

The problem statement is “devices they can’t easily reach.” The v1 provisioner
is **USB config flash**. A Wi-Fi change is a reflash. That makes the second
operation — the one that happens after you install the thing — require the same
physical access the product exists to remove.

Hobbyist reality: the board moves from the desk AP to the house AP to a travel
router; the PSK rotates; the device is in a box, a garden, a frame, a vehicle.
ESPHome and WLED solved this years ago with Improv (Apache-2.0, serial and BLE).
The ESPHome review already recommended it for the enrollment `next_url`
handoff. Use it for the *repeat* path too.

What a hobbyist should be able to do:

1. Phone or Chromium talks to the board over BLE (or the serial console already
   on the flash page).
2. New SSID/PSK in. Nothing about the device's identity is re-typed — note this
   is **not** the enrollment token: that lives in the `ff_cfg` flash partition
   and is *burned* at enrollment. What must survive a re-provision is the broker
   credential returned by `POST /v1/enroll`.
3. Board reconnects, appears green. No ladder, no USB.

**The design question this opens:** Wi-Fi creds live in `ff_cfg`, a flash
partition the browser flasher writes. Improv means the running app rewrites
them — so either the agent gains a `ff_cfg` write path, or credentials move to
NVS. Settle that before scoping.

**Split serial from BLE.** Serial Improv reuses the Web Serial code already on
the flash page and is nearly free; BLE is the larger, separate half and is what
buys the phone-in-the-garden story. They are two tasks, not one.

Remembered flash profiles (SSID + token scope, not the PSK in the browser) are
the cheap sibling for board #2…#N on the bench —
[`spec/open-questions.md`](../spec/open-questions.md) already asks whether the
repeat path deserves its own design. Yes. Ten identical controllers is a more
common hobbyist fleet than five different research projects.

This stays inside the product boundary: it provisions **the link the agent
needs**, not application config. SoftAP can wait; Improv is the one the
community already knows.

---

## Honourable mentions (not top 3, not ignore)

| Feature | Why it is not #1–#3 | When it starts to matter |
|---|---|---|
| **Home Assistant add-on + MQTT Discovery `update` entities** | Cheap *after* the API exists; does not help you write firmware or recover a brick | The week you want other hobbyists to try it. ESPHome’s entire distribution *is* HA. |
| **Self-host Compose on a Pi** | v1 is hosted-for-one by design; TLS-without-DNS is the real unsolved piece | The moment a second person runs it. Hobbyists will not send device traffic to someone else’s domain. |
| **CLI / batch flasher** | v1 enroll is one-board, Chromium-only | ~10 boards of the same build (LED controllers, sensors). |
| **ESPHome `managed-lite` adapter** (serve a poll manifest) | Distribution play, not a core capability; MD5-only, 6 h latency, no safe-window | Instant fleet from people who already have ESPHome nodes. |
| **git-tag → artifact (R6–R7)** | Nice once OTA is trusted; hobbyists already have a `.bin` | When the library exists and they are tired of the upload button. |
| **Server-side compile (R9)** | Reverses a v1 non-goal; ACE risk; ESPHome already owns this niche | Only if you want “no toolchain on the laptop.” Do not let it become per-device compile. |

---

## What not to pull forward for hobbyists

- **V3 swarm / gateway / Thread / delta / FPGA.** Right architecture, wrong
  decade for a maker with five DevKits. `parent_device_id` reserved in the
  schema is enough.
- **Groups as a *release*.** Tags on enrollment tokens already exist. Bulk
  deploy of identical artifacts starts to matter around board #8, not at V3. A
  thin “deploy this `.bin` to these checkboxes” is a hobbyist feature; a
  hierarchical drone mesh is not.
- **Application config through `dn/cfg`.** Correct refusal. That path turns
  this into a worse ESPHome. If hobbyists need a setting, it belongs in their
  firmware (or a later, explicit app-config channel).
- **Simulation (R8) before rollback is boringly reliable.** At hobbyist scale a
  bad build costs one reboot. Sim earns its keep when CI pushes unattended.

---

## Suggested sequencing

The current plan (close R0 on metal → finish R1 E2E → R2) is right **for the
stack**. For the *hobbyist*, add two releases after R2 — and note the ladder is
integer-numbered (`R{N}-{cat}-{n}` task IDs, `roadmap.md` and `releases.md` both
key off it), so these are a renumber, not `R1.5`/`R2.5`:

```
R0  close on metal         ← do not skip; onboarding is still the stated risk
R1  E2E on metal           ← Deploy is already in the UI
R2  auto-rollback          ← the gamble; nothing safe to embed before it exists
      + safe_mode (a) in-image only
      + sleepy confirm rule
R3  OTA library + CUJ      ← NEW. Arduino example is the demo from here on
R4  health view            (was R3)
R5  custom self-test       (was R4)
R6  signed OTA → v1        (was R5)
then Improv reprovision, HA, self-host, V2 source-to-artifact
```

**Landed 2026-09-22:** R3 is now the OTA library on the real ladder
([`releases.md`](releases.md)), and the v1 tail shifted by one. Improv is not yet
slotted — it remains a recommendation, not a release.

**Why the library comes after R2, not before it.** Section 3 calls today's
Deploy button a brick factory, and that is the argument against shipping the
four-verb contract into other people's `setup()`/`loop()` first: a library is a
multiplier on however safe deploy currently is. Handing it out before
auto-rollback exists spreads the unsafe path across custom firmware on boards
nobody can reach — the exact failure the product exists to prevent. If the
library must ship earlier for momentum, gate its documentation on rollback being
live, and say so in the release.

Until R2 exists, hobbyists should not push to anything they cannot unplug. Until
the library exists, every later release is features of a firmware the hobbyist
did not write. Until Improv exists, “can’t easily reach” is only true for the
first flash.

The architecture can carry all of this — opaque artifacts, four verbs,
device-owned reboot, API-first clients. The gap is not the waist. The gap is
that the waist has a demo app on one side and a swarm roadmap on the other, and
nothing in the middle that a person with an Arduino sketch and a board in the
other room can pick up.

---

## Related

- [`docs/personas/PERSONAS.md`](personas/PERSONAS.md) — canonical persona descriptions and priority matrix
- [`docs/SWARM.md`](SWARM.md) — mixed air/ground flock persona; gateway, coordinated apply, Pi+delta
- [`docs/PLATFORMS.md`](PLATFORMS.md) — STM32 / FPGA / the ESP-shaped leaks in the waist
- [`spec/prd.md`](../spec/prd.md) — users, scope, KPIs
- [`docs/roadmap.md`](roadmap.md) — release ladder
- [`docs/releases.md`](releases.md) — what each release adds
- [`docs/features/ota-deploy.md`](features/ota-deploy.md) — R1/R2
- [`docs/features/enrollment.md`](features/enrollment.md) — R0 onboarding
- [`products/docs/esphome-review.md`](../../docs/esphome-review.md) — adjacent
  product, reuse / copy / refuse
- [`spec/open-questions.md`](../spec/open-questions.md) — CUJs, repeat-path
  enrollment, artifact-size double cap
