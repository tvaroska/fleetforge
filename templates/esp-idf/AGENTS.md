# {{APP_NAME}} Firmware Agent Instructions (`AGENTS.md`)

## 1. Project & Hardware Target

- **Application Binary:** `build/{{APP_NAME}}.bin` ([`CMakeLists.txt`](CMakeLists.txt))
- **ESP-IDF Target:** `{{TARGET}}` (`ESP-IDF >= 5.5`, partition layout `ab-4m-v1`)
- **Fleetforge Project Slug:** `{{PROJECT_SLUG}}` (`{{FLEETFORGE_URL}}`)
- **Fleetforge Component Dependency:** [`main/idf_component.yml`](main/idf_component.yml) (`https://github.com/tvaroska/fleetforge.git`, `path: agent/components/fleetforge`, `version: {{FLEETFORGE_REF}}`)
- **CI/CD Pipeline:** [`.github/workflows/fleetforge.yml`](.github/workflows/fleetforge.yml) (compiles inside `espressif/idf:v5.5.5` on GitHub Actions and pushes `build/{{APP_NAME}}.bin` with Git provenance when `FLEETFORGE_TOKEN` is set)

---

## 2. Closed-Loop Agent Workflow (`build → push → status → logs`)

1. **Bump Firmware Version on Every Change:**
   - Update `set(PROJECT_VER "X.Y.Z")` in [`CMakeLists.txt`](CMakeLists.txt) before building. Fleetforge versions are immutable per `(project, target, version)`.
2. **Compile Firmware:**
   - Local build (uses host `idf.py` if on `PATH`, or falls back to Docker `espressif/idf:v5.5.5`): `just build` (or explicitly `just build-idf` / `just build-docker`)
   - Remote CI build: push to `main` or tag `v*` to trigger [`.github/workflows/fleetforge.yml`](.github/workflows/fleetforge.yml) (or `gh workflow run fleetforge.yml -f deploy=dev`)
3. **Upload & Deploy Over the Air:**
   - Upload and auto-deploy (if Continuous Deployment is configured on `{{PROJECT_SLUG}}`): `just push`
   - Or trigger an explicit deploy to `dev` / `all` boards: `just push --deploy dev`
4. **Verify Rollout & Inspect Live Board Logs:**
   - Check board presence, `fw_version`, `free_heap`, `secrets_rev`, and deploy state (`staging → staged → confirming → confirmed` or `rolled_back`): `just status`
   - Tail structured `up/log` records, `[prev-boot]` crash-ring lines, and heartbeat context (`rssi`, `free_heap`, `uptime_s`): `just logs`
   - Rotate runtime secrets (`api_url`, `api_key`, etc.) without reflashing: `just secrets set <key> <value>`

---

## 3. Flash-Time Immutables & OTA Safety Rules

1. **`fleetforge_start()` First in `app_main()` ([`main/main.c`](main/main.c), [`main/fleetforge_start.c`](main/fleetforge_start.c)):**
   - `fleetforge_start()` MUST be the first statement of `app_main()`. It arms the OTA rollback timer before any network/peripheral init and enters safe-mode hold if 3 crash resets occurred within 60 seconds.
   - Never modify [`main/fleetforge_start.c`](main/fleetforge_start.c) or [`main/fleetforge_start.h`](main/fleetforge_start.h).
2. **Never Edit [`partitions.csv`](partitions.csv) or Bootloader Rollback Flags in [`sdkconfig.defaults`](sdkconfig.defaults):**
   - `partitions.csv` defines the `ab-4m-v1` map (`ff_cfg` at `0x12000`, two `1,966,080`-byte OTA slots `ota_0`/`ota_1`).
   - `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y` is required at compile time (`#error` in `fleetforge_start.c`).
   - Never enable `CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK`, `CONFIG_SECURE_BOOT`, or `CONFIG_SECURE_FLASH_ENC_ENABLED` unless intentionally burning irreversible hardware eFuses.
3. **Never Call `esp_ota_mark_app_valid_cancel_rollback()` Directly:**
   - Fleetforge owns OTA confirmation after the board connects to the broker and your self-test passes.
   - Register a self-test callback with `ff_set_selftest(selftest_cb, NULL)` returning `FF_SELFTEST_PENDING`, then call `ff_selftest_report(true, NULL)` once your main loop has verified peripheral/application health (or `ff_selftest_report(false, "reason")` to trigger immediate rollback).
4. **Gate Critical Hardware Work with `ff_set_safe_window_cb()`:**
   - Return `false` from your safe-window callback while refreshing an e-paper/OLED display, writing NVS/flash, or driving actuators so OTA apply/rollback reboots only occur when idle.
5. **Deep-Sleep Readiness (`FF_POWER_SLEEPY`):**
   - For battery-powered boards (`ff_set_power_class(FF_POWER_SLEEPY, wake_interval_s)`), wait until `ff_is_ready_for_sleep()` returns `true` before calling `esp_deep_sleep_start()`. This guarantees MQTT announce, secret sync, OTA staging, and `up/log` drain have finished.

---

## 4. Fleetforge C Maker API Reference (`#include "ff_*.h"`)

| Header | Function | Purpose |
| :--- | :--- | :--- |
| `fleetforge_start.h` | `bool fleetforge_start(void)` | First call in `app_main()`; arms rollback timer and spawns background `fleetforge` task. |
| `ff_identity.h` | `const char *ff_identity_fw_version(void)` | Returns running firmware version (`PROJECT_VER`). |
| `ff_log.h` | `esp_err_t ff_logf(char level, const char *fmt, ...)` | Non-blocking structured log (`'D'`, `'I'`, `'W'`, `'E'`) exported over `up/log` + OTel (`32`-line RAM ring + `4`-line `RTC_NOINIT_ATTR` pre-crash ring). |
| `ff_telemetry.h` | `esp_err_t ff_telemetry(const char *name, double value)` | Non-blocking numeric metric (`[a-z][a-z0-9_]{0,30}`, not starting with `ff_`) batched on `up/telemetry`. |
| `ff_sec.h` | `esp_err_t ff_get_secret(const char *key, char *buf, size_t len)` | Reads encrypted project secret synced to NVS `ff_sec` (`ESP_ERR_NOT_FOUND` if unset). |
| `ff_sec.h` | `bool ff_sec_synced(void)` | `true` once the board has synced the project's latest `secrets_rev`. |
| `ff_selftest.h` | `void ff_set_selftest(ff_selftest_fn fn, void *ctx)` | Registers boot self-test hook (`FF_SELFTEST_PASS`, `FF_SELFTEST_FAIL`, or `FF_SELFTEST_PENDING`). |
| `ff_selftest.h` | `void ff_selftest_report(bool passed, const char *reason)` | Completes an async (`FF_SELFTEST_PENDING`) boot self-test. |
| `ff_safe_window.h` | `void ff_set_safe_window_cb(ff_safe_window_fn fn, void *ctx)` | Defers OTA/rollback reboot (`awaiting_safe_window`) while callback returns `false`. |
| `ff_sleep.h` | `void ff_set_power_class(ff_power_class_t cls, uint32_t wake_s)` | Declares `FF_POWER_ALWAYS_ON` (`0`) or `FF_POWER_SLEEPY` (`wake_s`). |
| `ff_sleep.h` | `bool ff_is_connected(void)` | `true` when the board's MQTT session is connected and announced. |
| `ff_sleep.h` | `bool ff_is_ready_for_sleep(void)` | `true` when announce, OTA check, secret sync, and log drain are complete. |

---

## 5. ESP32 DRAM & Hardware Guardrails

- **8-Bit DRAM Headroom for Concurrent TLS (`free_heap >= 65 KB`):**
  - Keep [`sdkconfig.defaults`](sdkconfig.defaults)'s `CONFIG_MBEDTLS_DYNAMIC_BUFFER=y` enabled and avoid large static `.bss` buffers. Heap-allocate transient HTTP response or display buffers only while needed and `free()` them before idle.
  - If using LVGL v9, cap `CONFIG_LV_MEM_SIZE_KILOBYTES=16` (the 64 KB default exhausts internal SRAM alongside `mqtts://`).
- **FreeRTOS 100 Hz Tick Truncation (`pdMS_TO_TICKS`):**
  - At ESP-IDF's default `CONFIG_FREERTOS_HZ=100` (`10 ms` tick), `pdMS_TO_TICKS(ms)` with `ms < 10` evaluates to `0` ticks. Use `>= pdMS_TO_TICKS(20)` for hardware reset pulses or `esp_rom_delay_us()` for sub-10 ms delays.
