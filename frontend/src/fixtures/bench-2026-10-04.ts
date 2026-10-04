/**
 * The S0-bug-1 bench session, 2026-10-04: an ESP32-S3 (94a990dd09a4) flashed over native USB.
 *
 * **This is a reconstruction, not a capture.** The serial log was not saved. The lines are
 * the agent's real format strings (`agent/main/*.c`, identical at 0.3.2 and 0.4.5) arranged
 * in the order the server-side evidence implies (`device_progress`, API and broker logs):
 * the SNTP wait timed out after 15 s, yet enrolment over https passed and MQTT connected,
 * because the S3's RTC kept a sane clock across the flasher's hard reset.
 */
export const BENCH_2026_10_04: string[] = [
  'I (100) ff-agent: fleetforge agent 0.3.2 (idf v5.5.5), built Sep 23 2026 19:00:00',
  'I (2100) ff-net: wifi link up, ip 192.168.1.57 gw 192.168.1.1 mask 255.255.255.0',
  'W (17200) ff-time: sntp: no answer from pool.ntp.org within 15000 ms; clock is still 2026-10-04T14:19:59Z',
  'I (19000) ff-enroll: enroll 200 https://bingo.tvaroska.sk/v1/enroll',
  'I (24800) ff-mqtt: mqtt connected as 94a990dd09a4 (mqtts://bingo.tvaroska.sk:8883)',
]
