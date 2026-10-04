// R2b-fe-3: the result card's judgement, over the same logs the panel tests use.
//
// The fixtures below are copies of the ones in `BoardConsole.test.tsx` (a test file cannot
// be imported without running its suites); the bench logs come from `fixtures/`.

import { describe, expect, it } from 'vitest'
import type { DeviceSummary } from './api'
import {
  MILESTONE_DEADLINE_MS,
  classifyConsoleLine,
  summarizeConsole,
  type ConsoleEvent,
} from './boardConsole'
import { BENCH_2026_09_11 } from './fixtures/bench-2026-09-11'
import { BENCH_2026_10_04 } from './fixtures/bench-2026-10-04'
import {
  CAUSE_NEXT,
  consoleFacts,
  describeOnboardingResult,
  type OnboardingResult,
  type ResultContext,
} from './onboardingResult'

const HAPPY = [
  'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
  'I (120) ff-id: device_id a4cf12b3de90',
  'I (800) ff-wifi: associated; waiting for DHCP',
  'I (900) ff-net: wifi link up, ip 192.168.1.40 gw 192.168.1.1 mask 255.255.255.0',
  'I (1500) ff-time: sntp: 1970-01-01T00:00:02Z -> 2026-09-10T21:00:00Z (via pool.ntp.org)',
  'I (2600) ff-enroll: enroll 200 https://bingo.tvaroska.sk/v1/enroll',
  'I (3100) ff-mqtt: mqtt connected as a4cf12b3de90 (mqtts://bingo.tvaroska.sk:8883)',
]

const SPENT_TOKEN = [
  'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
  'I (900) ff-net: wifi link up, ip 192.168.1.40 gw 192.168.1.1 mask 255.255.255.0',
  'I (1500) ff-time: sntp: 1970-01-01T00:00:02Z -> 2026-09-11T08:14:05Z (via pool.ntp.org)',
  'E (2600) ff-enroll: enroll 409: this token is already used, revoked or expired.',
  "E (2900) ff-agent: halted: this board's enrollment token was refused for good — re-flash " +
    'ff_cfg with a fresh ffe_ token (POST /v1/enrollment-tokens)',
]

const UNEXPLAINED_LOOP = [
  'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
  'I (120) ff-id: device_id a4cf12b3de90',
  'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
  'I (120) ff-id: device_id a4cf12b3de90',
  'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
  'I (120) ff-id: device_id a4cf12b3de90',
]

const SILENT_BOARD = [
  'I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
  'I (120) ff-id: device_id a4cf12b3de90',
  'I (300) ff-wifi: wifi sta starting, ssid home-5g',
  'W (5300) ff-wifi: disconnected (reason 201); reconnecting in 1000 ms',
  'W (6300) ff-agent: no network yet; waiting for the link',
]

const T0 = Date.parse('2026-10-04T12:00:00Z')

/** Every line lands at T0, so `now` alone decides whether a deadline has passed. */
const eventsOf = (lines: string[]): ConsoleEvent[] =>
  lines.map((line, seq) => classifyConsoleLine(line, seq, T0))

function judge(lines: string[], now = T0 + 1_000, context: ResultContext | null = null) {
  const events = eventsOf(lines)
  return describeOnboardingResult({ events, summary: summarizeConsole(events, now), context })
}

const value = (result: OnboardingResult | null, label: string) =>
  result?.rows.find((row) => row.label === label)?.value

const device = (over: Partial<DeviceSummary> = {}): DeviceSummary => ({
  device_id: 'A4CF12B3DE90',
  name: null,
  group_id: null,
  platform_type: 'esp32',
  fw_version: '9.9.9',
  agent_version: '0.3.0',
  link_type: 'wifi',
  power_class: 'always_on',
  expected_wake_interval_s: null,
  parent_device_id: null,
  partition_layout: 'ab-4m-v1',
  ota_slot_size: 1966080,
  capabilities: ['ota'],
  last_seen: '2026-10-04T11:59:30Z',
  enrolled_at: '2026-10-04T11:00:00Z',
  broker_provisioned_at: null,
  online: true,
  deploy: null,
  ...over,
})

describe('describeOnboardingResult', () => {
  it('HAPPY is a success with every fact the console gave', () => {
    const result = judge(HAPPY)
    expect(result?.outcome).toBe('success')
    expect(value(result, 'Device id')).toBe('a4cf12b3de90')
    expect(value(result, 'Firmware')).toBe('0.1.0')
    expect(value(result, 'Link')).toContain('192.168.1.40')
    expect(value(result, 'Clock source')).toBe('NTP (pool.ntp.org)')
    expect(value(result, 'Enrolled')).toBe('yes')
    expect(value(result, 'On the fleet')).toBe('yes')
    // No versions passed: no versions row.
    expect(value(result, 'UI / API')).toBeUndefined()
  })

  it('names the clock source on the 2026-10-04 bench log (S0-bug-1)', () => {
    const result = judge(BENCH_2026_10_04)
    expect(result?.outcome).toBe('success')
    expect(value(result, 'Clock source')).toBe(
      'Kept across the reset (no answer from pool.ntp.org)',
    )
    expect(value(result, 'Enrolled')).toBe('yes')
    expect(value(result, 'Device id')).toBe('94a990dd09a4')
  })

  it('says the clock was not set when the year held was not sane', () => {
    const result = judge(
      [
        'I (100) ff-agent: fleetforge agent 0.3.2 (idf v5.5.5), built Sep 23 2026 19:00:00',
        'I (2100) ff-net: wifi link up, ip 192.168.1.57 gw 192.168.1.1 mask 255.255.255.0',
        'W (17200) ff-time: sntp: no answer from pool.ntp.org within 15000 ms; clock is still 1970-01-01T00:00:17Z',
      ],
      T0 + MILESTONE_DEADLINE_MS.clock + 1_000,
    )
    expect(result?.outcome).toBe('failure')
    expect(value(result, 'Clock source')).toMatch(/^Not set/)
    expect(result?.outcome === 'failure' && result.cause).toBe('clock')
  })

  it('a silent Wi-Fi board past the link deadline is a Wi-Fi failure with no action', () => {
    const result = judge(SILENT_BOARD, T0 + MILESTONE_DEADLINE_MS.link + 2_000)
    expect(result?.outcome).toBe('failure')
    if (result?.outcome !== 'failure') return
    expect(result.cause).toBe('wifi')
    expect(result.remedy).toBeNull()
    expect(result.next).toBe(CAUSE_NEXT.wifi)
    expect(value(result, 'Link')).toBe('—')
    expect(value(result, 'Enrolled')).toBe('no')
  })

  it('gives no card while the board is still before a deadline', () => {
    expect(judge(SILENT_BOARD, T0 + 1_000)).toBeNull()
  })

  it('a spent token is a token failure whose action is a re-flash', () => {
    const result = judge(SPENT_TOKEN)
    expect(result?.outcome).toBe('failure')
    if (result?.outcome !== 'failure') return
    expect(result.cause).toBe('token')
    expect(result.remedy).toBe('reflash')
    expect(result.headline).toMatch(/enrolment refused/i)
  })

  it('the 2026-09-11 brownout is a power failure with no button', () => {
    const result = judge(BENCH_2026_09_11)
    expect(result?.outcome).toBe('failure')
    if (result?.outcome !== 'failure') return
    expect(result.cause).toBe('power')
    expect(result.remedy).toBeNull()
  })

  it('a loop the log does not explain is called power', () => {
    const result = judge(UNEXPLAINED_LOOP)
    expect(result?.outcome === 'failure' && result.cause).toBe('power')
  })

  it('reads facts from this boot only', () => {
    const facts = consoleFacts(
      eventsOf([
        'I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
        'I (120) ff-id: device_id a4cf12b3de90',
        'I (900) ff-net: wifi link up, ip 192.168.1.40 gw 192.168.1.1 mask 255.255.255.0',
        'rst:0xf (RTCWDT_BROWN_OUT_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
        'I (100) ff-agent: fleetforge agent 0.2.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
      ]),
    )
    expect(facts.link).toBeNull()
    expect(facts.deviceId).toBeNull()
    expect(facts.agentVersion).toBe('0.2.0')
  })

  it('reads an ethernet link', () => {
    const facts = consoleFacts(
      eventsOf(['I (900) ff-net: eth link up, ip 10.0.2.15 gw 10.0.2.2 mask 255.255.255.0']),
    )
    expect(facts.link).toEqual({ type: 'ethernet', ip: '10.0.2.15' })
  })

  it('enriches the rows from the device row, the versions and what was flashed', () => {
    const lines = HAPPY.filter((line) => !line.includes('ff-wifi'))
    const result = judge(lines, T0 + 1_000, {
      devices: [device()],
      versions: { ui: '0.4.2', uiTitle: '', api: '0.4.3', differ: true },
      flashed: {
        deviceId: 'a4cf12b3de90',
        agentVersion: '0.1.0',
        layout: 'other-layout',
        link: 'wifi',
        ssid: 'shed',
      },
    })
    expect(value(result, 'Partition layout')).toBe('ab-4m-v1')
    expect(value(result, 'On the fleet')).toBe('yes · server: online')
    expect(value(result, 'Link')).toBe('Wi-Fi shed · ip 192.168.1.40')
    expect(value(result, 'UI / API')).toBe('UI 0.4.2 · API 0.4.3')
    expect(result?.versionsDiffer).toBe(true)
    // The console's own version wins over the (possibly stale) device row.
    expect(value(result, 'Firmware')).toBe('0.1.0')
  })

  it('says the server has not seen a board that is not on the fleet yet', () => {
    const result = judge(HAPPY, T0 + 1_000, { devices: [], versions: null, flashed: null })
    expect(value(result, 'On the fleet')).toBe('yes · server: not seen yet')
  })

  it('gives no card for no events', () => {
    expect(
      describeOnboardingResult({ events: [], summary: summarizeConsole([], T0), context: null }),
    ).toBeNull()
  })
})
