# Fleetforge + LVGL v9 on a 1-bpp E-Paper Panel (ESP-IDF)

A minimal ESP-IDF + LVGL v9 pattern for a 1-bpp e-paper board (such as the Waveshare
ESP32-S3-ePaper-3.97 with an 800×480 SSD1677 panel and AXP2101 PMIC). Copy
`fleetforge_start.c`, `fleetforge_start.h`, `partitions.csv` (`ab-4m-v1`), and
`sdkconfig.defaults` from [`../basic_idf/`](../basic_idf/README.md); see
[`../../README.md`](../../README.md) for the library reference.

## Why this pattern is ~80 lines instead of ~9,000

- **LVGL v9 (`lvgl/lvgl: "^9.2.0"`)** owns the 1-bpp framebuffer (`LV_COLOR_FORMAT_I1` +
  `LV_DISPLAY_RENDER_MODE_FULL`), text layout, and built-in fonts (`lv_font_montserrat_48`) —
  no custom pixel-packing or generated C font bitmaps.
- **On-screen version proof:** `lv_label_set_text_fmt(label, "%s (%s)", MESSAGE, ff_identity_fw_version())`
  renders `"Hello (1.1.0)"` directly on the bistable screen so you can verify at a glance which
  OTA image is running.
- **Safe-window gating (`ff_set_safe_window_cb`):** holds `s_busy = true` while the e-paper
  refresh waveform is active (`~2.7 s`), so an incoming OTA apply or rollback reboot never cuts
  power mid-refresh (which leaves ghosting on e-paper).
- **Hardware self-test (`ff_set_selftest`):** reports `ff_selftest_report(true, NULL)` after the
  first connection and panel refresh succeed.

## Three hardware & LVGL gotchas solved in this example

1. **LVGL v9 `LV_COLOR_FORMAT_I1` 8-byte palette header:**
   LVGL v9 prepends two 32-bit `lv_color32_t` palette entries (`8` bytes) before the packed 1-bpp
   rows (`800 * 480 / 8 = 48,000` bytes). Allocate `EPD_FB_SIZE + 8` bytes (`48,008` bytes) and
   pass `px_map + 8` in `flush_cb`.
2. **100 Hz FreeRTOS tick truncation (`pdMS_TO_TICKS(2) == 0`):**
   ESP-IDF defaults to `CONFIG_FREERTOS_HZ=100` (`10 ms` tick). Any `vTaskDelay(pdMS_TO_TICKS(ms))`
   with `ms < 10` truncates to `0` ticks (`vTaskDelay(0)` yield, `~1 µs`), which cannot wake an
   SSD1677 controller from Deep Sleep (`0x10 0x01`). Always hold `PIN_RST` low for at least
   `pdMS_TO_TICKS(20)` (`20 ms`).
3. **GPIO ESD-diode back-powering across PMIC LDO power cycles:**
   In deep sleep the SSD1677 draws `~1 µA`. If `PIN_RST` or `PIN_CS` stays driven high (`3.3 V`)
   while the PMIC LDO rail (`ALDO3`) is turned off, current leaking through the controller's ESD
   protection diodes keeps `VDD` above its Power-On Reset threshold. Drive `RST`, `CS`, `DC`,
   `MOSI`, and `SCLK` low (`0 V`) before disabling the LDO rail.
