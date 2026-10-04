// R2b-fe-5: the server's half of the watch. What this defends:
//
// 1. **A native-USB port loss is not "no board".** The device list alone can mark Enrolled
//    and On the fleet.
// 2. **The re-flash trap stays shut.** A known board's stale `online: true` row never marks
//    anything until the server shows a NEW enrolment and the board speaking after it.
// 3. **The console's classifier is untouched**; the merge re-applies its furthest-reached
//    rule.

import { describe, expect, it } from 'vitest'
import type { DeviceSummary } from './api'
import {
  MILESTONE_DEADLINE_MS,
  classifyConsoleLine,
  summarizeConsole,
  type ConsoleSummary,
} from './boardConsole'
import {
  SERVER_WAIT_MS,
  describeServerView,
  lastDeviceId,
  mergeServerView,
  takeBaseline,
} from './serverWatch'

const ID = 'a4cf12b3de90'
const E0 = '2026-10-04T10:00:00Z'
const E1 = '2026-10-04T12:00:00Z'

const row = (over: Partial<DeviceSummary> = {}): DeviceSummary => ({
  device_id: ID,
  name: null,
  group_id: null,
  platform_type: 'esp32',
  fw_version: '0.4.0',
  agent_version: '0.4.0',
  link_type: 'wifi',
  power_class: 'always_on',
  expected_wake_interval_s: null,
  parent_device_id: null,
  partition_layout: 'ab-4m-v1',
  ota_slot_size: 1966080,
  capabilities: ['ota'],
  last_seen: null,
  enrolled_at: E0,
  broker_provisioned_at: null,
  online: false,
  deploy: null,
  ...over,
})

const EMPTY: ConsoleSummary = summarizeConsole([], 0)

function summaryOf(lines: string[], now = 10_000): ConsoleSummary {
  return summarizeConsole(
    lines.map((raw, i) => ({ ...classifyConsoleLine(raw, i), at: 0 })),
    now,
  )
}

describe('describeServerView (R2b-fe-5)', () => {
  it('a new board after a flash: enrolled once provisioned, on the fleet once it spoke after', () => {
    const baseline = takeBaseline([])
    const enrolledRow = row({ broker_provisioned_at: '2026-10-04T10:00:01Z' })
    expect(
      describeServerView({ deviceId: ID, devices: [enrolledRow], baseline, expectEnroll: true }),
    ).toEqual({ deviceId: ID, enrolled: true, onFleet: false })

    const online = { ...enrolledRow, online: true, last_seen: '2026-10-04T10:00:05Z' }
    expect(
      describeServerView({ deviceId: ID, devices: [online], baseline, expectEnroll: true }),
    ).toEqual({ deviceId: ID, enrolled: true, onFleet: true })
  })

  it('the re-flash trap: a known board’s stale online row marks nothing', () => {
    const stale = row({
      broker_provisioned_at: '2026-10-04T10:00:01Z',
      online: true,
      last_seen: '2026-10-04T11:59:00Z',
    })
    const baseline = takeBaseline([stale])
    expect(
      describeServerView({ deviceId: ID, devices: [stale], baseline, expectEnroll: true }),
    ).toEqual({ deviceId: ID, enrolled: false, onFleet: false })
  })

  it('a re-enrolled board whose last_seen predates the new enrolment is not on the fleet', () => {
    const stale = row({
      broker_provisioned_at: '2026-10-04T10:00:01Z',
      online: true,
      last_seen: '2026-10-04T11:59:00Z',
    })
    const baseline = takeBaseline([stale])
    const reEnrolled = { ...stale, enrolled_at: E1, broker_provisioned_at: '2026-10-04T12:00:01Z' }
    expect(
      describeServerView({ deviceId: ID, devices: [reEnrolled], baseline, expectEnroll: true }),
    ).toEqual({ deviceId: ID, enrolled: true, onFleet: false })
    const spoke = { ...reEnrolled, last_seen: '2026-10-04T12:00:09Z' }
    expect(
      describeServerView({ deviceId: ID, devices: [spoke], baseline, expectEnroll: true }),
    ).toEqual({ deviceId: ID, enrolled: true, onFleet: true })
  })

  it('no credential in dynsec yet is not enrolled', () => {
    const r = row({ enrolled_at: E1, online: true, last_seen: '2026-10-04T12:00:09Z' })
    expect(
      describeServerView({
        deviceId: ID,
        devices: [r],
        baseline: takeBaseline([]),
        expectEnroll: true,
      }),
    ).toEqual({ deviceId: ID, enrolled: false, onFleet: false })
  })

  it('no flash here: a held credential is enrolled; on the fleet only once last_seen moves', () => {
    const held = row({
      broker_provisioned_at: '2026-10-04T10:00:01Z',
      online: true,
      last_seen: '2026-10-04T11:00:00Z',
    })
    const baseline = takeBaseline([held])
    expect(
      describeServerView({ deviceId: ID, devices: [held], baseline, expectEnroll: false }),
    ).toEqual({ deviceId: ID, enrolled: true, onFleet: false })
    const moved = { ...held, last_seen: '2026-10-04T11:00:30Z' }
    expect(
      describeServerView({ deviceId: ID, devices: [moved], baseline, expectEnroll: false }),
    ).toEqual({ deviceId: ID, enrolled: true, onFleet: true })
  })

  it('matches ids case-insensitively and needs an id, a fleet and a baseline', () => {
    const r = row({ device_id: ID.toUpperCase(), broker_provisioned_at: E0 })
    const baseline = takeBaseline([])
    expect(
      describeServerView({ deviceId: ID, devices: [r], baseline, expectEnroll: true })?.enrolled,
    ).toBe(true)
    expect(
      describeServerView({ deviceId: null, devices: [r], baseline, expectEnroll: true }),
    ).toBeNull()
    expect(
      describeServerView({ deviceId: ID, devices: null, baseline, expectEnroll: true }),
    ).toBeNull()
    expect(
      describeServerView({ deviceId: ID, devices: [r], baseline: null, expectEnroll: true }),
    ).toBeNull()
    expect(
      describeServerView({ deviceId: ID, devices: [], baseline, expectEnroll: true }),
    ).toEqual({ deviceId: ID, enrolled: false, onFleet: false })
  })

  it('waits the console’s own budget for the two server milestones', () => {
    expect(SERVER_WAIT_MS).toBe(MILESTONE_DEADLINE_MS.enroll + MILESTONE_DEADLINE_MS.fleet)
  })
})

describe('lastDeviceId (R2b-fe-5)', () => {
  it('survives a later reset banner with no ff-id after it', () => {
    const events = [
      'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
      `I (120) ff-id: device_id ${ID.toUpperCase()}`,
      'rst:0x15 (USB_UART_CHIP_RESET),boot:0x8 (SPI_FAST_FLASH_BOOT)',
    ].map((raw, i) => classifyConsoleLine(raw, i))
    expect(lastDeviceId(events)).toBe(ID)
  })

  it('reads the mqtt line too, and nothing when the board never said', () => {
    const events = [`I (3100) ff-mqtt: mqtt connected as ${ID} (mqtts://x:8883)`].map((raw, i) =>
      classifyConsoleLine(raw, i),
    )
    expect(lastDeviceId(events)).toBe(ID)
    expect(lastDeviceId([])).toBeNull()
  })
})

describe('mergeServerView (R2b-fe-5)', () => {
  const BOOT_AND_LINK = [
    'I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
    'I (900) ff-net: wifi link up, ip 192.168.1.40 gw 192.168.1.1 mask 255.255.255.0',
  ]

  it('null server: the summary is returned untouched', () => {
    const summary = summaryOf(BOOT_AND_LINK)
    const merged = mergeServerView(summary, null, true)
    expect(merged.summary).toEqual(summary)
    expect(merged.fromServer).toEqual([])
  })

  it('enrolled and on the fleet: furthest-reached rule re-applied', () => {
    const merged = mergeServerView(
      summaryOf(BOOT_AND_LINK),
      { deviceId: ID, enrolled: true, onFleet: true },
      false,
    )
    expect(merged.summary.reached).toEqual(['boot', 'link', 'enroll', 'fleet'])
    expect(merged.summary.skipped).toEqual(['clock'])
    expect(merged.summary.waitingFor).toBeNull()
    expect(merged.fromServer).toEqual(['enroll', 'fleet'])
  })

  it('enrolled only: waiting for the fleet', () => {
    const merged = mergeServerView(
      summaryOf(BOOT_AND_LINK),
      { deviceId: ID, enrolled: true, onFleet: false },
      false,
    )
    expect(merged.summary.waitingFor).toBe('fleet')
    expect(merged.summary.skipped).toEqual(['clock'])
    expect(merged.fromServer).toEqual(['enroll'])
  })

  it('fromServer excludes what the console reached itself', () => {
    const summary = summaryOf([
      ...BOOT_AND_LINK,
      'I (1500) ff-time: sntp: 1970-01-01T00:00:02Z -> 2026-09-10T21:00:00Z (via pool.ntp.org)',
      'I (2600) ff-enroll: enroll 200 https://bingo.tvaroska.sk/v1/enroll',
    ])
    const merged = mergeServerView(summary, { deviceId: ID, enrolled: true, onFleet: true }, false)
    expect(merged.fromServer).toEqual(['fleet'])
  })

  it('clears overdue: on the fleet, already passed, or the console stopped', () => {
    // Stalled at `clock` (link reached at 0, now past the clock deadline).
    const stalled = summaryOf(BOOT_AND_LINK, MILESTONE_DEADLINE_MS.clock + 1_000)
    expect(stalled.overdue?.milestone).toBe('clock')
    const none = { deviceId: ID, enrolled: false, onFleet: false }

    expect(mergeServerView(stalled, none, false).summary.overdue?.milestone).toBe('clock')
    expect(
      mergeServerView(stalled, { deviceId: ID, enrolled: true, onFleet: true }, false).summary
        .overdue,
    ).toBeNull()
    // Enrolled implies the clock was passed.
    expect(
      mergeServerView(stalled, { deviceId: ID, enrolled: true, onFleet: false }, false).summary
        .overdue,
    ).toBeNull()
    expect(mergeServerView(stalled, none, true).summary.overdue).toBeNull()
  })

  it('clears the fault only once the server sees the board on the fleet', () => {
    const faulted = summaryOf([
      ...BOOT_AND_LINK,
      'E (2600) ff-enroll: enroll 409: this token is already used, revoked or expired.',
    ])
    expect(faulted.fault).not.toBeNull()
    expect(
      mergeServerView(faulted, { deviceId: ID, enrolled: true, onFleet: false }, false).summary
        .fault,
    ).toEqual(faulted.fault)
    expect(
      mergeServerView(faulted, { deviceId: ID, enrolled: true, onFleet: true }, false).summary
        .fault,
    ).toBeNull()
  })

  it('with zero console events the server alone reaches the fleet', () => {
    const merged = mergeServerView(EMPTY, { deviceId: ID, enrolled: true, onFleet: true }, true)
    expect(merged.summary.reached).toEqual(['enroll', 'fleet'])
    expect(merged.summary.skipped).toEqual(['boot', 'link', 'clock'])
  })
})
