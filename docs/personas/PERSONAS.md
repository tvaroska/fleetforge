# Personas — Users of Fleetforge

**Last Updated:** 2026-09-21
**Purpose:** Canonical reference for who uses Fleetforge, what constraints they face,
and what makes or breaks the product for them.
**Companions:** Detailed reviews and technical roadmaps in [`docs/HOBBYIST.md`](../HOBBYIST.md),
[`docs/SWARM.md`](../SWARM.md), and [`docs/PLATFORMS.md`](../PLATFORMS.md).

---

## 1. Electronics Hobbyist / Solo Maker ("Alex")

> *"I spent three weekends building an automated chicken coop and a garden moisture
> array. Now they are sealed in IP67 boxes across my property. I want to tweak the
> sensor timing without crawling under the porch with a laptop and a micro-USB cable."*

### Context & Scale
- **Fleet:** 3 to 15 boards (heterogeneous: e-paper display, garden sensors, LED matrix,
  coop door controller).
- **Environment:** Home Wi-Fi (single SSID/PSK), residential router, sporadic dead spots.
- **Hardware:** ESP32-WROOM devkits, Xiao C3/S3, classic NodeMCU boards.
- **Workflow:** Writes firmware in **Arduino IDE** or **PlatformIO**. Compiles locally
  on a Mac or Windows laptop. Does not use ESP-IDF directly.

### Core Goals
- Push a newly compiled `.bin` from the desk to an installed board in under 2 minutes.
- Absolute certainty that a buggy loop or null pointer will not brick the board,
  forcing physical retrieval and disassembly.
- Easily change Wi-Fi credentials when moving a project from the desk to the shed.

### Pain Points & Blockers
- **The "Demo Agent" Trap:** Cannot use Fleetforge if it requires running the stock C agent;
  needs an embeddable Arduino/PlatformIO library that slips into existing `setup()` and `loop()`.
- **Partition Wipeout:** Flashing via Arduino IDE over USB frequently overwrites custom
  partition tables unless a board definition or custom `partitions.csv` is packaged for them.
- **Sleepy Node Rollback:** Battery-powered sensors that wake, sample, report, and sleep
  in 3 seconds get falsely rolled back by a naive 300-second wall-clock confirm timer.
- **Local Self-Hosting TLS Wall:** Running Docker Compose on a local Raspberry Pi (`192.168.1.50`)
  breaks Web Serial browser security (Chrome requires `localhost` or valid public HTTPS).

### Make-or-Break Features
1. **Arduino / PlatformIO library** with drop-in examples and a safe partition template.
2. **Device-side auto-rollback (R2) + Safe Mode** (fallback boot when both slots fail).
3. **Improv-Wi-Fi (BLE/Serial)** to re-provision credentials without USB.

---

## 2. Robotic Swarm Operator ("Elena")

> *"We operate five ground rovers and twenty scouting drones in field trials where
> there is zero cell reception. When we find an obstacle-avoidance bug at the test site,
> we need to stage the patch to all 25 units from the field truck and apply it only
> when every airframe is safely on the ground."*

### Context & Scale
- **Fleet:** 10 to 50+ mixed nodes (Linux SBC ground vehicles + ESP32/STM32 flight
  controllers and telemetry radios).
- **Environment:** Off-grid, RF-congested, GPS/time-denied valleys, vehicle-hosted Wi-Fi AP.
- **Hardware:** Raspberry Pi / Jetson (ground gateway) + ESP32-S3/C6 (radios) + STM32 (flight controllers).
- **Workflow:** Multi-developer Git repo, CI-generated builds, coordinated field missions
  using MAVLink, ROS 2, or Zenoh.

### Core Goals
- Safe, airtime-budgeted fleet updates in fully disconnected field conditions.
- Zero risk of an airborne drone rebooting mid-flight or rolling back during a mission.
- Fast delta updates that don't saturate the mission radio link during operations.

### Pain Points & Blockers
- **Hosted Cloud Dependency:** A server that relies on Google Cloud Storage or public Let's
  Encrypt certificates is useless in a field with no internet.
- **Simultaneous Reboot Crashes:** Group deploy that triggers simultaneous reboots will
  drop flying assets out of the sky or cause radio blackouts.
- **Clock Initialization Failure:** Devices that boot in the field with no NTP server
  fail TLS certificate validity checks before they can even connect to the local broker.
- **Radio Saturation:** Pushing 1.5 MB uncompressed images to 30 drones over an 802.11 or
  802.15.4 mesh starves flight telemetry and MAVLink heartbeats.

### Make-or-Break Features
1. **Disconnected Field Gateway:** Local broker + artifact cache + time source running
   on the ground vehicle SBC, syncing upstream only when back in the hangar.
2. **Coordinated Apply & Canary Gating:** `stage-in-air, apply-on-ground` state machine,
   canary rollouts, and version-skew visibility.
3. **Multi-Platform Support & Deltas:** Raspberry Pi vehicle adapter + 50 KB delta updates.

---

## 3. Other High-Value Personas for Fleetforge

Looking at Fleetforge’s architecture (two thin waists, opaque versioned artifacts,
device-owned reboots, headless API, strict topic ACLs), three additional personas
naturally fit the system without distorting its core boundaries:

```
                  ┌─────────────────────────────────────────┐
                  │          FLEETFORGE CORE API            │
                  │   Opaque Artifacts · 4-Verb Contract    │
                  └────────────────────┬────────────────────┘
                                       │
         ┌─────────────────────────────┼─────────────────────────────┐
         │                             │                             │
┌────────▼────────┐           ┌────────▼────────┐           ┌────────▼────────┐
│  1. Alex        │           │  2. Elena       │           │  3. Marcus      │
│  The Hobbyist   │           │  The Swarm Op   │           │  Commercial OEM │
│  (Bench → Shed) │           │  (Air & Ground) │           │  (AgTech / HVAC)│
└─────────────────┘           └─────────────────┘           └─────────────────┘
                                       │
                      ┌────────────────┴────────────────┐
                      │                                 │
             ┌────────▼────────┐               ┌────────▼────────┐
             │  4. DevSecOps   │               │  5. Hardware    │
             │  Platform Eng   │               │  Test Engineer  │
             │  (Enterprise/CI)│               │  (HIL Testbed)  │
             └─────────────────┘               └─────────────────┘
```

---

### 3. Commercial Hardware OEM / Small IoT Team ("Marcus")
*Building 50 to 500 connected commercial units (e.g., smart agriculture sensors, brewery monitors, HVAC controllers).*

- **Profile:** Small startup (2–5 engineers). Sells physical products to non-technical customers.
  Devices are scattered across customer sites with varying firewall rules.
- **Why Fleetforge fits:** They don't have the runway to build a custom OTA backend, but they
  refuse to pay $2–$5/device/month to vendor clouds (Particle, Balena, AWS IoT Device Management)
  which erodes their hardware margins.
- **Key Needs:**
  - Single-tenant hosted or turnkey self-hosted cloud instance.
  - Granular deployment rings (Alpha / Customer Beta / Production Fleet).
  - Cryptographic artifact signing (R5) so compromised endpoints cannot flash rogue binaries.
  - Zero-touch factory provisioning: flashing an enrollment token at assembly, shipping
    the box to a customer who plugs it in, and having it auto-register.
- **Distinction from Hobbyist:** Cannot accept Chromium-only Web Serial flashing; needs a
  **batch CLI flasher** for the assembly bench and an audit log of who deployed what version.

---

### 4. Embedded CI / DevSecOps Engineer ("Siddharth")
*Automating the path from `git push` to test fleets, enforcing supply chain security and traceability.*

- **Profile:** Software engineer in a mid-sized team managing 10+ hardware variants. Focuses on
  continuous integration, reproducible builds, and security posture.
- **Why Fleetforge fits:** Fleetforge's **API-first / headless core** and opaque artifact model
  allow them to bypass web dashboards entirely and drive everything through GitHub Actions,
  GitLab CI, and automated scripts.
- **Key Needs:**
  - Provenance tracking (linking a deployed binary to commit SHA, build container digest,
    and CI run ID — planned for V2 / R6).
  - Headless CLI / REST API authentication via long-lived, scoped machine tokens (not cookies).
  - Pre-flight simulation gate (R8) running Espressif QEMU in headless CI to reject crashing
    builds before touching hardware benches.
  - Software Bill of Materials (SBOM) and artifact signature verification.
- **Distinction from Swarm Operator:** Doesn't care about field gateways or airframe deferral;
  cares about automated gating, branch-to-fleet deployment policies, and reproducible toolchains.

---

### 5. Hardware-in-the-Loop (HIL) Test Lab Engineer ("Sarah")
*Running automated regression suites across physical racks of development boards.*

- **Profile:** Test / QA engineer maintaining a physical testbed of 20–100 mixed boards
  (ESP32, ESP32-S3, ESP32-C6, STM32) wired to relay boards, logic analyzers, and power monitors.
- **Why Fleetforge fits:** Flashing devkits via physical USB hubs in a server rack frequently
  fails due to USB hub resets, serial port enumeration drops, and OS kernel lockups.
  OTA updates over Wi-Fi/Ethernet are significantly faster and more reliable for daily test runs.
- **Key Needs:**
  - High-throughput OTA updates (multiple test builds flashed per hour).
  - Custom confirm hook (R4 self-test): Run an automated regression suite on the board;
    if tests fail, auto-rollback and report failure back to the test harness.
  - Device health telemetry: Free heap, crash counts, assert logs, and reset reasons.
  - Fast forced reboot / re-flash endpoints to recover hung boards.
- **Distinction from OEM:** Devices are all on a controlled local network; physical damage
  to a board is annoying but not fatal. Speed of re-flashing and diagnostic reporting trumps
  battery sleep or safe-window deferral.

---

## 4. Persona Priority Matrix

| Capability / Need | Alex (Hobbyist) | Elena (Swarm) | Marcus (OEM) | Siddharth (CI/Ops)| Sarah (HIL Lab) |
|---|:---:|:---:|:---:|:---:|:---:|
| **Target Scale** | 3–15 | 10–50 | 50–500 | N/A (Pipelines)| 20–100 |
| **Embeddable Library** | **P0 (Blocker)**| **P0 (Blocker)**| **P0 (Blocker)**| P1 | P1 |
| **Auto-Rollback (R2)** | **P0** | **P0** | **P0** | P1 | **P0** |
| **Web Serial Flasher** | **P0** | P2 | P2 | P3 (Wants CLI)| P3 (Wants CLI) |
| **Batch CLI Onboarding**| P2 | P1 | **P0** | **P0** | **P0** |
| **Disconnected Gateway**| P3 | **P0 (Blocker)**| P3 | P3 | P2 |
| **Group / Ring Deploy** | P2 | **P0** | **P0** | **P0** | P1 |
| **Safe-Window Deferral**| P1 (Sleepy) | **P0 (Flight)** | P1 (In-use) | P3 | P3 |
| **Delta Updates** | P2 | **P0** | P1 (Cellular) | P3 | P2 |
| **CI / Provenance (V2)** | P3 | P1 | **P0** | **P0 (Blocker)**| P1 |
| **Custom Confirm (R4)** | P1 | **P0** | **P0** | P1 | **P0** |

### Strategic Recommendation for Fleetforge

1. **V1 Horizon (Current):** Focus exclusively on **Alex (Hobbyist)** and the shared foundation
   with **Sarah (HIL Lab)**. The embeddable library (R1.5) and rock-solid auto-rollback (R2)
   are the common denominators that unlock real usage on hardware.
2. **V2 Horizon:** Attracts **Marcus (OEM)** and **Siddharth (CI/Ops)** through provenance (R6),
   CLI automation (R7), and simulation gating (R8).
3. **V3 Horizon:** Delivers the disconnected gateway, airtime scheduling, and hierarchy required
   by **Elena (Swarm Operator)**.
