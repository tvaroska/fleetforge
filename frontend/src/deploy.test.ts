// `deployOutcome` is the rule that decides whether a finished deploy may say `good`.
// Every row of the R2-fe-1 table is pinned here, without rendering.

import { describe, expect, it } from 'vitest'
import { type DeploySummary, type DeviceSummary } from './api'
import {
  DEPLOY_BAD_STATES,
  DEPLOY_STATE_LABELS,
  ROLLBACK_WAIT_LABEL,
  deployOutcome,
  deployStateLabel,
  describeDevicePresence,
  devicePriorityScore,
  isCommandRollback,
  safeWindowWait,
} from './deploy'

function deploy(overrides: Partial<DeploySummary> = {}): DeploySummary {
  return {
    cmd_id: 'cmd-a',
    state: 'confirmed',
    at: '2026-09-10T11:59:40Z',
    is_terminal: true,
    artifact_version: '1.5.0',
    from_version: '1.4.2',
    pct: 100,
    detail: null,
    ...overrides,
  }
}

describe('deployOutcome', () => {
  it('says good when the board still runs the confirmed version', () => {
    expect(deployOutcome(deploy(), '1.5.0')).toEqual({
      kind: 'verdict',
      word: 'good',
      tone: 'ok',
      note: 'running 1.5.0',
    })
  })

  it('says good without a version when no requested row exists', () => {
    const outcome = deployOutcome(deploy({ artifact_version: null }), '1.5.0')
    expect(outcome).toMatchObject({ kind: 'verdict', word: 'good', note: 'running the new version' })
  })

  it('drops the verdict when the announce contradicts the confirm', () => {
    expect(deployOutcome(deploy(), '1.4.2')).toEqual({ kind: 'drift', reported: '1.4.2' })
    expect(deployOutcome(deploy(), null)).toEqual({ kind: 'drift', reported: null })
  })

  it('names the version that did not confirm, and where the board is back, if it agrees', () => {
    const rolled = deploy({ state: 'rolled_back' })
    expect(deployOutcome(rolled, '1.4.2')).toEqual({
      kind: 'verdict',
      word: 'rolled back',
      tone: 'bad',
      note: '1.5.0 did not confirm; back on 1.4.2',
    })
  })

  it('claims no "back on" unless the announce agrees or from is known', () => {
    const rolled = deploy({ state: 'rolled_back' })
    expect(deployOutcome(rolled, '9.9.9')).toMatchObject({ note: '1.5.0 did not confirm' })
    expect(deployOutcome(deploy({ state: 'rolled_back', from_version: null }), null)).toMatchObject({
      note: '1.5.0 did not confirm',
    })
    expect(
      deployOutcome(deploy({ state: 'rolled_back', artifact_version: null }), '1.4.2'),
    ).toMatchObject({ note: 'the new image did not confirm; back on 1.4.2' })
  })

  it('is gated by the server is_terminal flag, not the state name', () => {
    expect(deployOutcome(deploy({ is_terminal: false }), '1.5.0')).toBeNull()
    expect(deployOutcome(deploy({ state: 'rolled_back', is_terminal: false }), '1.4.2')).toBeNull()
  })

  it('gives no verdict to a terminal failed or unknown state', () => {
    expect(deployOutcome(deploy({ state: 'failed' }), '1.4.2')).toBeNull()
    expect(deployOutcome(deploy({ state: 'quarantined' }), '1.4.2')).toBeNull()
  })
})

// R5-fe-1: which way an `awaiting_safe_window` waits, the command rollback head, and the
// `cancelled` label.
const at = '2026-09-10T11:59:00Z'
const toStaged = ['requested', 'staging', 'downloading', 'verifying', 'staged'].map((state) => ({ state, at }))
const afterReboot = [...toStaged, { state: 'applying', at }, { state: 'rebooting', at }, { state: 'confirming', at }]

describe('safeWindowWait (R5-fe-1)', () => {
  const waiting = (overrides: Partial<DeploySummary>) =>
    deploy({ state: 'awaiting_safe_window', is_terminal: false, ...overrides })

  it.each([
    ['the rollback detail', { detail: 'rollback waits for a safe window' }, 'rollback'],
    ['the apply detail', { detail: 'apply waits for a safe window' }, 'apply'],
    ['the apply detail even after a reboot step', { detail: 'apply waits for a safe window', steps: afterReboot }, 'apply'],
    ['no detail and no reboot step (auto hold)', { detail: null, steps: toStaged }, 'apply'],
    ['no detail and no steps (older API)', { detail: null }, 'apply'],
    ['no detail after a confirming step', { detail: null, steps: afterReboot }, 'rollback'],
    ['a `constructor` detail falls back to the steps', { detail: 'constructor', steps: afterReboot }, 'rollback'],
    ['a `constructor` detail before the reboot', { detail: 'constructor', steps: toStaged }, 'apply'],
  ] as const)('%s', (_name, overrides, expected) => {
    expect(safeWindowWait(waiting(overrides as Partial<DeploySummary>))).toBe(expected)
  })

  it('is null for every other state, whatever the detail says', () => {
    expect(safeWindowWait(deploy({ state: 'staged', detail: 'rollback waits for a safe window' }))).toBeNull()
    expect(safeWindowWait(deploy({ state: 'confirmed' }))).toBeNull()
  })
})

describe('deployStateLabel (R5-fe-1)', () => {
  it('names a rollback wait as a rollback, decided and unbounded', () => {
    const label = deployStateLabel(
      deploy({ state: 'awaiting_safe_window', is_terminal: false, detail: 'rollback waits for a safe window' }),
    )
    expect(label).toBe(ROLLBACK_WAIT_LABEL)
    expect(label).toContain('rolling back')
    expect(label).toContain('may wait indefinitely')
  })

  it('keeps the existing words for an apply wait', () => {
    expect(
      deployStateLabel(deploy({ state: 'awaiting_safe_window', detail: 'apply waits for a safe window' })),
    ).toBe(DEPLOY_STATE_LABELS.awaiting_safe_window)
  })

  it('falls back to the raw state for an unknown one', () => {
    expect(deployStateLabel(deploy({ state: 'teleporting' }))).toBe('teleporting')
  })

  it('labels cancelled as the operator action it is, not a failure', () => {
    expect(deployStateLabel(deploy({ state: 'cancelled' }))).toBe(
      'cancelled — the board keeps its current image',
    )
    expect(DEPLOY_BAD_STATES.has('cancelled')).toBe(false)
  })
})

describe('isCommandRollback (R5-fe-1)', () => {
  it.each([
    ['rolled back by command; returned to ota_0', true],
    ['rolled back by command', true],
    [' rolled back by command ; returned to ota_1', true],
    ['selftest failed: x; returned to ota_0', false],
    ['rolled back by commander; returned to ota_0', false],
    ['constructor', false],
  ])('%s -> %s', (detail, expected) => {
    expect(isCommandRollback(deploy({ state: 'rolled_back', detail }))).toBe(expected)
  })

  it('needs the rolled_back state and a detail', () => {
    expect(isCommandRollback(deploy({ state: 'failed', detail: 'rolled back by command' }))).toBe(false)
    expect(isCommandRollback(deploy({ state: 'rolled_back', detail: null }))).toBe(false)
  })

  it('makes the verdict note name the operator, never "did not confirm"', () => {
    const outcome = deployOutcome(
      deploy({
        state: 'rolled_back',
        detail: 'rolled back by command; returned to ota_0',
        artifact_version: null,
        from_version: null,
      }),
      '1.4.2',
    )
    expect(outcome).toEqual({ kind: 'verdict', word: 'rolled back', tone: 'bad', note: 'by an operator command' })
  })

  it('handles non-app companion cargo without false drift against fwVersion (P0-2)', () => {
    const cargoDeploy = deploy({
      artifact_type: 'model_weights',
      artifact_version: 'yolo-v8',
    })
    expect(deployOutcome(cargoDeploy, '1.4.2', { model_weights: 'yolo-v8' })).toEqual({
      kind: 'verdict',
      word: 'good',
      tone: 'ok',
      note: 'running yolo-v8',
    })
    expect(deployOutcome(cargoDeploy, '1.4.2', null)).toEqual({
      kind: 'verdict',
      word: 'good',
      tone: 'ok',
      note: 'running yolo-v8',
    })
    expect(deployOutcome(cargoDeploy, '1.4.2', { model_weights: 'yolo-v7' })).toEqual({
      kind: 'drift',
      reported: 'yolo-v7',
    })
  })
})

function mockDevice(over: Partial<DeviceSummary> = {}): DeviceSummary {
  return {
    device_id: 'a4cf12b3de90',
    name: 'node-01',
    group_id: null,
    platform_type: 'esp32s3',
    fw_version: '1.5.0',
    agent_version: '0.7.2',
    link_type: 'wifi',
    ssid: 'lab',
    known_networks: 1,
    power_class: 'always_on',
    expected_wake_interval_s: null,
    parent_device_id: null,
    partition_layout: 'ab-4m-v1',
    ota_slot_size: 1966080,
    capabilities: ['apply', 'cancel', 'rollback', 'reboot'],
    last_seen: '2026-10-25T12:00:00Z',
    enrolled_at: '2026-10-20T12:00:00Z',
    broker_provisioned_at: '2026-10-20T12:00:00Z',
    online: true,
    deploy: null,
    ...over,
  }
}

describe('devicePriorityScore & describeDevicePresence (P1-1, P2-2)', () => {
  const nowMs = Date.parse('2026-10-25T12:10:00Z')

  it('scores safe_mode as -1, rolled_back/failed/overdue as 0, in-flight as 1, drift/weak RSSI as 2, healthy as 3, offline as 4', () => {
    expect(devicePriorityScore(mockDevice({ safe_mode: true }), '1.5.0', nowMs)).toBe(-1)
    expect(
      devicePriorityScore(
        mockDevice({ deploy: deploy({ state: 'rolled_back', is_terminal: true }) }),
        '1.5.0',
        nowMs,
      ),
    ).toBe(0)
    expect(
      devicePriorityScore(
        mockDevice({
          online: false,
          presence_reason: 'sortie',
          expected_back_s: 300,
          last_seen: '2026-10-25T12:00:00Z',
        }),
        '1.5.0',
        nowMs,
      ),
    ).toBe(0)
    expect(
      devicePriorityScore(
        mockDevice({ deploy: deploy({ state: 'downloading', is_terminal: false }) }),
        '1.5.0',
        nowMs,
      ),
    ).toBe(1)
    expect(devicePriorityScore(mockDevice({ fw_version: '1.4.2' }), '1.5.0', nowMs)).toBe(2)
    expect(
      devicePriorityScore(
        mockDevice({
          health: {
            at: '2026-10-25T12:00:00Z',
            uptime_s: 100,
            rssi: -82,
            free_heap: 140000,
            min_free_heap: 110000,
            boot_ok: true,
          },
        }),
        '1.5.0',
        nowMs,
      ),
    ).toBe(2)
    expect(devicePriorityScore(mockDevice(), '1.5.0', nowMs)).toBe(3)
    expect(devicePriorityScore(mockDevice({ online: false, power_class: 'sleepy' }), '1.5.0', nowMs)).toBe(3)
    expect(devicePriorityScore(mockDevice({ online: false }), '1.5.0', nowMs)).toBe(4)
  })

  it('describes all 6+ presence states with distinct glyphs', () => {
    expect(describeDevicePresence(mockDevice(), nowMs)).toMatchObject({
      code: 'wifi',
      glyph: '█',
    })
    expect(
      describeDevicePresence(mockDevice({ link_type: 'dock', parent_device_id: 'ugv-01' }), nowMs),
    ).toMatchObject({
      code: 'dock',
      glyph: '█',
      label: 'DOCK (@ugv-01)',
    })
    expect(
      describeDevicePresence(mockDevice({ online: false, power_class: 'sleepy' }), nowMs),
    ).toMatchObject({
      code: 'asleep',
      glyph: '◐',
    })
    expect(
      describeDevicePresence(
        mockDevice({ online: false, presence_reason: 'sortie', expected_back_s: 900 }),
        nowMs,
      ),
    ).toMatchObject({
      code: 'sortie',
      glyph: '◎',
    })
    expect(describeDevicePresence(mockDevice({ offline_mode: true }), nowMs)).toMatchObject({
      code: 'emcon',
      glyph: '⊘',
      className: 'emcon-badge',
    })
    expect(
      describeDevicePresence(
        mockDevice({
          online: false,
          presence_reason: 'sortie',
          expected_back_s: 300,
          last_seen: '2026-10-25T12:00:00Z',
        }),
        nowMs,
      ),
    ).toMatchObject({
      code: 'overdue',
      glyph: '!!',
    })
    expect(describeDevicePresence(mockDevice({ online: false }), nowMs)).toMatchObject({
      code: 'offline',
      glyph: '░',
    })
  })
})
