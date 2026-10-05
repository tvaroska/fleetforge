// The pre-flight card's rules, with no DOM.

import { describe, expect, it } from 'vitest'
import {
  type AgentManifest,
  type ArrivalSummary,
  type DeploySummary,
  type DeviceSummary,
} from './api'
import { type ChipInfo } from './flasher'
import { describePreflight, installFor } from './preflight'

const device = (over: Partial<DeviceSummary> = {}): DeviceSummary => ({
  device_id: 'a4cf12b3de90',
  name: null,
  group_id: null,
  platform_type: 'esp32',
  fw_version: '1.4.2',
  agent_version: '0.4.5',
  link_type: 'wifi',
  ssid: null,
  known_networks: null,
  power_class: 'always_on',
  expected_wake_interval_s: null,
  parent_device_id: null,
  partition_layout: 'ab-4m-v1',
  ota_slot_size: 1966080,
  capabilities: ['ota'],
  last_seen: '2026-09-10T11:59:30Z',
  enrolled_at: '2026-09-10T11:00:00Z',
  broker_provisioned_at: null,
  online: true,
  deploy: null,
  ...over,
})
const deploy = (over: Partial<DeploySummary> = {}): DeploySummary => ({
  cmd_id: 'c',
  state: 'rebooting',
  at: '2026-09-10T11:59:00Z',
  is_terminal: false,
  artifact_version: '1.5.0',
  from_version: '1.4.2',
  pct: null,
  detail: null,
  ...over,
})
const arrival = (over: Partial<ArrivalSummary> = {}): ArrivalSummary => ({
  device_id: 'a4cf12b3de90',
  stage: 'wifi',
  detail: null,
  at: '2026-09-10T11:59:00Z',
  stalled: false,
  ...over,
})
const install = { agentVersion: '0.4.5', layout: 'ab-4m-v1' }
const base = {
  predictedId: 'a4cf12b3de90',
  devices: [] as DeviceSummary[],
  arrivals: [],
  fleetError: null,
  install: null,
}

describe('describePreflight', () => {
  it('is unknown-id without a predicted id, even with a loaded fleet', () => {
    expect(describePreflight({ ...base, predictedId: null, devices: [device()] }).kind).toBe(
      'unknown-id',
    )
  })
  it('is checking while the fleet is loading, carrying the error', () => {
    expect(describePreflight({ ...base, devices: null, fleetError: 'boom' })).toEqual({
      kind: 'checking',
      error: 'boom',
    })
  })
  it('finds a known board case-insensitively and copies its fields', () => {
    const p = describePreflight({
      ...base,
      predictedId: 'A4CF12B3DE90',
      devices: [device({ name: 'porch', online: false })],
    })
    expect(p).toMatchObject({
      kind: 'known',
      deviceId: 'a4cf12b3de90',
      name: 'porch',
      platform: 'esp32',
      firmware: '1.4.2',
      online: false,
      lastSeen: '2026-09-10T11:59:30Z',
    })
  })
  it('labels a non-terminal deploy and ignores a terminal one', () => {
    const live = describePreflight({
      ...base,
      devices: [device({ deploy: deploy() })],
    })
    expect(live).toMatchObject({ kind: 'known', updating: expect.any(String) })
    expect((live as { updating: string }).updating).not.toBe('')
    const done = describePreflight({
      ...base,
      devices: [device({ deploy: deploy({ state: 'committed', is_terminal: true }) })],
    })
    expect(done).toMatchObject({ updating: null })
  })
  it('flags a layout change only when both layouts are known and differ', () => {
    const run = (layout: string | null, inst: typeof install | null) =>
      describePreflight({
        ...base,
        devices: [device({ partition_layout: layout })],
        install: inst,
      })
    expect(run('other', install)).toMatchObject({
      layoutChange: { from: 'other', to: 'ab-4m-v1' },
    })
    expect(run('ab-4m-v1', install)).toMatchObject({ layoutChange: null })
    expect(run(null, install)).toMatchObject({ layoutChange: null })
    expect(run('other', null)).toMatchObject({ layoutChange: null })
  })
  it('is new with and without an arrival, labelling the stage', () => {
    expect(
      describePreflight({
        ...base,
        devices: [device({ device_id: 'b26a938324ab' })],
      }),
    ).toMatchObject({
      kind: 'new',
      arrival: null,
    })
    const seen = describePreflight({
      ...base,
      arrivals: [arrival({ stalled: true })],
      install,
    })
    expect(seen).toMatchObject({
      kind: 'new',
      install,
      arrival: { stalled: true },
    })
    const odd = describePreflight({
      ...base,
      arrivals: [arrival({ stage: 'zz-future' })],
    })
    expect(odd).toMatchObject({ arrival: { stage: 'zz-future' } })
  })
})

describe('installFor', () => {
  const chip = { chipName: 'ESP32' } as ChipInfo
  const build = (chip_family: string) => ({
    chip_family,
    agent_version: '0.4.5',
    partition_layout: 'ab-4m-v1',
  })
  const manifest = (...fam: string[]) =>
    ({
      agent_version: '0.4.5',
      builds: fam.map(build),
    }) as unknown as AgentManifest
  it('returns the one build for the chip', () => {
    expect(installFor(manifest('ESP32', 'ESP32-S3'), chip)).toEqual(install)
  })
  it('returns null for none, several, or no manifest, and never throws', () => {
    expect(installFor(manifest('ESP32-S3'), chip)).toBeNull()
    expect(installFor(manifest('ESP32', 'ESP32'), chip)).toBeNull()
    expect(installFor(null, chip)).toBeNull()
  })
})
