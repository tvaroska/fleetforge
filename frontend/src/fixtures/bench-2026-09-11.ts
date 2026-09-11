/**
 * The first real-hardware session, 2026-09-11: an ESP32-DevKit v1 that never enrolled.
 *
 * **This is a reconstruction, not a capture.** The raw UART log of that session was pasted
 * into a chat window and was never committed, which is itself part of what S0-fe-7 exists
 * to fix. Every line that the session record quotes verbatim is here verbatim — the
 * `E BOD:` line, `phy_init … falling back to full calibration`, the one early boot that
 * reached `ff-net: … link up` (the source of the stale ✓ that sent the diagnosis in the
 * wrong direction), and the agent's own log strings from `agent/main/*.c`. The ROM banners
 * around them are what a DevKit v1 prints on a brownout reset. Sources: `TODO.md`
 * (S0-fe-4), `DECISIONS.md` 2026-09-11, and
 * `design/decisions/enrollment-console-is-the-diagnostic-surface.md`.
 *
 * The board browns out during Wi-Fi PHY calibration — the radio's current draw collapses
 * the 3.3 V rail — so each cycle gets a little further or a little less far depending on
 * how much charge the cap had. The first cycle made it all the way to DHCP before dying.
 */
export const BENCH_2026_09_11: string[] = [
  // ── cycle 1: power on, gets a link, then browns out with the radio up ──────────────
  'ets Jun  8 2016 00:22:57',
  '',
  'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'configsip: 0, SPIWP:0xee',
  'clk_drv:0x00,q_drv:0x00,d_drv:0x00,cs0_drv:0x00,hd_drv:0x00,wp_drv:0x00',
  'mode:DIO, clock div:2',
  'load:0x3fff0030,len:1344',
  'entry 0x400805f0',
  'I (31) boot: ESP-IDF v5.5.5 2nd stage bootloader',
  'I (72) boot: Loaded app from partition at offset 0x20000',
  'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
  'I (104) ff-agent: chip: model=1 cores=2 revision=301',
  'I (118) ff-cfg: ff_cfg v1 loaded (crc ok), 214 byte payload from 0x12000',
  'I (121) ff-id: device_id 3c8427b1f0a4',
  'I (300) ff-wifi: wifi sta starting, ssid bench-2g',
  'W (331) phy_init: failed to load RF calibration data (0x1102), falling back to full calibration',
  'I (884) ff-wifi: associated; waiting for DHCP',
  'I (1002) ff-net: wifi link up, ip 192.168.1.57 gw 192.168.1.1 mask 255.255.255.0',
  'E BOD: Brownout detector was triggered',
  '',
  // ── cycle 2: dies inside calibration, before the radio ever associates ─────────────
  'ets Jun  8 2016 00:22:57',
  '',
  'rst:0xf (RTCWDT_BROWN_OUT_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'configsip: 0, SPIWP:0xee',
  'mode:DIO, clock div:2',
  'entry 0x400805f0',
  'I (31) boot: ESP-IDF v5.5.5 2nd stage bootloader',
  'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
  'I (118) ff-cfg: ff_cfg v1 loaded (crc ok), 214 byte payload from 0x12000',
  'I (121) ff-id: device_id 3c8427b1f0a4',
  'I (300) ff-wifi: wifi sta starting, ssid bench-2g',
  'W (330) phy_init: failed to load RF calibration data (0x1102), falling back to full calibration',
  'E BOD: Brownout detector was triggered',
  '',
  // ── cycle 3: identical. It did this for the whole session. ─────────────────────────
  'ets Jun  8 2016 00:22:57',
  '',
  'rst:0xf (RTCWDT_BROWN_OUT_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'mode:DIO, clock div:2',
  'entry 0x400805f0',
  'I (31) boot: ESP-IDF v5.5.5 2nd stage bootloader',
  'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
  'I (118) ff-cfg: ff_cfg v1 loaded (crc ok), 214 byte payload from 0x12000',
  'I (121) ff-id: device_id 3c8427b1f0a4',
  'I (300) ff-wifi: wifi sta starting, ssid bench-2g',
  'W (330) phy_init: failed to load RF calibration data (0x1102), falling back to full calibration',
  'E BOD: Brownout detector was triggered',
]
