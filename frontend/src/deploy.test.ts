// `deployOutcome` is the rule that decides whether a finished deploy may say `good`.
// Every row of the R2-fe-1 table is pinned here, without rendering.

import { describe, expect, it } from 'vitest'
import { type DeploySummary } from './api'
import {
  DEPLOY_BAD_STATES,
  DEPLOY_STATE_LABELS,
  ROLLBACK_WAIT_LABEL,
  deployOutcome,
  deployStateLabel,
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
})
