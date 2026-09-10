// Every log line quoted here is a real format string from `agent/main/*.c`. That is the
// contract this file defends: if someone rewords `ff_net_wifi.c`'s "disconnected (reason
// %d)" and the classifier is not updated, the panel silently stops naming causes and the
// operator is back to guessing. These tests fail loudly instead.

import { describe, expect, it } from 'vitest'
import {
  MILESTONES,
  classifyConsoleLine,
  summarizeConsole,
  type ConsoleEvent,
} from './boardConsole'

const classify = (lines: string[]): ConsoleEvent[] =>
  lines.map((line, index) => classifyConsoleLine(line, index))

describe('classifyConsoleLine', () => {
  it('splits the ESP-IDF preamble off', () => {
    const event = classifyConsoleLine('I (1234) ff-wifi: associated; waiting for DHCP', 0)
    expect(event.level).toBe('info')
    expect(event.tag).toBe('ff-wifi')
    expect(event.text).toBe('associated; waiting for DHCP')
  })

  it('strips the colour codes IDF emits when CONFIG_LOG_COLORS is on', () => {
    const event = classifyConsoleLine('\u001b[0;31mE (77) ff-enroll: enroll 401: nope\u001b[0m', 0)
    expect(event.level).toBe('error')
    expect(event.tag).toBe('ff-enroll')
    expect(event.raw).toBe('E (77) ff-enroll: enroll 401: nope')
  })

  it('keeps boot-ROM chatter, which has no preamble at all', () => {
    const event = classifyConsoleLine('rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)', 0)
    expect(event.level).toBe('plain')
    expect(event.tag).toBeNull()
    expect(event.milestone).toBeNull()
  })

  it.each([
    ['I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built …', 'boot'],
    ['I (900) ff-net: wifi link up, ip 192.168.1.40 gw 192.168.1.1 mask 255.255.255.0', 'link'],
    ['I (1500) ff-time: sntp: 1970-01-01T00:00:02Z -> 2026-09-10T21:00:00Z (via pool.ntp.org)', 'clock'],
    ['I (2600) ff-enroll: enroll 200 https://bingo.tvaroska.sk/v1/enroll', 'enroll'],
    ['I (3100) ff-mqtt: mqtt connected as a4cf12b3de90 (mqtts://bingo.tvaroska.sk:8883)', 'fleet'],
  ])('recognises the milestone in %s', (line, milestone) => {
    expect(classifyConsoleLine(line, 0).milestone).toBe(milestone)
  })

  it('does not mistake the sntp TIMEOUT warning for a set clock', () => {
    // `ff_time.c:75` — same tag, same "sntp:" prefix, no arrow. Mistaking it would hide
    // the single most confusing failure there is: TLS failing because it is still 1970.
    const event = classifyConsoleLine(
      'W (12500) ff-time: sntp: no answer from pool.ntp.org within 10000 ms; clock is still 1970-01-01T00:00:02Z',
      0,
    )
    expect(event.milestone).toBeNull()
    expect(event.hint).toMatch(/TLS/)
  })

  it.each([
    [201, /2\.4 GHz/],
    [15, /password/],
    [202, /password/],
    [204, /password/],
    [200, /weak signal/],
  ])('decodes esp_wifi disconnect reason %i', (code, expected) => {
    const event = classifyConsoleLine(
      `W (5000) ff-wifi: disconnected (reason ${code}); reconnecting in 1000 ms`,
      0,
    )
    expect(event.hint).toMatch(expected)
  })

  it('still says something useful for a reason code it has never seen', () => {
    const event = classifyConsoleLine('W (5000) ff-wifi: disconnected (reason 39); …', 0)
    expect(event.hint).toContain('39')
  })

  it.each([
    ['E (2600) ff-enroll: enroll 401: the enrollment token was rejected', /new one/],
    ['E (2600) ff-enroll: enroll 409: this token is already used, revoked or expired.', /single-use/],
    ['E (2600) ff-enroll: enroll 503: broker provisioning is unavailable.', /server-side/],
    ['E (99) ff-agent: halted: no running partition', /re-flashed/],
    ['E (4000) ff-mqtt: broker refused the connection (return code 5).', /Re-flash/],
  ])('names the cause behind %s', (line, expected) => {
    expect(classifyConsoleLine(line, 0).hint).toMatch(expected)
  })
})

describe('summarizeConsole', () => {
  it('walks the whole happy path to the fleet', () => {
    const summary = summarizeConsole(
      classify([
        'I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
        'I (120) ff-id: device_id a4cf12b3de90',
        'I (300) ff-wifi: wifi sta starting, ssid fleetforge-test',
        'I (800) ff-wifi: associated; waiting for DHCP',
        'I (900) ff-net: wifi link up, ip 192.168.1.40 gw 192.168.1.1 mask 255.255.255.0',
        'I (1500) ff-time: sntp: 1970-01-01T00:00:02Z -> 2026-09-10T21:00:00Z (via pool.ntp.org)',
        'I (2600) ff-enroll: enroll 200 https://bingo.tvaroska.sk/v1/enroll',
        'I (3100) ff-mqtt: mqtt connected as a4cf12b3de90 (mqtts://bingo.tvaroska.sk:8883)',
      ]),
    )
    expect(summary.reached).toEqual([...MILESTONES])
    expect(summary.waitingFor).toBeNull()
    expect(summary.fault).toBeNull()
  })

  it('reproduces the silent board of 2026-09-10 and names the cause', () => {
    // Booted, printed its device_id, and then nothing: the ONLY further output is the
    // 5 s retry from `agent_main.c:167`. This is exactly what the bench saw, and exactly
    // what nobody could diagnose without a serial cable.
    const summary = summarizeConsole(
      classify([
        'I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
        'I (120) ff-id: device_id a4cf12b3de90',
        'I (300) ff-wifi: wifi sta starting, ssid home-5g',
        'W (5300) ff-wifi: disconnected (reason 201); reconnecting in 1000 ms',
        'W (6300) ff-agent: no network yet; waiting for the link',
        'W (11300) ff-wifi: disconnected (reason 201); reconnecting in 2000 ms',
        'W (16300) ff-agent: no network yet; waiting for the link',
      ]),
    )
    expect(summary.reached).toEqual(['boot'])
    expect(summary.waitingFor).toBe('link')
    expect(summary.fault?.hint).toMatch(/2\.4 GHz/)
  })

  it('clears a fault the board recovered from on its own', () => {
    // Three bad handshakes and then a join is a working board on a busy AP. Leaving "the
    // PSK is wrong" on screen after the link came up would be an outright lie.
    const summary = summarizeConsole(
      classify([
        'I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
        'W (5300) ff-wifi: disconnected (reason 15); reconnecting in 1000 ms',
        'W (7300) ff-wifi: disconnected (reason 15); reconnecting in 2000 ms',
        'I (9900) ff-net: wifi link up, ip 192.168.1.40 gw 192.168.1.1 mask 255.255.255.0',
      ]),
    )
    expect(summary.reached).toEqual(['boot', 'link'])
    expect(summary.fault).toBeNull()
  })

  it('points at the clock when enrolment cannot reach the API', () => {
    // The nastiest of the lot: the network is fine and the error mentions the URL, so the
    // operator goes hunting for a firewall. The real cause is two lines up.
    const summary = summarizeConsole(
      classify([
        'I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
        'I (900) ff-net: wifi link up, ip 192.168.1.40 gw 192.168.1.1 mask 255.255.255.0',
        'W (12500) ff-time: sntp: no answer from pool.ntp.org within 10000 ms; clock is still 1970-01-01T00:00:02Z',
        'E (13000) ff-enroll: cannot reach https://bingo.tvaroska.sk/v1/enroll: ESP_ERR_ESP_TLS_FAILED',
      ]),
    )
    expect(summary.waitingFor).toBe('clock')
    expect(summary.fault?.hint).toMatch(/clock milestone/)
  })
})
