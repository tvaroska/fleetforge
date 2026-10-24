# Personas — Users of Fleetforge

**Last Updated:** 2026-10-24
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
- **Fleet:** 3 to 15 boards (heterogeneous: e-paper show, garden sensors, LED matrix,
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
- **The "Demo Agent" Trap:** Cannot use Fleetforge if it requires running the stock C agent.
  Needs an embeddable Arduino/PlatformIO library that slips into existing `setup()` and `loop()`.
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
- Fast delta updates that do not saturate the mission radio link during operations.

### Pain Points & Blockers
- **Hosted Cloud Dependency:** A server that relies on Google Cloud Storage or public Let us
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
device-owned reboots, headless API, strict topic ACLs), four additional personas
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
          ┌─────────────────────────────┼─────────────────────────────┐
          │                             │                             │
 ┌────────▼────────┐           ┌────────▼────────┐           ┌────────▼────────┐
 │  4. DevSecOps   │           │  5. Hardware    │           │  6. Chloe       │
 │  Platform Eng   │           │  Test Engineer  │           │  Comp Pit / RF  │
 │  (Enterprise/CI)│           │  (HIL Testbed)  │           │  (LoRa+BLE+Dock)│
 └─────────────────┘           └─────────────────┘           └─────────────────┘
```

---

### 3. Commercial Hardware OEM / Small IoT Team ("Marcus")
*Building 50 to 500 connected commercial units (for example, smart agriculture sensors, brewery monitors, HVAC controllers).*

- **Profile:** Small startup (2–5 engineers). Sells physical products to non-technical customers.
  Devices are scattered across customer sites with varying firewall rules.
- **Why Fleetforge fits:** They do not have the runway to build a custom OTA backend, but they
  refuse to pay $2–$5/device/month to vendor clouds (Particle, Balena, AWS IoT Device Management)
  which erodes their hardware margins.
- **Key Needs:**
  - Single-tenant hosted or turnkey self-hosted cloud instance.
  - Granular deployment rings (Alpha / Customer Beta / Production Fleet).
  - Cryptographic artifact signing (R6) so compromised endpoints cannot flash rogue binaries.
  - Zero-touch factory provisioning: flashing an enrollment token at assembly, shipping
    the box to a customer who plugs it in, and having it auto-register.
- **Distinction from Hobbyist:** Cannot accept Chromium-only Web Serial flashing. Needs a
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
    and CI run ID — planned for V2 / R7).
  - Headless CLI / REST API authentication via long-lived, scoped machine tokens (not cookies).
  - Pre-flight simulation gate (R9) running Espressif QEMU in headless CI to reject crashing
    builds before touching hardware benches.
  - Software Bill of Materials (SBOM) and artifact signature verification.
- **Distinction from Swarm Operator:** Does not care about field gateways or airframe deferral.
  Cares about automated gating, branch-to-fleet deployment policies, and reproducible toolchains.

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
  - Custom confirm hook (R5 self-test): Run an automated regression suite on the board.
    If tests fail, auto-rollback and report failure back to the test harness.
  - Device health telemetry: Free heap, crash counts, assert logs, and reset reasons.
  - Fast forced reboot / re-flash endpoints to recover hung boards.
- **Distinction from OEM:** Devices are all on a controlled local network. Physical damage
  to a board is annoying but not fatal. Speed of re-flashing and diagnostic reporting trumps
  battery sleep or safe-window deferral.

---

### 6. Competition Pit & Contested-Venue Telemetry Engineer ("Chloe")
*Operating a small, mission-critical `ESP32-S3 + 915 MHz LoRa + BLE` pit/stands/cart mesh (`FLUX-10413-Hub`) that develops over Wi-Fi in the shop or hotel, then operates under a strict `802.11` Wi-Fi radio ban inside competition arenas.*

> *"In the shop and at the hotel, our three `ESP32-S3 + SX1262 915 MHz LoRa` nodes
> (`Pit Node`, `Robot Cart Node`, `Scouting Chief Node`) pull firmware, rotating
> `AES-256-GCM` keys, and battery/schedule lookup tables from Fleetforge over Wi-Fi.
> The moment we walk into the competition arena, `802.11` Wi-Fi is strictly banned
> by event rules (`E301`), our BLE GATT stack is busy serving scouters' phones, and
> any mid-event firmware or `data_partition` patch has to flow over the magnetic
> breakaway USB-C dock cable from the pit laptop."*

### Context & Scale
- **Fleet:** 3 to 6 specialized `ESP32-S3` nodes in one project (`flux-10413-hub`), grouped by role (`pit`, `cart`, `scouting-chief`).
- **Environment:** Dual-regime:
  1. **Shop / Hotel (Online):** Normal Wi-Fi (`ff_cfg` / `ff_net`) connected to `bingo.tvaroska.sk` with Git-driven CI/CD (`cloudbuild.yaml` $\to$ `fleetforge push`), secret rotation (`GET /v1/device-secrets`), and OTLP log export.
  2. **Competition Venue (EMCON / Zero `802.11` Wi-Fi):** Event rules (`E301`) strictly prohibit any `802.11` Wi-Fi or `ESP-NOW` transmission (`esp_wifi_stop()`), and `G303`/`R905` prohibit wireless dongles inside the field alliance station. Inside the arena, nodes communicate exclusively via **Sub-GHz `915 MHz LoRa` (`SX1262`)**, authenticated **BLE (`AES-256-GCM`)** to scouter phones, **CAN 2.0 (`SN65HVD230`)** to battery fuel gauges, and a **magnetic breakaway USB-C cable (`link_type: "dock"`)** to the Driver Station / Pit laptop (`FluxTray.jar`).
- **Hardware:** `ESP32-S3` (4 MB / 8 MB flash, `ab-4m-v1`) + `SX1262 915 MHz LoRa` + OLED/e-Paper + `SN65HVD230` CAN transceiver.

### Core Goals
- **One OTA, Secret & Crash-Recovery Stack:** Reuse Fleetforge's `ab-4m-v1` partition layout, ECDSA P-256 signature verification, 3-crash `safe_mode`, `RTC_NOINIT_ATTR` `[prev-boot]` crash log ring, and NVS `ff_sec` secret storage instead of writing bespoke OTA or key-provisioning code.
- **Hard `802.11` Venue Radio Lockout + Shared NimBLE Coexistence:** Guarantee that in Venue Mode (`ff_set_offline_mode(true)`), `libfleetforge` never turns on the `802.11` Wi-Fi radio and never hijacks the NimBLE GATT server for 120 s Improv Wi-Fi advertising while scouters' phones are syncing over BLE.
- **USB-C Dock (`link_type: "dock"`) Pit Updates & Log Drain:** When Wi-Fi is banned at the venue, push a Fleetforge-signed `.bin` or `artifact_type=data_partition` cargo bundle (`batteries.csv` + match schedule) over the magnetic USB-C cable from the pit laptop into the node's A/B slot—preserving full ECDSA verification, `ff_selftest` (`SX1262` SPI + BLE + CAN check), and automatic rollback—while draining buffered `up/log` and `up/telemetry` (`age_ms`) rings.
- **LoRa Mesh Peer Version & Health Visibility:** Surface the bleacher node's firmware version (`sub_versions`) and battery/RSSI telemetry through the tethered Pit/Cart node so the pit crew spots version skew or a low power bank before a match.

### Pain Points & Blockers (Addressed by `CUJ-3`)
- **Unconditional Wi-Fi & Improv BLE Startup in `ff_net_wifi.c` (Solved by `ff_set_offline_mode`):** Calling `fleetforge_start()` without offline mode starts `esp_wifi_start()` and, after association failure in a Wi-Fi-banned arena, opens the 120 s Improv Wi-Fi BLE GATT service (`00467768-6228-2272-4663-277478268000`). Calling `ff_set_offline_mode(true)` / `Fleetforge.setOfflineMode(true)` hard-stops `esp_wifi`, disables Improv BLE advertising, and checks `ble_hs_synced()` so the application's own NimBLE GATT stack runs undisturbed, while `ff_mqtt_confirm_offline()` confirms `ESP_OTA_IMG_PENDING_VERIFY` images locally via `ff_selftest`.
- **Tethered / Dock (`link_type: "dock"`) Stage & Drain Path (Solved by `ff_ota_dock_stage` & `dock-sync`):** At the venue with Wi-Fi disabled, the pit laptop connected over USB-C feeds a signed Fleetforge `stage` transaction into `ff_ota_dock_stage()`, drains the node's offline `ff_log_drain_json()` / `ff_telemetry_drain_json()` ring buffers, and replays the bundle to `POST /v1/devices/{device_id}/dock-sync` (`fleetforge dock-sync`) when back on hotel/shop Wi-Fi.
- **Rebooting During Queue / BLE Sync (Solved by `ff_safe_window`):** A node defers OTA reboots (`ff_safe_window_set(false)` / callback) while a scouter phone is mid-BLE transfer or while the Robot Cart USB-C link is actively streaming queue updates before a match.

### Make-or-Break Features
1. **Venue / EMCON Offline Radio Mode & NimBLE Coexistence (`ff_set_offline_mode` + shared NimBLE host):** Allows firmware (via hardware switch, USB-C command, or `dn/cfg`) to hard-disable `802.11` Wi-Fi and Improv BLE while keeping `ff_get_secret()`, `ff_logf()`, `ff_telemetry()`, `ff_selftest`, and `ff_safe_window` active alongside the app's own NimBLE GATT services.
2. **USB-C Dock Transport (`link_type: "dock"`) for Signed A/B OTA, `data_partition` Cargo & Offline Ring Drain:** Lets a host companion (`FluxTray.jar` or `fleetforge` CLI over USB-CDC serial) stream a signed `.bin` or `data_partition` cargo (`batteries.csv` + match schedule) directly into `ff_ota` and drain buffered `up/log` + `up/telemetry` (`age_ms`) samples without Wi-Fi.
3. **Hardware Peripheral Self-Test (`ff_selftest`), Safe-Window Interlock (`ff_safe_window`) & Peer `sub_versions` Reporting:** Holds `confirmed` until `SX1262` LoRa SPI, BLE GATT, and CAN transceiver self-tests pass; defers reboots while BLE/LoRa/USB-C queue traffic is active; and reports peer mesh versions (`sub_versions: {"cart_node": "...", "chief_node": "...", "batteries_csv": "..."}`).

---

## 4. Persona Priority Matrix

| Capability / Need | Alex (Hobbyist) | Elena (Swarm) | Marcus (OEM) | Siddharth (CI/Ops)| Sarah (HIL Lab) | Chloe (Comp Pit / RF) |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| **Target Scale** | 3–15 | 10–50 | 50–500 | N/A (Pipelines)| 20–100 | 3–6 (Mesh) |
| **Embeddable Library** | **P0 (Blocker)**| **P0 (Blocker)**| **P0 (Blocker)**| P1 | P1 | **P0 (Blocker)** |
| **Auto-Rollback (R2)** | **P0** | **P0** | **P0** | P1 | **P0** | **P0** |
| **Web Serial Flasher** | **P0** | P2 | P2 | P3 (Wants CLI)| P3 (Wants CLI) | P1 |
| **Batch CLI Onboarding**| P2 | P1 | **P0** | **P0** | **P0** | P2 |
| **Venue Offline Mode & Shared BLE** | P3 | P1 (EMCON) | P2 | P3 | P3 | **P0 (Blocker)** |
| **USB-C Dock OTA & Log Drain (`dock`)** | P3 | **P0 (Deck)** | P2 | P2 | P1 | **P0 (Blocker)** |
| **Rotating Secrets (`ff_sec`)** | P1 | P1 | **P0** | **P0** | P2 | **P0 (`AES-256-GCM`)** |
| **`data_partition` Cargo & `sub_versions`** | P2 | **P0 (Co-proc)**| **P0** | P1 | P1 | **P0 (CSVs / Mesh)** |
| **Disconnected Gateway**| P3 | **P0 (Blocker)**| P3 | P3 | P2 | P1 (Laptop USB-C) |
| **Group / Ring Deploy** | P2 | **P0** | **P0** | **P0** | P1 | **P0 (Canary 1-of-3)**|
| **Safe-Window Deferral**| P1 (Sleepy) | **P0 (Flight)** | P1 (In-use) | P3 | P3 | **P0 (Queue/BLE)** |
| **Delta Updates** | P2 | **P0** | P1 (Cellular) | P3 | P2 | P2 |
| **CI / Provenance (V2)** | P3 | P1 | **P0** | **P0 (Blocker)**| P1 | **P0** |
| **Custom Confirm (R5)** | P1 | **P0** | **P0** | P1 | **P0** | **P0 (LoRa/CAN SPI)** |

### Strategic Recommendation for Fleetforge

1. **V1 Horizon (Current):** Focus on **Alex (Hobbyist)**, **Sarah (HIL Lab)**, and the online shop/hotel loop of **Chloe (Comp Pit / RF)**. The embeddable library (R3), auto-rollback (R2), custom self-test & safe window (R5), ECDSA signing (R6), rotating secrets (`SEC-1`), and `data_partition` cargo (`0013_artifact_type`) are the common foundation.
2. **V2 Horizon:** Attracts **Marcus (OEM)**, **Siddharth (CI/Ops)**, and **Chloe (Comp Pit / RF)** through provenance & auto-deploy (`CI-CD-1`), venue radio lockout + NimBLE coexistence (`ff_set_offline_mode`), and tethered USB-C dock (`link_type: "dock"`) stage & log drain.
3. **V3 Horizon:** Delivers the disconnected gateway, airtime scheduling, and hierarchy required by **Elena (Swarm Operator)** and **Chloe (Comp Pit / RF)**.
