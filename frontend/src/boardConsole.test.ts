// Every log line quoted here is a real format string from `agent/main/*.c`. That is the
// contract this file defends: if someone rewords `ff_net_wifi.c`'s "disconnected (reason
// %d)" and the classifier is not updated, the panel silently stops naming causes and the
// operator is back to guessing. These tests fail loudly instead.

import { describe, expect, it } from 'vitest'
import {
  MILESTONE_DEADLINE_MS,
  MILESTONES,
  classifyConsoleLine,
  summarizeConsole,
  type ConsoleEvent,
} from './boardConsole'
import { BENCH_2026_09_11 } from './fixtures/bench-2026-09-11'

/** "Now" for a replay. Anchored to the real clock so the tests that do not care about time
 *  still see a summary taken moments after the last line, as a bench operator would. */
const T0 = Date.now()

/** Lines a second apart, so a summary can be asked what it thinks at a given moment. */
const classify = (lines: string[]): ConsoleEvent[] =>
  lines.map((line, index) => classifyConsoleLine(line, index, T0 + index * 1000))

const at = (events: ConsoleEvent[]) => T0 + events.length * 1000

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

  // S0-fe-4. None of these carry `E (1234) tag:`, so before this they were `plain`/`null`
  // and could not become a fault however many times the board printed them.
  describe('lines with no ESP-IDF preamble at all', () => {
    it('names a brownout, which is the most common first-board failure there is', () => {
      const event = classifyConsoleLine('E BOD: Brownout detector was triggered', 0)
      expect(event.level).toBe('error')
      expect(event.hint).toMatch(/browning out/)
      expect(event.hint).toMatch(/not a hub/)
    })

    it('names it from the reset banner too, which is all a fast loop leaves behind', () => {
      const event = classifyConsoleLine(
        'rst:0xf (RTCWDT_BROWN_OUT_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
        0,
      )
      expect(event.level).toBe('error')
      expect(event.hint).toMatch(/browned out/)
      expect(event.bootMarker).toBe('rom')
    })

    it('leaves an ordinary power-on banner alone', () => {
      // It is the normal top of every boot. Calling it a fault would make the panel cry
      // wolf on every single board, which is how a diagnosis stops being read.
      const event = classifyConsoleLine(
        'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
        0,
      )
      expect(event.level).toBe('plain')
      expect(event.hint).toBeNull()
      expect(event.bootMarker).toBe('rom')
    })

    it.each([
      ["Guru Meditation Error: Core  0 panic'ed (LoadProhibited). Exception was unhandled.", /crashed/],
      ['assert failed: ff_cfg_load ff_cfg.c:88 (crc == expected)', /crashed/],
      ['waiting for download', /flashing bootloader/],
      ['invalid header: 0xffffffff', /no usable firmware/],
    ])('classifies %s as a fault', (line, expected) => {
      const event = classifyConsoleLine(line, 0)
      expect(event.level).toBe('error')
      expect(event.hint).toMatch(expected)
    })

    it('lets the panic keep the diagnosis, not the backtrace under it', () => {
      const summary = summarizeConsole(
        classify([
          'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
          "Guru Meditation Error: Core  0 panic'ed (LoadProhibited). Exception was unhandled.",
          'Backtrace: 0x400d1a2b:0x3ffb1f30 0x400d1c55:0x3ffb1f50',
        ]),
      )
      expect(summary.fault?.hint).toMatch(/crashed/)
      expect(summary.fault?.text).toMatch(/Guru Meditation/)
    })
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

  // ── S0-fe-4: what this boot proved, not what the session ever saw ───────────────────
  describe('a board that reboots', () => {
    const REBOOTED = [
      'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
      'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
      'I (900) ff-net: wifi link up, ip 192.168.1.57 gw 192.168.1.1 mask 255.255.255.0',
      'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
      'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
      'I (300) ff-wifi: wifi sta starting, ssid bench-2g',
    ]

    it('retracts what the previous boot proved', () => {
      const summary = summarizeConsole(classify(REBOOTED))
      // The 2026-09-11 bug in one assertion: `link` was reached, once, and then was not.
      expect(summary.reached).toEqual(['boot'])
      expect(summary.waitingFor).toBe('link')
    })

    it('counts the boot pair as one boot, not two', () => {
      // The ROM banner and the agent's banner belong to the same boot. Counting both would
      // report every board in existence as looping.
      expect(summarizeConsole(classify(REBOOTED)).boots).toBe(2)
    })

    it('calls a restart that never reached the fleet a loop', () => {
      const summary = summarizeConsole(classify(REBOOTED))
      expect(summary.rebootLoop).toEqual({ boots: 2 })
    })

    it('does not call a deliberate reboot of a working board a loop', () => {
      // The operator presses "Reboot the board" to read a log from the top. That must not
      // be reported as a fault, or the panel is crying wolf on the happy path.
      const summary = summarizeConsole(
        classify([
          'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
          'I (900) ff-net: wifi link up, ip 192.168.1.57 gw 192.168.1.1 mask 255.255.255.0',
          'I (1500) ff-time: sntp: 1970-01-01T00:00:02Z -> 2026-09-11T09:00:00Z (via pool.ntp.org)',
          'I (2600) ff-enroll: enroll 200 https://bingo.tvaroska.sk/v1/enroll',
          'I (3100) ff-mqtt: mqtt connected as 3c8427b1f0a4 (mqtts://bingo.tvaroska.sk:8883)',
          'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
          'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
        ]),
      )
      expect(summary.boots).toBe(2)
      expect(summary.rebootLoop).toBeNull()
    })

    it('stops calling it a loop once the board finally gets on the fleet', () => {
      const summary = summarizeConsole(
        classify([
          'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
          'rst:0xf (RTCWDT_BROWN_OUT_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
          'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
          'I (900) ff-net: wifi link up, ip 192.168.1.57 gw 192.168.1.1 mask 255.255.255.0',
          'I (1500) ff-time: sntp: 1970-01-01T00:00:02Z -> 2026-09-11T09:00:00Z (via pool.ntp.org)',
          'I (2600) ff-enroll: enroll 200 https://bingo.tvaroska.sk/v1/enroll',
          'I (3100) ff-mqtt: mqtt connected as 3c8427b1f0a4 (mqtts://bingo.tvaroska.sk:8883)',
        ]),
      )
      expect(summary.rebootLoop).toBeNull()
      expect(summary.reached).toEqual([...MILESTONES])
    })
  })

  // ── S0-fe-4: no unbounded wait (spec/standards.md) ──────────────────────────────────
  describe('a milestone that never arrives', () => {
    const STUCK = classify([
      'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
      'I (300) ff-wifi: wifi sta starting, ssid bench-2g',
    ])

    it('says nothing while the board is still within its own budget', () => {
      // `agent_main.c` waits NET_TIMEOUT_MS 30 s for a link. Complaining at 5 s would be
      // the panel inventing a fault the board has not had yet.
      expect(summarizeConsole(STUCK, at(STUCK) + 10_000).overdue).toBeNull()
    })

    it('names the stall once the deadline passes, with no new line to prompt it', () => {
      const summary = summarizeConsole(STUCK, at(STUCK) + MILESTONE_DEADLINE_MS.link + 1_000)
      expect(summary.overdue?.milestone).toBe('link')
      expect(summary.overdue?.hint).toMatch(/5 GHz/)
      expect(summary.overdue?.waitedMs).toBeGreaterThanOrEqual(MILESTONE_DEADLINE_MS.link)
    })

    it('measures each milestone from the one before it, not from the boot', () => {
      const events = classify([
        'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
        'I (900) ff-net: wifi link up, ip 192.168.1.57 gw 192.168.1.1 mask 255.255.255.0',
      ])
      // A link that took its full budget must not instantly declare the clock overdue.
      expect(summarizeConsole(events, at(events) + 10_000).overdue).toBeNull()
      const late = summarizeConsole(events, at(events) + MILESTONE_DEADLINE_MS.clock + 1_000)
      expect(late.overdue?.milestone).toBe('clock')
      expect(late.overdue?.hint).toMatch(/NTP/)
    })
  })

  // ── S0-fe-4 acceptance: the session that started all of this ────────────────────────
  describe('the bench session of 2026-09-11', () => {
    const events = classify(BENCH_2026_09_11)
    const summary = summarizeConsole(events, at(events))

    it('names the brownout as the fault', () => {
      // The operator spent a session unable to learn this from anything but a UART cable.
      expect(summary.fault?.hint).toMatch(/browning out/)
      expect(summary.fault?.text).toMatch(/Brownout detector was triggered/)
    })

    it('shows the reboot loop', () => {
      expect(summary.boots).toBe(3)
      expect(summary.rebootLoop).toEqual({ boots: 3 })
    })

    it('does NOT claim the network is up', () => {
      // One early cycle did reach `link up`, and the ✓ it left behind is what sent the
      // diagnosis in the wrong direction for most of the session.
      expect(summary.reached).not.toContain('link')
      expect(summary.reached).toEqual(['boot'])
      expect(summary.waitingFor).toBe('link')
    })
  })
})
