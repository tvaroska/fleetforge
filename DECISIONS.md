# Decisions — Architectural Decision Index (`~/projects/fleetforge`)

Compact architectural decision index and recent decision log for **FleetForge** (`<= 12 KB`).
- **Full Historical Decision Log (`6,022` lines, `R0–R11`, `P1`, `S0`): [`docs/archive/DECISIONS-history.md`](docs/archive/DECISIONS-history.md)**
- **Core Specs & Architecture:** [`spec/prd.md`](spec/prd.md) · [`spec/device-protocol.md`](spec/device-protocol.md) · [`design/architecture.md`](design/architecture.md) · [`CRITICAL.md`](CRITICAL.md) · [`docs/roadmap.md`](docs/roadmap.md)

Append new decisions newest-first below. Keep this root file under `25 KB` (`< 12 KB` target) by archiving older sprint-level entries into [`docs/archive/DECISIONS-history.md`](docs/archive/DECISIONS-history.md).

---

## 1. Architectural Decision Index (`R0–R11` & Multi-Project Core)

| Area / Release | Core Architectural Decisions & Invariants | Reference Anchor in [`docs/archive/DECISIONS-history.md`](docs/archive/DECISIONS-history.md) |
| :--- | :--- | :--- |
| **Two Thin Waists (`R0–R6`)** | **Device waist:** opaque versioned artifact + 4-verb MQTT/HTTPS contract (`stage → apply → confirm → rollback`). **User waist:** headless FastAPI + SSE event stream (`api` + single-writer `ingestor`). | `R0-infra-1..3`, `R1-be-1..3`, [`design/architecture.md`](design/architecture.md) |
| **Enrollment & Identity (`R0`, `R2b`)** | Browser Web Serial flashes `ff_cfg` NVS partition; single-use enrollment token (`ffe_...`) exchanged once at `POST /v1/enroll` for `mqtt_password` (never stored in plaintext in Postgres; SHA-256 in `devices.mqtt_password_hash`, PBKDF2 in Mosquitto dynsec). | `R0-be-1..4`, `R0-fw-1..3`, `R2b-fe-1..6`, [`docs/features/enrollment.md`](docs/features/enrollment.md) |
| **Safe OTA, A/B Rollback & Pre-Check (`R1`, `R2`, `R2b`)** | `ab-4m-v1` partition table with `ESP_OTA_IMG_PENDING_VERIFY` + 300 s rollback timer. `POST /v1/deploy/precheck` is a side-effect-free twin of `POST /v1/deploy`; merged full-flash binaries are rejected at upload (`422`); `rollback_capable: false` is a gating warning requiring explicit per-code `override`. | `R2-fw-1..3`, `R2b-be-2..7`, `R2b-fw-2`, [`docs/features/ota-deploy.md`](docs/features/ota-deploy.md) |
| **Embeddable Library & Partition Profiles (`R3`)** | `libfleetforge` builds as both an ESP-IDF component and a PlatformIO/Arduino library (`agent/components/fleetforge`). Boards announce measured flash chip size, running partition layout SHA-256, and `rollback_capable` on every `up/announce`. | `R3-fw-1..4`, `R3-be-1..2`, [`docs/features/ota-library.md`](docs/features/ota-library.md) |
| **Health, Faults, RTC Crash Ring & OTel (`R4`, `OTEL-1`)** | `RTC_NOINIT_ATTR` magic+CRC32 ring (`0x46464C47`) survives panic/watchdog resets and flushes `[prev-boot]` logs before first MQTT connect. `fleetforge-ingestor` exports `up/log` and `up/telemetry` batches to OTLP/HTTP (`POST /v1/logs`, `POST /v1/metrics`). | `R4-fw-1..4`, `OTEL-1`, [`docs/features/health-telemetry.md`](docs/features/health-telemetry.md) |
| **Self-Test, Safe Window, Verbs & Sleepy Boards (`R5`)** | `ff_set_selftest()` / `Fleetforge.onSelfTest()` gates `PENDING_VERIFY` confirmation after `up/announce` `PUBACK`. `ff_set_safe_window_cb()` defers reboots (`awaiting_safe_window`) without blocking deadline/self-test rollback latching. Three rapid crashes within 60 s enter safe mode (`safe_mode: true`). `isReadyForSleep()` gates deep sleep until check-in, secret sync, and log/telemetry drain complete. | `R5-fw-1..7`, `R5-be-1..2`, `R5-fe-1`, [`docs/features/self-test.md`](docs/features/self-test.md) |
| **ECDSA P-256 Signing & Resumable OTA (`R6`)** | Every artifact is signed (`ecdsa-p256-sha256`); devices verify against a 2-key rotation ring (`ff_cfg` primary + NVS secondary) and read back flash SHA-256 before moving the boot pointer. Interrupted downloads resume via HTTP `Range` from a scratch flash slot. | `R6-fw-1..4`, `R6-be-1..3`, [`docs/features/security-hardening.md`](docs/features/security-hardening.md) |
| **Multi-Project RBAC, Scoped Tokens & Firebase Auth (`P1`, `S0-sec-1..2`)** | Every device, group, artifact, and token belongs to a `project_id`. Token prefixes (`ffa_` admin/CI vs. `ffe_` enroll) are disjoint. Partition-profile writes are superadmin-only; project-pinned tokens cannot mint child tokens; cross-project device IDs return `404`; Firebase Auth requires verified email (`email_verified`), `iss` check, and live `accounts:lookup` revocation check. | `2026-10-19 (S0-sec-1, S0-sec-2, S0-bug-5..7, S0-rel-1)`, [`docs/features/projects-and-users.md`](docs/features/projects-and-users.md) |

---

## 2. Recent Architectural Decisions (2026-10)

### 2026-10-24 — CUJ-3 Competition Pit & Contested-Venue Telemetry ("Chloe" / `FLUX-10413-Hub`): Venue Offline Mode (`ff_set_offline_mode`), shared NimBLE coexistence, local `PENDING_VERIFY` self-test confirm, and USB-C dock (`link_type: "dock"`) stage & ring drain

- **Cause:** Supporting Persona #6 ("Chloe" — Competition Pit & Contested-Venue Telemetry Engineer operating a 3-node `ESP32-S3 + SX1262 915 MHz LoRa + BLE + CAN` mesh under competition rules `E301` / `G303` / `R905`, `CUJ-3` in `spec/cujs.md`) exposed four gaps when transitioning from shop/hotel Wi-Fi to an `802.11`-banned competition arena:
  1. `ff_net_wifi.c` unconditionally started `esp_wifi_start()` and opened a 120 s Improv Wi-Fi BLE GATT service via its own `nimble_port_init()` when known SSIDs were unreachable — violating `802.11` radio bans and colliding with the application's own NimBLE GATT stack.
  2. `ff_mqtt.c` only evaluated `ff_selftest` and marked `ESP_OTA_IMG_PENDING_VERIFY` slots valid upon receiving an MQTT `PUBACK` for `up/announce`.
  3. No tethered USB-C dock (`link_type: "dock"`) entry point existed in `ff_ota.c` to stage a signed `.bin` or `artifact_type=data_partition` cargo (`batteries.csv` + match schedule) or drain buffered `ff_log` / `ff_telemetry` rings into a JSON bundle when Wi-Fi is disabled.
  4. No server endpoint or CLI command existed to ingest a pit laptop's drained USB-C dock bundle once the laptop regained internet.
- **Decisions:**
  - **Venue / EMCON Offline Radio Lockout & Shared NimBLE Coexistence (`ff_set_offline_mode`):** `ff_set_offline_mode(bool offline)` and `Fleetforge.setOfflineMode(bool offline)` immediately stop `802.11` Wi-Fi (`esp_wifi_stop()`), suppress Improv BLE GATT startup, and halt background MQTT reconnect attempts while keeping NVS `ff_get_secret()`, `ff_logf()`, `ff_telemetry()`, `ff_selftest`, and `ff_safe_window` active. `improv_ble_gatt_start()` checks `s_venue_offline || !s_improv_ble_enabled` and inspects `ble_hs_synced()` before calling `nimble_port_init()`.
  - **Offline `PENDING_VERIFY` Self-Test Confirmation (`ff_mqtt_confirm_offline`):** Evaluates `ff_selftest_run()` locally without waiting for a broker `PUBACK`; marks the slot valid (`esp_ota_mark_app_valid_cancel_rollback()`) on pass or triggers immediate rollback (`esp_ota_mark_app_invalid_rollback_and_reboot()`) on failure.
  - **Tethered USB-C Dock Stage & Offline Ring Drains (`ff_ota_dock_stage`, `ff_log_drain_json`, `ff_telemetry_drain_json`):** Verifies SHA-256 and ECDSA P-256 signature against the 2-key rotation ring, stages `app` or `data_partition` cargo while respecting `ff_safe_window_is_safe()`, and drains RAM + `[prev-boot]` `RTC_NOINIT_ATTR` logs and `age_ms` telemetry into `proto: 1` JSON envelopes.
  - **Single-Writer Ingest Reuse for Dock Sync (`POST /v1/devices/{device_id}/dock-sync` & `fleetforge dock-sync`):** Replays dock bundles (`announce`, `statuses`, `telemetry`, `logs`) through `ingestor/handlers.py::handle_up_message`, preserving single-writer `deploy_events` invariants and `(device_id, cmd_id, state)` deduplication.
- **Details:** [`spec/cujs.md`](spec/cujs.md) (`CUJ-3`), [`docs/personas/PERSONAS.md`](docs/personas/PERSONAS.md) (§3.6), [`agent/components/fleetforge/README.md`](agent/components/fleetforge/README.md), `tests/test_ci_cd.py` (`TestCuj3PitTetheredDockAndVenueOffline`).

### 2026-10-21 — CUJ-2 E-Ink & Developer Experience: hybrid Git-driven CI/CD (`CI-CD-1`), rotating secrets (`SEC-1`), OTel log/metric export (`OTEL-1` + `R4-fw-3/4`), and Improv BLE Wi-Fi (`WIFI-BLE-1`)

- **Decisions:**
  - **Pillar 1 — Hybrid Git-Driven CI/CD (`CI-CD-1`):** External CI uploads `.bin` + git provenance (`repo_url`, `branch`, `commit_sha`, `tag`, `build_url`, `test_passed`) via `python -m fleetforge.cli push` to `POST /v1/artifact`. `projects` tracks `repo_url`, `track_mode` (`off | branch | tag | commit`), `track_ref`, `auto_deploy_target` (`off | dev | all`), and `ci_webhook_url`. `devices` gains `is_dev` and `stay_awake` (published on retained `dn/cfg` so bench boards hold deep sleep during rapid CI iteration).
  - **Pillar 2 — Rotating Secrets Without Reflashing (`SEC-1`):** `dn/cfg` carries only the integer revision doorbell (`secrets_rev`); encrypted values live in PostgreSQL (`project_secrets`, Fernet-encrypted). When `dn/cfg.secrets_rev > local_secrets_rev`, the board fetches `GET /v1/device-secrets` over HTTPS (`Authorization: Basic base64(device_id:mqtt_password)`), writes NVS `"ff_sec"`, and re-announces `secrets_rev`.
  - **Pillar 3 — Board-to-OTel Observability (`OTEL-1` + `R4-fw-3/4`):** Firmware `ff_logf()` buffers 32 RAM lines + 4 crash-surviving `RTC_NOINIT_ATTR` lines and flushes to `up/log` before deep sleep. `fleetforge-ingestor` exports `up/log` and `up/telemetry` batches as OTLP/HTTP JSON.
  - **Pillar 4 — Multi-Wi-Fi & Improv Wi-Fi over BLE (`WIFI-BLE-1`):** `ff_net_wifi.c` prepends a writable 4-SSID NVS `"ff_net"` ring ahead of `ff_cfg` SSIDs and opens a bounded 120 s Improv Wi-Fi BLE GATT window (`00467768-6228-2272-4663-277478268000`) when all known APs are unreachable, stopping BLE before deep sleep.
- **Details:** [`spec/device-protocol.md`](spec/device-protocol.md), [`spec/cujs.md`](spec/cujs.md) (`CUJ-2`), [`docs/features/vcs-integration.md`](docs/features/vcs-integration.md), migration `alembic/versions/0011_ci_cd_and_device_cfg.py`.

---

> For all prior entries (`2026-10-19` `P1`/`S0` through `2026-09` `R0`), see [`docs/archive/DECISIONS-history.md`](docs/archive/DECISIONS-history.md).
