# Fleetforge — Critical User Journeys

THE WHAT, told as a person's day rather than a system's behaviour. Status-free by
construction: no counts, no progress, no dates. Live task state lives only in `TODO.md`;
as-built design lives in `design/`.

Protected. `/implement` proposes changes here, it does not make them.

Every feature should support at least one journey below, and every journey should be
walkable by a real person without guidance. A CUJ that only an engineer who has read this
repo could complete has already failed.

Each CUJ carries, on top of Description / Steps / Success Criteria / Supported By, the two
fields the verification ladder's T3 tier consumes:

- **Driver** — how the journey is played end to end.
- **Judge** — how a run is scored: deterministic must-pass assertions plus hard-fail
  traps, and the rubric handed to the external LLM judge (`jeep`) against the Success
  Criteria. **Both judges must pass.**

**On segmented Drivers.** A journey may cross several releases, in which case no single
harness can play it yet. Such a CUJ names a harness **per segment** and declares which
segments are gradeable; a segment with no harness is not graded and is not a pass either.
This is a deliberate departure from a single `Driver:` command — the alternative is a CUJ
that is ungradeable in full until its last release lands, which makes T3 inert for
everything it already covers.

---

## CUJ-1: A sketch on the desk becomes a board in the field that fixes itself

**Persona:** Alex, the solo maker (`docs/personas/PERSONAS.md` §1). Writes firmware in the
Arduino IDE or PlatformIO on a laptop, does not use ESP-IDF, and has three to fifteen
boards — most of them already sealed in boxes across a property.

**Description:** Alex has written a sketch that does something they care about, and it
works on a DevKit on the desk. They want that board to go live somewhere awkward and
still be updatable — without adopting Fleetforge's prebuilt agent, without porting the
project to ESP-IDF, and without ever again crawling under a porch with a USB cable.

This is the journey the product exists for. Everything else Fleetforge does is in service
of it: the prebuilt agent is the demo of this, not the substitute for it.

**Steps:**

1. Alex has a working sketch on a DevKit, plugged into the laptop.
2. They add the Fleetforge library to the sketch — an include and a few lines in
   `setup()` and `loop()` — and it compiles in the IDE they already have open.
3. They flash the board once over USB and enter their Wi-Fi details. The board appears in
   the fleet list, online, reporting the version they just built.
4. Alex unplugs it and installs it wherever it is going to live.
5. They change something visible in the sketch, build, and upload that `.bin` from the
   dashboard. The board takes the update and starts doing the new thing; the version in
   the list is the one they uploaded.
6. Later they deploy a build that is broken. The board recovers on its own, comes back on
   the version that worked, and says so. Alex does not get up.

**Success Criteria:**

- Alex never reads a raw UART log — not on the happy path, and not on any failure path.
  When something goes wrong, the product names the cause in plain language and gives a
  next action.
- Alex never opens ESP-IDF, a serial monitor, or the Fleetforge source.
- No board is ever physically retrieved. A bad push comes back by itself; that is the
  whole claim, and a brick is a product failure rather than a missed percentage
  (`prd.md` → *Success criteria*, Fleet safety = 100%).
- A deploy over a healthy link finishes within **5 min** for a 1.5 MB image, and the
  dashboard reflects a state change within **2 s** of the server receiving it
  (`prd.md` → *Requirements & targets*).
- The board that ends up in the field is as recoverable as one running the prebuilt
  agent — same A/B slots, same rollback-enabled bootloader, same device-armed confirm.
  A configuration that cannot roll back fails at build or at enroll; it does not warn.
- The worked example Alex copied from is short enough to read in one screen.

**Supported By:**

- `standards.md` → *ota-library* → **Embeddable OTA** — steps 2, 5 and 6: the library,
  its safety posture, and honest capability reporting.
- `standards.md` → *enrollment* → **Unaided onboarding** — step 3: flash to green in the
  fleet list, with no UART log in any path.
- `standards.md` → *dashboard* → **Getting firmware in is a dashboard operation** —
  step 5's first half. Alex uploads the `.bin` their IDE just built from the dashboard's
  upload form (`flows.md` Flow 2).
- `spec/device-protocol.md` — the wire contract both the library and the agent speak.
- Releases: R0 (enroll) · R1 (OTA transport) · R2 (auto-rollback) · R3 (the library
  itself). See `docs/releases.md`.

**Driver:** segmented — this journey crosses four releases. Each segment names the harness
that plays it and whether it is graded; a segment whose harness does not exist yet is
**not graded**, and neither is a half that names a procedure rather than a command.

| Steps | Segment | Harness | Graded |
|---|---|---|---|
| 1–2 | Sketch compiles with the library added | `just lib-quickstart --build-only`: the worked example's README build steps, run verbatim in a clean copy of the tree — Arduino (PlatformIO) for esp32 and esp32s3 plus the edited second build, and ESP-IDF for esp32 and esp32s3 | yes |
| 3 | One flash → board on the fleet | `just lib-quickstart`, phase 2a (enroll): the example, flashed once in QEMU in place of the USB flash, enrolls and heartbeats against the dev stack and is online in the fleet list on the version it was built with; `pytest tests/test_enroll.py` for the `POST /v1/enroll` surface | yes |
| 5 | OTA a changed build → new version reported | On the device: `just lib-quickstart`, phase 2b (OTA): the edited build is uploaded, deployed and confirmed, and the board reports its version. Server and dashboard: `just update-e2e`, scenarios `upload-good` and `deploy-good` — the build is uploaded from the dashboard form, sent, and the result card says good | yes |
| 6 | A bad build recovers itself | On the device: `just lib-quickstart`, phase 2c (rollback): a build that never confirms is deployed, and the run requires `rolled_back` and the previous `fw_version` and fails on any `confirmed`. Server and dashboard: `just update-e2e`, scenario `deploy-broken` | yes |
| — | A wrong flash layout is refused, not flashed | Server and dashboard: `just update-e2e`, scenario `precheck-wrong-layout` — a refusal card with no Send, and a direct deploy is a 409 that sends nothing. On the device: `docs/runbooks/agent-qemu.md` → *A wrong flash layout is refused* (a procedure) | server and dashboard half |

One full `just lib-quickstart` run plays segments 1–2, 3, 5 and 6 on the device, in that
order, on one emulated board; the API must give that board `10.0.2.2` origins
(`docs/runbooks/agent-qemu.md` → *The worked example, end to end*). QEMU stands in for two
things a board does by itself: the one USB flash, and the reboot after an update (an
`on_command` apply and a power cycle). `just update-e2e` plays the dashboard against
simulated boards that report the versions they were told, so the `fw_version` assertions
of steps 5 and 6 are graded on the `lib-quickstart` run, never on it.

Step 4 (physical install) and the unaided half of step 3 are done by a person; no harness
plays them and T3 does not grade them.

**Judge:**

- *Deterministic (must-pass):*
  - After step 3, the device has a row in the fleet list and is online.
  - After step 5, the `fw_version` the device reports equals the version of the build that
    was uploaded — read from the running image itself (its app descriptor, or in a library
    build the version compiled into it and handed to the library at start), not from what
    it was told to install.
  - After step 6, the device reports `rolled_back` and its `fw_version` is the pre-deploy
    value.
  - A duplicated deploy command produces one download, not two.
- *Hard-fail traps (cap the score at 0):*
  - Any step required reading a raw UART log, or opening ESP-IDF or this repo.
  - Any board needed physical retrieval, or ended in a state it could not boot out of.
  - An enrolment token, Wi-Fi passphrase or MQTT credential appeared in any log, any
    diagnostic bundle, or any URL.
  - A build whose partition table disagreed with its announced `partition_layout` was
    flashed rather than refused.
  - A milestone was shown as reached while it was no longer true, or a wait had no
    deadline.
- *LLM-judge rubric (graded by `jeep` against the Success Criteria):*
  - Every failure the run encountered was explained in plain language and paired with a
    next action — not a log line, not an error code.
  - The steps Alex performed are the steps the documentation says to perform.
  - The worked example is readable in one screen and does not assume ESP-IDF knowledge.
  - Nothing in the run required knowledge only available from the Fleetforge source.

---

## CUJ-2: A sleepy e-ink project iterates from Git, streams OTel logs, rotates secrets, and switches Wi-Fi without a cable

**Persona:** Alex / Boris, building an e-ink display project (`epaper`) — starts on 1 bench
board with frequent code changes, then deploys to 2+ frames on walls across multiple Wi-Fi
networks.

**Description:** The e-ink frame wakes periodically to fetch data from a backend using an
API key, renders a screen, emits logs/metrics, and goes back to deep sleep. During active
development the operator wants `git push` (or switching a tracked branch/tag/commit in
project settings) to compile, test, and OTA-deploy to the board automatically without manual
web uploads. In the field, the frame must roam across known Wi-Fi networks or accept a new
Wi-Fi network over BLE without reflashing, forward its logs to an OpenTelemetry collector,
and pick up rotated backend API keys without reflashing.

**Steps:**

1. The operator creates the `epaper` project, sets the Continuous Deployment rule (`repo_url`,
   tracked `branch`/`tag`/`commit`, and `auto_deploy_target: dev` or `all`), and marks the
   bench board `stay_awake: true` so it does not sleep between code pushes.
2. They push a commit to the tracked branch (or switch the tracked ref in Project Settings).
   CI compiles the sketch, runs tests, and uploads the `.bin` with Git provenance
   (`branch`, `commit_sha`, `tag`, `build_url`, `test_passed=true`). Fleetforge matches the
   project's tracking rule and automatically deploys the build to the targeted board(s).
3. As the fleet grows to 2+ e-ink boards, switching `auto_deploy_target` to `all` (or
   clicking "Deploy to Project Fleet") rolls the verified commit out across every board in
   the project.
4. The sketch logs diagnostics via `Fleetforge.logf()` and metrics via `Fleetforge.metric()`
   before calling `Fleetforge.isReadyForSleep()`. The board drains its log and telemetry
   batches before sleeping, and `fleetforge-ingestor` exports them over OTLP/HTTP to the
   configured OpenTelemetry collector with `service.name="epaper"` and `device.id`.
5. The operator rotates `backend_api_key` in the project's Secrets settings. Fleetforge
   bumps `secrets_rev` on `dn/cfg`. On its next wake, each e-ink board sees
   `secrets_rev > local_rev`, pulls `GET /v1/device-secrets` over HTTPS using its per-device
   broker credential, stores the new key in NVS (`ff_sec`), and returns it from
   `Fleetforge.getSecret("backend_api_key", ...)` without reflashing.
6. When an e-ink frame is carried to a location whose Wi-Fi is not in `ff_cfg`, it opens a
   bounded 120 s Improv Wi-Fi over BLE window on wake, receives the new SSID + passphrase
   from Chrome or a phone, stores it in the NVS `ff_net` overlay, and reconnects without a
   USB cable or reflash.

**Success Criteria:**

- Code iteration requires zero manual `.bin` drag-and-drop: a push to the tracked branch/tag
  (or changing the tracked ref in Project Settings) lands on the target board(s)
  automatically once tests pass, and every running board links to its exact Git commit SHA.
- A sleepy e-ink board never loses logs or metrics emitted during a wake (`isReadyForSleep()`
  holds sleep until `up/log`, `up/telemetry`, and any active `secrets_rev` sync or OTA stage
  finish), and logs arrive at the OTLP/HTTP collector as valid OpenTelemetry `LogRecord`s.
- Secret values (`backend_api_key`) never appear in MQTT payloads, logs, or firmware binaries;
  they are pulled over HTTPS (`GET /v1/device-secrets`), cached in NVS across deep sleep,
  and reflected on the dashboard via `secrets_rev` in `up/announce`.
- Adding a new Wi-Fi network in the field requires neither USB reflashing nor server-stored
  Wi-Fi passphrases.

**Supported By:**

- `spec/flows.md` → Flow 2 (V2) & Flow 3
- `spec/device-protocol.md` → `up/log`, `up/telemetry`, `dn/cfg` (`stay_awake`, `secrets_rev`),
  and *Known networks* (`ff_net` NVS overlay + Improv BLE)
- Pillars: `CI-CD-1` (`R7`+`R8`+`R11`), `SEC-1`, `R4-fw-3/4` + `OTEL-1`, `WIFI-BLE-1`

---

## CUJ-3: A 3-node competition LoRa & BLE mesh updates at the shop, goes Wi-Fi-silent at the venue, takes tethered USB-C dock updates in the pit, and flushes field telemetry

**Persona:** Chloe, Competition Pit & Contested-Venue Telemetry Engineer (`docs/personas/PERSONAS.md` §3.6, `FLUX-10413-Hub`). Operates three `ESP32-S3 + SX1262 915 MHz LoRa` nodes (`Pit Node` on the battery cart with a CAN 2.0 transceiver, `Robot Cart Node` with a magnetic breakaway USB-C cable to the Driver Station laptop, and `Scouting Chief Node` in the bleachers syncing scouter phones over `AES-256-GCM` Web Bluetooth).

**Description:** In the shop and at the hotel, all three nodes connect over Wi-Fi to Fleetforge (`flux-10413-hub` project) to receive Canary-gated firmware rollouts, rotated `AES-256-GCM` mesh/BLE keys (`ff_sec`), and `artifact_type=data_partition` cargo (`batteries.csv` CAN serial lookup table + event match schedule). Inside the competition venue, event rules (`E301`) strictly prohibit `802.11` Wi-Fi traffic, and the nodes' NimBLE GATT server is dedicated to scouter phone syncs. The nodes switch to Venue Offline Mode (`ff_set_offline_mode(true)`), keeping `802.11` Wi-Fi and Improv BLE completely silent while continuing to use `ff_get_secret()`, `ff_logf()`, `ff_telemetry()` (`age_ms`), `ff_selftest`, and `ff_safe_window`. When a firmware fix or updated `batteries.csv` cargo is needed in the pit between matches, the pit laptop pushes the Fleetforge-signed bundle over the magnetic USB-C dock cable (`link_type: "dock"`) and drains buffered logs, crash rings, and mesh peer telemetry (`sub_versions`) without turning on Wi-Fi.

**Steps:**

1. In the shop, Chloe creates the `flux-10413-hub` project with cohort groups (`pit`, `cart`, `scouting-chief`), configures the `hub_aes_key` secret in Project Secrets, and enables Canary-gated Continuous Deployment (`canary: true`).
2. Pushing a commit triggers CI to compile the ESP-IDF firmware (`ab-4m-v1`) and upload the ECDSA P-256 signed `.bin` to Fleetforge. Fleetforge stages the build to the Canary node (`pit`) first. On boot, the node runs its registered `ff_selftest` (`SX1262` LoRa SPI version check, NimBLE GATT service registration, and `SN65HVD230` CAN loopback) before reporting `confirmed`, automatically advancing the wave to `cart` and `scouting-chief`.
3. Chloe uploads `hub-config.bin` (`batteries.csv` + event match schedule) with `?artifact_type=data_partition`. Each node's registered cargo handler (`ff_ota_register_cargo_handler`) writes the table to LittleFS/NVS without rebooting the application slot and reports `"sub_versions": {"batteries_csv": "v14", "schedule": "2027nytr"}` in `up/announce`.
4. Entering the competition venue, Chloe flips the Venue Mode switch (or the pit companion sends the venue lockout command over USB-C). Each node calls `ff_set_offline_mode(true)`: `libfleetforge` keeps `802.11` Wi-Fi (`esp_wifi_stop()`) and Improv BLE advertising 100% disabled while leaving the shared NimBLE GATT stack running the team's `AES-256-GCM` scouting service and returning `hub_aes_key` from NVS (`ff_get_secret`).
5. During matches, the nodes exchange `915 MHz LoRa` packets and log diagnostics (`ff_logf`) and link metrics (`ff_telemetry("lora_rssi", -78)` with relative `age_ms`) into RAM / `RTC_NOINIT_ATTR` rings. The `Robot Cart Node` and `Pit Node` track the bleacher node's firmware and battery status in `sub_versions` (`{"chief_node": "1.2.0", "batteries_csv": "v14"}`). While a BLE sync or USB-C queue session is active, `ff_safe_window` returns `false` so no pending reboot interrupts match queuing.
6. Between matches in the pit (with Wi-Fi still off), Chloe plugs the magnetic USB-C cable (`link_type: "dock"`) into the pit laptop. The laptop drains the node's buffered `up/log` (including any `[prev-boot]` crash lines) and `up/telemetry` (`age_ms`) rings over USB-C serial, and stages a signed hotfix `.bin` (or updated `data_partition` cargo) directly over USB-C into the node's OTA/cargo pipeline—verifying the ECDSA P-256 signature on-chip, waiting for `ff_safe_window`, and confirming via `ff_selftest` with zero Wi-Fi packets transmitted.

**Success Criteria:**

- In Venue Offline Mode (`ff_set_offline_mode(true)`), the node transmits **zero** `802.11` Wi-Fi frames and never starts Improv BLE advertising (`00467768-6228-2272-4663-277478268000`), while the application's own NimBLE GATT service, `ff_get_secret()`, `ff_logf()`, and `ff_telemetry()` operate without interruption.
- A bad firmware update whose `SX1262` LoRa SPI or CAN transceiver check fails in `ff_selftest` automatically rolls back to the previous slot and halts the Canary rollout before `cart` or `scouting-chief` are touched.
- Non-`app` `data_partition` cargo updates (`batteries.csv`, match schedule) apply in place via `ff_ota_register_cargo_handler` and update `sub_versions` without rebooting or overwriting the A/B firmware slot.
- Tethered USB-C dock (`link_type: "dock"`) updates enforce the exact same ECDSA P-256 signature ring, `ota_slot_size` check, `ff_safe_window` deferral, and `ff_selftest` rollback contract as Wi-Fi OTA, and buffered offline logs/metrics preserve their original capture spacing via `age_ms`.

**Supported By:**

- `spec/device-protocol.md` → `up/announce` (`link_type: "dock"`, `sub_versions`, `capabilities`), `dn/cmd` (`stage` with `artifact.type` and `sig`), `up/log`, `up/telemetry` (`age_ms`), and `dn/cfg` (`secrets_rev`)
- `docs/SWARM.md` → §5.1 (`age_ms` store-and-forward), §5.3 (`link_type: "dock"`), and §5.4 (`artifact.type` + `sub_versions`)
- Pillars: `R4` (crash-surviving `up/log` + `age_ms` `up/telemetry`), `R5` (`ff_selftest` + `ff_safe_window`), `R6` (ECDSA P-256 signing), `CUJ-2` (`SEC-1` NVS `ff_sec` + Canary wave rollouts + `0013_artifact_type` cargo handlers), and Venue/Dock extensions (`ff_set_offline_mode`, NimBLE coexistence, `link_type: "dock"` serial bridge)

**Driver:** segmented — the online shop/hotel segments run against the existing simulator and C component test suites today; the Venue Offline Mode and USB-C `link_type: "dock"` serial bridge segments are graded as their firmware hooks land.

| Steps | Segment | Harness | Graded |
|---|---|---|---|
| 1–2 | Canary rollout + `ff_selftest` gate + `ff_sec` secret sync | `pytest tests/test_api_deploy.py tests/test_device_secrets.py tests/test_selftest.py`: Wave 1 canary stages 1 board, advances to cohort on `confirmed`, halts on `rolled_back`; `GET /v1/device-secrets` syncs `hub_aes_key` | yes |
| 3 | `data_partition` cargo deploy & `sub_versions` reporting | `pytest tests/test_api_deploy.py tests/test_ota_component.py`: `POST /v1/artifact?artifact_type=data_partition` stages via `ff_ota_register_cargo_handler` and updates `sub_versions` in `up/announce` | yes |
| 4 | Venue Offline Mode (`ff_set_offline_mode`) & shared NimBLE coexistence | `pytest tests/test_ci_cd.py tests/test_ota_component.py`: enabling `ff_set_offline_mode(true)` suppresses `esp_wifi_start()` and Improv BLE GATT registration, coexists with shared NimBLE (`ble_hs_synced()`), and confirms `ESP_OTA_IMG_PENDING_VERIFY` locally via `ff_selftest` | yes |
| 5–6 | USB-C `link_type: "dock"` signed stage, `ff_safe_window` deferral & offline `age_ms` ring drain | `pytest tests/test_ci_cd.py`: stages signed `.bin` / `data_partition` over USB-C dock (`ff_ota_dock_stage` / `StageRunner.dock_stage`), verifies ECDSA P-256 signature and `ff_safe_window` hold, drains `up/log` + `up/telemetry` (`age_ms`), and syncs via `POST /v1/devices/{device_id}/dock-sync` (`fleetforge dock-sync`) | yes |

**Judge:**

- *Deterministic (must-pass):*
  - After step 2, a Canary failure (`rolled_back` from `ff_selftest`) leaves the remaining group devices untouched on their prior `fw_version`.
  - After step 3, the device's `sub_versions` map in `GET /v1/devices` contains the deployed `data_partition` version while `fw_version` is unchanged.
  - In step 4, `ff_get_secret("hub_aes_key", ...)` returns the provisioned secret from NVS with zero network sockets opened.
  - In steps 5–6, offline `up/telemetry` samples flushed after a delay preserve their relative capture timestamps (`receipt_ts - age_ms`), and an unsigned or tampered dock image is rejected before flashing.
- *Hard-fail traps (cap the score at 0):*
  - Any `802.11` Wi-Fi frame or Improv BLE advertisement (`00467768-6228-2272-4663-277478268000`) is emitted while Venue Offline Mode is active.
  - A node reboots to apply an update while `ff_safe_window` reports `false` (active BLE sync or USB-C queue session).
  - `hub_aes_key` appears in plaintext in any MQTT payload, `up/log` line, or diagnostic bundle.
- *LLM-judge rubric (graded by `jeep` against the Success Criteria):*
  - The transition between Shop/Hotel Wi-Fi Mode and Venue Offline Mode is explicit and requires no reflashing or source edits.
  - Every failure (bad LoRa SPI self-test, invalid ECDSA signature over dock, stalled safe window) names the cause and recovery state clearly.
