// `deployOutcome` is the rule that decides whether a finished deploy may say `good`.
// Every row of the R2-fe-1 table is pinned here, without rendering.

import { describe, expect, it } from 'vitest'
import { type DeploySummary } from './api'
import { deployOutcome } from './deploy'

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
