# Fleetforge — v1 Specification

*What the product must do, for whom, and to what standard. **How** it does it is [design/architecture.md](../design/architecture.md); the concrete stack and topology is [architecture.md](../design/production.md); the wire form is [device-protocol.md](device-protocol.md); the order of work is [releases.md](../docs/releases.md).*

## Problem
Makers and small teams who deploy connected devices they can't easily reach (sensors, home/farm/product installs) have no safe way to update firmware remotely, without handing their fleet to a vendor cloud. USB flashing doesn't scale past the bench; a bad push with no recovery bricks devices and kills trust.

## v1 Scope — the one thing done well
**Safe remote firmware updates for a fleet of ESP32 devices** — where "safe" means a bad build is caught *before* the fleet, and any device that does get a bad update recovers itself.

## Users
Primary: **solo makers / small teams** with a deployed ESP32 project (a handful to a few dozen boards). Values owning their fleet data and a fast path from install to first update.

## Deployment model
**v1 ships as a single hosted instance on a public domain** (TLS via Let's Encrypt), **single-tenant** — one instance manages one fleet, behind admin auth. The stack stays a Docker Compose deployment throughout, so nothing about the architecture assumes hosting; but the *supported* v1 deployment is the hosted one.

**Not a public product until V3.** The instance is internet-reachable — devices and the Web Serial flasher both require real TLS — but there is no signup, no marketing surface and one admin. v1 and V2 are for one operator's own fleet. Hosting details, including the domain it currently occupies, are in [architecture.md](../design/production.md).

**Deferred to V2:** turnkey self-hosting (the hard part is TLS without public DNS — a local CA the browser and the device both trust) and multi-tenant/public signup. Local Compose on `localhost` remains the dev loop, which is what keeps the V2 self-host path a configuration change rather than a rewrite.

*Consequence:* the broker and artifact endpoint are exposed to the public internet from R0, so per-device broker credentials + topic ACLs and single-use enrollment tokens are **R0 requirements**, not post-v1 hardening.

## Device link — IP-bearing
The device contract requires **an IP-bearing link and TLS, nothing more** — never "Wi-Fi". Wi-Fi is the v1 workhorse; Ethernet and cellular work today at no extra cost, because MQTT and HTTPS only need IP.

**Deferred to V2+ — gateway-mediated non-IP radios** (Zigbee, BLE, LoRa). These cannot reach a hosted server at all; they need an on-site Fleetforge **gateway** proxying the four verbs onto the local radio — a second product, so it waits. What v1 pays to keep that additive is described in [design/architecture.md](../design/architecture.md) → *Transport*.

## Scope — concrete targets

### v1 — ~5 boards, heterogeneous
Not a homogeneous fleet: **five different projects, five different builds**, one board each. Group targeting matters far less than per-device targeting and per-device capability checking.

| Board | Character | What it exercises |
|---|---|---|
| Morse-code blinker | trivial, disposable | R2's *deliberately broken build* — bricking it costs nothing |
| E-paper frame | battery, deep-sleep, wakes to refresh | derived presence, commands queued for next wake |
| Simple vehicle | **safety-critical, in motion** | update deferral — must not reboot while moving |
| Simple drone | **safety-critical, airborne** | update deferral; confirm/rollback only on the ground |
| … | | |

**Consequence — device-side update deferral is a v1 requirement.** A frame can reboot whenever; a drone rebooting mid-flight falls out of the sky. The device, not the server, owns the decision of *when* to apply, and may defer indefinitely while reporting honestly. Auto-rollback inherits the same rule — a rollback reboot is still a reboot. This is a state-machine shape, cheap to design in and expensive to retrofit.

**Consequence — sleepy is a v1 case, not aspirational.** The e-paper frame sleeps between refreshes, so derived presence and commands queued for next contact are needed in v1. The presence model cannot assume a live socket.

### V2 — robotic swarm
**One ground vehicle acting as gateway, plus a large number of flying drones** — a *mobile, in-fleet* node that is simultaneously a managed device and the parent of its drones. Two properties shape it more than radio choice does: **hierarchy** (the server tracks each drone individually but reaches it only via its parent) and **disconnected operation** (the vehicle will be out of internet range in the field).

Full treatment in [features/groups-deploy.md](../docs/features/groups-deploy.md).

### Beyond
A 100+ node low-power swarm reaches past Wi-Fi's ceiling; **Thread** is the answer, and **delta updates** plus airtime-aware scheduling are what make full-image OTA affordable on it. See [roadmap.md](../docs/roadmap.md) → *Beyond*.

## Product surface
- **Server:** control plane + MQTT broker + web dashboard. Ships as one Docker Compose stack; opinionated defaults, TLS handled for you — onboarding friction is the make-or-break risk, so treat it as a feature.
- **Device side:** (a) a **prebuilt agent** to flash for instant wow, and (b) a **thin OTA library** (ESP-IDF/Arduino) to embed in custom firmware.
- **Dashboard:** fleet inventory + live health (online/offline, firmware version, last-seen, boot-success) + a few user-defined telemetry metrics + push/rollback controls.

**Client constraint:** the in-browser flasher uses **Web Serial — Chromium-based browsers only** (Chrome/Edge), over HTTPS or `localhost`, with one mandatory click to grant the port. Firefox and Safari users cannot onboard a board in v1; there is no fallback until the post-v1 CLI flasher. This is a real limit on the primary onboarding path and is accepted knowingly.

## How it works — the deployment funnel
Every deployment passes gates; each catches what the previous can't (**defense in depth**). **v1 implements only the last one** — at ~5 boards a bad build costs one board a reboot, so per-device auto-rollback carries the whole load; the earlier gates arrive as the fleet and the automation grow:

1. **Simulate (pre-flight, advisory) — V2.** Boot the artifact in an emulator and run the user's self-test. Warns on failure; user may override. Catches boots-but-crashes bugs at **zero device cost**. Earns its keep once builds arrive automatically from CI and nobody is watching each one.
2. **Deliver.** The device pulls the update, verifies **checksum + signature**, applies to a secondary A/B slot, and reboots in a window it declares safe. Control and payload travel on **separate channels** — the command path never carries firmware bytes. Mechanism in [design/architecture.md](../design/architecture.md) → *Transport*.
3. **Confirm (per-device).** New firmware must **reconnect within a timeout** (and pass an **optional custom self-test**) — else **auto-rollback** to last-known-good. Any custom self-test is reused as the sim gate in step 1: *write once, gate in sim, confirm on device.*

## Requirements & targets

> **PROPOSED** — these are the first numbers written down for this project. Correct any that are wrong; downstream docs (heartbeat interval, presence tolerance, upload validation) resolve against this table rather than deciding for themselves.

### Capacity
| | v1 target |
|---|---|
| Devices per instance | **25** (5 in use, headroom for experiments) |
| Concurrent deploys | **5** — every v1 board at once |
| Artifact size | **≤ 1.9 MB**, rejected at upload above the target's `ota_slot_size` |
| Stored versions per platform | **20**, oldest pruned; the running and previous version are never pruned |

### Timing
| | v1 target |
|---|---|
| Heartbeat interval (always_on) | **60 s** |
| Heartbeat (sleepy) | one per wake |
| Offline shows in dashboard — always_on | **≤ 60 s** (Last Will, near-immediate) |
| Offline shows in dashboard — sleepy | `2.5 × expected_wake_interval_s` since `last_seen` |
| Full deploy, healthy Wi-Fi (`stage` → `confirmed`) | **≤ 5 min** for a 1.5 MB image |
| Full deploy, degraded link | **≤ 15 min**, or fail cleanly with the old version still running |
| Confirm timeout (device-armed, default) | **300 s** |
| Dashboard reflects a device state change | **≤ 2 s** from server receipt |

*Deploy duration excludes time spent in `awaiting_safe_window`, which is unbounded by design — a parked drone may wait days.*

### Retention
| Data | Kept |
|---|---|
| `deploy_events` (KPI source) | **forever** — the metric history is the product's evidence |
| Telemetry samples | **30 days** |
| Device logs (`up/log`) | **7 days** |
| Revoked/expired tokens | **90 days**, then purged |

### Success criteria
Both metrics are recorded from R1 and surfaced in the dashboard at R6.

- **Delivery success** = healthy AND running the intended new version (rollback counts as a *miss*; drives build quality + simulation). **Target ≥ 90%.**
- **Fleet safety** = device ends healthy on *some* version (rollback counts as a *save*; drives robustness). **Target 100% — a bricked board is a product failure, not a missed percentage.**

Two numbers so neither is gamed at the other's expense; the gap between them tells you *which stage* to fix.

### v1 is done when
All five boards are enrolled and visible; a **deliberately broken build** pushed to the morse blinker recovers itself unattended; a signed artifact deploys to the drone only once it is on the ground; and both KPIs are visible in the dashboard with real history behind them.

## Security & data posture

What the operator is promised, in v1 terms. Mechanism in [design/architecture.md](../design/architecture.md) → *API surface & authentication*.

- **One admin account**, password-authenticated, credential stored hashed. No multi-user, no RBAC, no MFA in v1.
- **A device credential grants only that device.** Broker authz is a per-device pattern ACL, so a stolen or extracted credential lets an attacker impersonate exactly one board — publish its telemetry, receive its commands — and nothing else. It grants no admin access and no access to artifacts beyond signed URLs already issued to it.
- **An enrollment token is single-use and short-lived** (**PROPOSED: 24 h**), group-scoped and revocable. It cannot be replayed from a recovered board.
- **Physical access to a board exposes its credentials.** Flash encryption is off in v1 (see design/architecture.md → *Flash-time immutables*), so NVS is readable over USB. The mitigation is scope, not secrecy: the credential is worth one device, and it is revocable from the dashboard.
- **Artifacts are served from a public endpoint** authorized by short-lived signed URLs, not by a bearer token embedded in a command.
- **Fleet data stays under the operator's control.** No telemetry or device inventory is transmitted to any third party. Retention limits are the table above. *One exception, stated plainly:* v1 stores **artifact bytes in Google Cloud Storage** rather than on the instance — a cloud dependency accepted while v1 is a single hosted instance, and removed by the MinIO backend that arrives with V2 self-hosting. Everything else lives in the instance's own database. See [architecture.md](../design/production.md) → *Artifact storage*.

## Explicitly out of scope (v1)

**Product boundary — Fleetforge manages *firmware versions*, not devices.** It does not provide remote shell, general device configuration management, application-level provisioning, or a data pipeline for telemetry. The `cfg` channel exists to tune the agent (intervals, thresholds) and deliberately stops there: pushing application settings through it would make Fleetforge a config-management product with a firmware feature, which is the opposite of the intent.

Also out: develop/build/debug pillar *(server-side compile arrives in V2)* · **advisory simulation gate** *(V2)* · **groups & bulk deploy** *(V3 — five heterogeneous boards have nothing to bulk-deploy)* · staged/canary rollouts · Raspberry Pi & FPGA implementations · turnkey self-hosting · multi-tenant/orgs & public signup · rich observability & alerting · non-Chromium browser onboarding.

## Roadmap (beyond v1)
- **V2 — source to artifact:** VCS ingestion with provenance, server-side compilation, the advisory simulation gate, deploy policy.
- **V3 — robotic swarm:** gateway/edge relay + many drones, hierarchy, groups & bulk deploy, delta updates.
- **Interaction surfaces:** the core is **API-first/headless from v1** (dashboard = first client); priority integrations are **Home Assistant** and **CLI**, later **Claude Code / MCP**.

Detail and ordering in [roadmap.md](../docs/roadmap.md) and [releases.md](../docs/releases.md).

## Key risks (v1)

1. **Onboarding friction** — mitigated in v1 by hosting the server ourselves (real TLS, nothing for the user to install) + prebuilt agent. *Deferred, not solved:* the self-host onboarding problem returns in full at V2.
2. **Public exposure** — hosting the broker + artifact endpoint on the open internet makes auth load-bearing from R0: per-device broker credentials, topic ACLs, single-use enrollment tokens. There is no LAN perimeter to fall back on.
3. **Bad pushes on flaky links** — mitigate with resumable range download, integrity check, atomic A/B apply, mandatory rollback handshake.
4. **Flash-time immutables** — the partition table, the bootloader's rollback support and eFuse posture cannot be changed by OTA. Getting them wrong at R0 means a physical recall of every deployed board. Mitigate by freezing the full A/B-capable layout at R0, before any device ships. See [design/architecture.md](../design/architecture.md) → *Flash-time immutables*.
5. **One layer of defense** — v1 relies on per-device auto-rollback alone; simulation (V2) and canary (later) do not exist yet. A deliberate bet that holds at 5 boards and stops holding when builds arrive unattended from CI.
6. **Browser lock-in on the onboarding path** — Web Serial is Chromium-only, and it is the *only* v1 way to flash a board. Mitigate by keeping the flasher a client of the same public API, so the post-v1 CLI flasher is an alternative rather than a rewrite.
7. **Wrong abstraction guess** for Pi/FPGA — mitigate by keeping the contract minimal and opaque (see design/architecture.md).

*Growth-stage risks — scale ceiling, simulation fidelity, sim-engine licensing — are tracked in [roadmap.md](../docs/roadmap.md).*
