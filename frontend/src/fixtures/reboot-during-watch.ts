/**
 * A board that reboots three times while the panel is watching (R2b-fe-4).
 *
 * **Synthetic, not a capture.** Every line is a real format string (`agent/main/*.c`, the
 * boot ROM, `esp_brownout`), arranged the way the agent's real brownout path prints them:
 * `E BOD: Brownout detector was triggered` at the END of a boot, then a `rst:0x3 (SW_RESET)`
 * banner (CONFIG_ESP_BROWNOUT_USE_INTR=y makes the ISR restart the chip, so the ROM says
 * SOFTWARE reset), then the agent's own "the previous boot ended in a BROWNOUT" line
 * (`agent_main.c::log_power_fault`). The reset reason is therefore only recoverable from the
 * line above the banner and from the agent, never from the banner itself.
 *
 * Boot 1 reaches the clock and dies; boots 2 and 3 die before the link; boot 4 is the
 * current one, waiting for the link.
 */
const AGENT = 'I (100) ff-agent: fleetforge agent 0.4.5 (idf v5.5.5), built Oct  4 2026 10:00:00'

const PREVIOUS_BOOT_BROWNOUT =
  "W (105) ff-agent: the previous boot ended in a BROWNOUT: this board's 3.3 V rail fell " +
  "below the detector's threshold and the chip reset itself. The supply is marginal for " +
  'this board even if this boot succeeds.'

/** One boot that came back from a brownout and starts Wi-Fi. */
const RESTARTED = [
  'rst:0x3 (SW_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  AGENT,
  PREVIOUS_BOOT_BROWNOUT,
  'I (300) ff-wifi: wifi sta starting, ssid bench-2g',
]

export const REBOOT_DURING_WATCH: string[] = [
  // boot 1: gets to Clock set, then browns out
  'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  AGENT,
  'I (121) ff-id: device_id 3c8427b1f0a4',
  'I (300) ff-wifi: wifi sta starting, ssid bench-2g',
  'I (1002) ff-net: wifi link up, ip 192.168.1.57 gw 192.168.1.1 mask 255.255.255.0',
  'I (1500) ff-time: sntp: 1970-01-01T00:00:02Z -> 2026-10-04T10:00:00Z (via pool.ntp.org)',
  'E BOD: Brownout detector was triggered',
  // boot 2: SW_RESET banner, dies before the link
  ...RESTARTED,
  'E BOD: Brownout detector was triggered',
  // boot 3: the same
  ...RESTARTED,
  'E BOD: Brownout detector was triggered',
  // boot 4: the current boot, waiting for the link
  ...RESTARTED,
]
