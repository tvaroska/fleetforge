// The strip's rules, with no DOM. The ones that cost most if they regress: a stale bundle
// must read as "differ", and the verdict word must not be authored here (drift => silence).

import { describe, expect, it } from 'vitest'
import { type ArrivalSummary, type DeploySummary, type DeviceSummary } from './api'
import { type BuildInfo } from './buildInfo'
import { describeBoard, describeVersions } from './statusStrip'

const ui = (over: Partial<BuildInfo> = {}): BuildInfo => ({
  version: '0.4.2',
  commit: 'unknown',
  builtAt: 'unknown',
  ...over,
})
const ok = (version: string, commit?: string) =>
  ({ phase: 'ok', health: { status: 'ok', version, commit } }) as const

describe('describeVersions', () => {
  it('does not differ when versions match and commits are unknown', () => {
    const v = describeVersions(ui(), ok('0.4.2'))
    expect(v.differ).toBe(false)
    expect(v.api).toBe('0.4.2')
  })
  it('differs on a version mismatch', () => {
    expect(describeVersions(ui(), ok('0.4.3')).differ).toBe(true)
  })
  it('differs on same version with different commits', () => {
    const v = describeVersions(ui({ commit: '815596b2aaaa' }), ok('0.4.2', 'deadbeefdead'))
    expect(v.differ).toBe(true)
  })
  it('compares an 8-char prefix, so a full sha equals its short form', () => {
    const full = '815596b2' + 'c'.repeat(32)
    expect(describeVersions(ui({ commit: full }), ok('0.4.2', '815596b2')).differ).toBe(false)
  })
  it('cannot compare an unknown UI version', () => {
    expect(describeVersions(ui({ version: 'unknown' }), ok('0.4.2')).differ).toBe(false)
  })
  it('says checking / unreachable and never differs while the API is not known', () => {
    const loading = describeVersions(ui(), { phase: 'loading' })
    expect(loading.api).toBe('checking…')
    expect(loading.differ).toBe(false)
    const down = describeVersions(ui(), { phase: 'unreachable' })
    expect(down.api).toBe('unreachable')
    expect(down.differ).toBe(false)
  })
})

function device(over: Partial<DeviceSummary> = {}): DeviceSummary {
  return {
    device_id: 'a4cf12b3de90',
    name: null,
    group_id: null,
    platform_type: 'esp32c6',
    fw_version: '0.1.0',
    agent_version: '0.1.0',
    link_type: 'wifi',
    power_class: 'always_on',
    expected_wake_interval_s: null,
    parent_device_id: null,
    partition_layout: null,
    ota_slot_size: null,
    capabilities: [],
    last_seen: null,
    enrolled_at: '2026-09-10T11:00:00Z',
    broker_provisioned_at: null,
    online: true,
    deploy: null,
    ...over,
  }
}
function deploy(over: Partial<DeploySummary> = {}): DeploySummary {
  return {
    cmd_id: 'c',
    state: 'downloading',
    at: '2026-09-10T11:59:40Z',
    is_terminal: false,
    artifact_version: '1.5.0',
    from_version: '1.4.2',
    pct: null,
    detail: null,
    ...over,
  }
}
function arrival(over: Partial<ArrivalSummary> = {}): ArrivalSummary {
  return {
    device_id: 'b26a938324ab',
    stage: 'time_synced',
    detail: null,
    at: '2026-09-10T11:59:40Z',
    stalled: false,
    ...over,
  }
}
function board(line: ReturnType<typeof describeBoard>) {
  if (line.kind !== 'board') throw new Error(`expected a board, got ${line.kind}`)
  return line
}
const two = [device(), device({ device_id: 'b26a938324ab' })]

describe('describeBoard', () => {
  it('is loading before the first read', () => {
    expect(describeBoard(null, null, [])).toEqual({ kind: 'loading' })
  })
  it('says no boards / none selected', () => {
    expect(describeBoard(null, [], [])).toEqual({ kind: 'none', reason: 'no-boards' })
    expect(describeBoard(null, two, [])).toEqual({ kind: 'none', reason: 'not-selected' })
  })
  it('implies the only board', () => {
    expect(board(describeBoard(null, [device()], [])).deviceId).toBe('a4cf12b3de90')
  })
  it('shows firmware and presence for a selected idle board', () => {
    const b = board(describeBoard('a4cf12b3de90', two, []))
    expect(b.firmware).toBe('fw 0.1.0')
    expect(b.state).toEqual([{ text: 'online', tone: 'ok' }])
    expect(b.platform).toBe('esp32c6')
  })
  it('shows an in-flight deploy as from -> to', () => {
    const d = device({ fw_version: '1.4.2', deploy: deploy() })
    const b = board(describeBoard(d.device_id, [d], []))
    expect(b.firmware).toBe('fw 1.4.2 → 1.5.0')
    expect(b.state.map((s) => s.text)).toContain('updating: downloading the image')
  })
  it('claims good only when fw matches the confirmed artifact', () => {
    const d = device({
      fw_version: '1.5.0',
      deploy: deploy({ state: 'confirmed', is_terminal: true }),
    })
    expect(board(describeBoard(d.device_id, [d], [])).state).toContainEqual({
      text: 'last update good',
      tone: 'ok',
    })
  })
  it('says nothing about a verdict on drift', () => {
    const d = device({
      fw_version: '1.4.2',
      deploy: deploy({ state: 'confirmed', is_terminal: true }),
    })
    const text = board(describeBoard(d.device_id, [d], [])).state.map((s) => s.text).join(' ')
    expect(text).not.toMatch(/good/)
    expect(text).not.toMatch(/last update/)
  })
  it('says rolled back and failed in the bad tone', () => {
    const rb = device({ deploy: deploy({ state: 'rolled_back', is_terminal: true }) })
    expect(board(describeBoard(rb.device_id, [rb], [])).state).toContainEqual({
      text: 'last update rolled back',
      tone: 'bad',
    })
    const f = device({ deploy: deploy({ state: 'failed', is_terminal: true }) })
    const seg = board(describeBoard(f.device_id, [f], [])).state.find((s) =>
      s.text.startsWith('last update'),
    )
    expect(seg?.tone).toBe('bad')
    expect(seg?.text).toMatch(/^last update: /)
  })
  it('renders an unknown future state as itself', () => {
    const d = device({ deploy: deploy({ state: 'exploding' }) })
    expect(board(describeBoard(d.device_id, [d], [])).state.map((s) => s.text)).toContain(
      'updating: exploding',
    )
  })
  it('uses an em dash for a board with no firmware', () => {
    const d = device({ fw_version: null })
    expect(board(describeBoard(d.device_id, [d], [])).firmware).toBe('fw —')
  })
  it('describes an arrival, with the word stalled', () => {
    const b = board(describeBoard('b26a938324ab', [device()], [arrival({ stalled: true })]))
    expect(b.state).toEqual([
      { text: 'arriving: clock set', tone: null },
      { text: 'stalled', tone: 'bad' },
    ])
  })
  it('says not on the fleet yet for an id seen nowhere', () => {
    const b = board(describeBoard('ffffffffffff', [device()], []))
    expect(b.state).toEqual([{ text: 'not on the fleet yet', tone: null }])
    expect(b.platform).toBeNull()
  })
})
