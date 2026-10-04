// The judgement behind the update result card (R2b-fe-10). What a regression would cost:
//
// 1. `good` claimed for a board that has since announced something else (R2-fe-1's trap).
// 2. A card for a transaction the server has not finished.
// 3. A rollback or failure with no reason, or with the wrong next action ("send it again"
//    after a rollback is the one thing the spec forbids).
// 4. `failed before reboot` claimed when the steps do not prove no reboot happened.
// 5. A device-controlled `detail` reaching a prototype key (`constructor`).
// 6. `null`, `undefined`, `NaN`, `%` or an arrow in any string.
// 7. (R2b-fe-11) The Send again version offered after a rollback, an unproven reboot or a
//    build defect.

import { describe, expect, it } from 'vitest'
import { type DeploySummary } from './api'
import { FAILURE_REASONS, deployResult, type DeployResult } from './deployResult'

const NOW = new Date('2026-09-10T12:00:00Z').getTime()
const at = (secondsAgo: number) => new Date(NOW - secondsAgo * 1000).toISOString()
const step = (state: string, secondsAgo: number) => ({ state, at: at(secondsAgo) })

const versions = {
  ui: '0.4.2 · abcdef12',
  uiTitle: 'built x',
  api: '0.4.2 · abcdef12',
  differ: false,
}

function deploy(overrides: Partial<DeploySummary> = {}): DeploySummary {
  return {
    cmd_id: 'cmd-a',
    state: 'confirmed',
    at: at(10),
    is_terminal: true,
    artifact_version: '1.5.0',
    from_version: '1.4.2',
    pct: null,
    detail: null,
    ...overrides,
  }
}

function row(result: DeployResult, label: string): string | undefined {
  return result.rows.find((r) => r.label === label)?.value
}

function strings(result: DeployResult): string[] {
  return [
    result.word,
    result.note,
    result.drift,
    result.reason,
    result.next,
    result.ago,
    ...result.rows.map((r) => r.value),
  ].filter((s): s is string => s !== null)
}

function must(result: DeployResult | null): DeployResult {
  expect(result).not.toBeNull()
  return result as DeployResult
}

describe('deployResult — when there is a card', () => {
  it('is null with no deploy', () => {
    expect(deployResult(null, { fw_version: '1.4.2' }, versions, NOW)).toBeNull()
  })

  it.each(['downloading', 'confirming', 'requested'])('is null while %s is in flight', (state) => {
    expect(
      deployResult(deploy({ state, is_terminal: false }), { fw_version: '1.4.2' }, versions, NOW),
    ).toBeNull()
  })

  it('trusts the server: a failed row that is not terminal gets no card', () => {
    expect(
      deployResult(deploy({ state: 'failed', is_terminal: false }), { fw_version: '1.4.2' }, versions, NOW),
    ).toBeNull()
  })
})

describe('deployResult — good', () => {
  const result = must(
    deployResult(
      deploy({ steps: [step('requested', 40), step('confirmed', 10)] }),
      { fw_version: '1.5.0' },
      versions,
      NOW,
    ),
  )

  it('says good, with the verdict note and the reason', () => {
    expect(result.outcome).toBe('good')
    expect(result.word).toBe('good')
    expect(result.tone).toBe('ok')
    expect(result.note).toBe('running 1.5.0')
    expect(result.reason).toBe(
      'The board rebooted into 1.5.0, reconnected to the server and confirmed it, so it keeps this image.',
    )
    expect(result.next).toBeNull()
    expect(result.drift).toBeNull()
  })

  it('lists before, after, sent, finished and the versions, in that order', () => {
    expect(result.rows.map((r) => r.label)).toEqual(['Before', 'After', 'Sent', 'Finished', 'UI / API'])
    expect(row(result, 'Before')).toBe('1.4.2')
    expect(row(result, 'After')).toBe('1.5.0')
    expect(row(result, 'Finished')).toContain('(10 s ago)')
    expect(row(result, 'UI / API')).toBe('UI 0.4.2 · abcdef12 · API 0.4.2 · abcdef12')
    expect(result.ago).toBe('10 s ago')
  })
})

describe('deployResult — drift', () => {
  it('withholds good, and never says running', () => {
    const result = must(deployResult(deploy(), { fw_version: '1.4.2' }, versions, NOW))
    expect(result.outcome).toBe('drift')
    expect(result.word).toBe('confirmed by the board')
    expect(result.tone).toBeNull()
    expect(result.note).toBeNull()
    expect(result.drift).toBe('the board has since reported 1.4.2, so this is not what it runs now')
    expect(row(result, 'After')).toBe('1.5.0 when it was confirmed')
    expect(result.word).not.toContain('good')
    for (const s of strings(result)) {
      expect(s).not.toContain('running')
    }
  })

  it('says so when the board has not reported at all', () => {
    const result = must(deployResult(deploy(), { fw_version: null }, versions, NOW))
    expect(result.drift).toBe('the board has not reported a version since')
  })
})

describe('deployResult — rolled back', () => {
  const rolled = deploy({
    state: 'rolled_back',
    detail: 'returned to 1.4.2; the new image did not confirm',
    confirm_timeout_s: 300,
  })

  it('names the reason, the window and one next action that is not "send again"', () => {
    const result = must(deployResult(rolled, { fw_version: '1.4.2' }, versions, NOW))
    expect(result.outcome).toBe('rolled-back')
    expect(result.word).toBe('rolled back')
    expect(result.tone).toBe('bad')
    expect(result.note).toBe('1.5.0 did not confirm; back on 1.4.2')
    expect(result.reason).toContain('went back to 1.4.2 on its own')
    expect(result.reason).toContain('within 300 s of its reboot')
    expect(result.reason).toContain('the board does not say which')
    expect(result.next).toMatch(/^Do not send 1\.5\.0 again as it is: /)
    expect(row(result, 'After')).toBe('1.4.2 again')
    expect(row(result, 'Board says')).toBe('returned to 1.4.2; the new image did not confirm')
  })

  it('says "in time" without the board’s window', () => {
    const { confirm_timeout_s: _drop, ...bare } = rolled
    void _drop
    const result = must(deployResult(bare, { fw_version: '1.4.2' }, versions, NOW))
    expect(result.reason).toContain('does not reconnect in time')
    expect(result.reason).not.toContain('within')
  })

  it('does not claim a version the board has not announced', () => {
    const result = must(deployResult(rolled, { fw_version: '2.0.0' }, versions, NOW))
    expect(result.reason).toContain('went back to the previous image on its own')
    expect(row(result, 'After')).toBe('the previous image')
    for (const s of strings(result).filter((s) => s !== row(result, 'Board says'))) {
      expect(s).not.toContain('back on')
    }
  })
})

describe('deployResult — failed', () => {
  const beforeReboot = [
    step('requested', 120),
    step('staging', 118),
    step('downloading', 115),
    step('failed', 35),
  ]

  it('before the reboot, when the steps prove it, with the cause and next from the table', () => {
    const result = must(
      deployResult(
        deploy({ state: 'failed', detail: 'download stalled', steps: beforeReboot }),
        { fw_version: '1.4.2' },
        versions,
        NOW,
      ),
    )
    expect(result.outcome).toBe('failed')
    expect(result.word).toBe('failed before reboot')
    expect(result.tone).toBe('bad')
    expect(result.reason).toBe(FAILURE_REASONS['download stalled'].cause)
    expect(result.next).toBe(FAILURE_REASONS['download stalled'].next)
    expect(row(result, 'After')).toBe('1.4.2, unchanged: 1.5.0 was not installed')
    expect(row(result, 'Board says')).toBe('download stalled')
  })

  it('does not say the old version is unchanged when the announce says otherwise', () => {
    const result = must(
      deployResult(
        deploy({ state: 'failed', detail: 'download stalled', steps: beforeReboot }),
        { fw_version: null },
        versions,
        NOW,
      ),
    )
    expect(row(result, 'After')).toBe('1.5.0 was not installed')
  })

  it('finds the simulator’s "download failed: <Exception>" by its prefix', () => {
    const result = must(
      deployResult(
        deploy({ state: 'failed', detail: 'download failed: ClientConnectorError', steps: beforeReboot }),
        { fw_version: '1.4.2' },
        versions,
        NOW,
      ),
    )
    expect(result.reason).toBe(FAILURE_REASONS['download failed'].cause)
    expect(row(result, 'Board says')).toBe('download failed: ClientConnectorError')
  })

  it('falls back for words it does not know, and still shows them', () => {
    const result = must(
      deployResult(
        deploy({ state: 'failed', detail: 'out of memory', steps: beforeReboot }),
        { fw_version: '1.4.2' },
        versions,
        NOW,
      ),
    )
    expect(result.reason).toBe('The board stopped the update; its own words are below.')
    expect(row(result, 'Board says')).toBe('out of memory')
  })

  it.each(['constructor', '__proto__', 'toString', 'hasOwnProperty'])(
    'does not treat the prototype key %s as a known failure',
    (detail) => {
      const result = must(
        deployResult(deploy({ state: 'failed', detail, steps: beforeReboot }), { fw_version: '1.4.2' }, versions, NOW),
      )
      expect(result.reason).toBe('The board stopped the update; its own words are below.')
      expect(result.next).toMatch(/serial console/)
    },
  )

  it('says no reason was reported for a null detail, and has no Board says row', () => {
    const result = must(
      deployResult(deploy({ state: 'failed', detail: null, steps: beforeReboot }), { fw_version: '1.4.2' }, versions, NOW),
    )
    expect(result.reason).toBe('The update failed and no reason was reported.')
    expect(result.rows.map((r) => r.label)).not.toContain('Board says')
  })

  it('is plain "failed" without steps', () => {
    const result = must(
      deployResult(deploy({ state: 'failed', detail: 'sha256 mismatch' }), { fw_version: '1.4.2' }, versions, NOW),
    )
    expect(result.word).toBe('failed')
    expect(row(result, 'After')).toBe('the board reports 1.4.2')
    expect(result.reason).toBe(FAILURE_REASONS['sha256 mismatch'].cause)
  })

  it('is plain "failed" when a step reached the reboot', () => {
    const result = must(
      deployResult(
        deploy({ state: 'failed', steps: [step('requested', 90), step('rebooting', 50), step('failed', 40)] }),
        { fw_version: null },
        versions,
        NOW,
      ),
    )
    expect(result.word).toBe('failed')
    expect(row(result, 'After')).toBe('not reported')
  })
})

describe('deployResult — everything else', () => {
  it('gives an unknown terminal state a card labelled as itself', () => {
    const result = must(deployResult(deploy({ state: 'exploded' }), { fw_version: '1.4.2' }, versions, NOW))
    expect(result.outcome).toBe('other')
    expect(result.word).toBe('exploded')
    expect(result.tone).toBeNull()
    expect(result.reason).toBeNull()
    expect(result.next).toBeNull()
  })

  it('has a Sent row only for parseable steps', () => {
    const none = must(deployResult(deploy(), { fw_version: '1.5.0' }, versions, NOW))
    expect(row(none, 'Sent')).toBeUndefined()
    const bad = must(
      deployResult(deploy({ steps: [{ state: 'requested', at: 'garbage' }] }), { fw_version: '1.5.0' }, versions, NOW),
    )
    expect(row(bad, 'Sent')).toBeUndefined()
    const good = must(deployResult(deploy({ steps: [step('requested', 40)] }), { fw_version: '1.5.0' }, versions, NOW))
    expect(row(good, 'Sent')).toBeDefined()
  })

  it('puts a Sent by row between Sent and Finished, only when recorded', () => {
    const fw = { fw_version: '1.5.0' }
    const sent = must(
      deployResult(
        deploy({
          steps: [step('requested', 40)],
          sent_by: { subject: 'admin', credential: 'dashboard session (203.0.113.7)' },
        }),
        fw,
        versions,
        NOW,
      ),
    )
    expect(sent.rows.map((r) => r.label)).toEqual(['Before', 'After', 'Sent', 'Sent by', 'Finished', 'UI / API'])
    expect(row(sent, 'Sent by')).toBe('admin · dashboard session (203.0.113.7)')
    const noCred = must(
      deployResult(deploy({ sent_by: { subject: 'admin', credential: null } }), fw, versions, NOW),
    )
    expect(row(noCred, 'Sent by')).toBe('admin')
    expect(row(must(deployResult(deploy({ sent_by: null }), fw, versions, NOW)), 'Sent by')).toBeUndefined()
    expect(row(must(deployResult(deploy(), fw, versions, NOW)), 'Sent by')).toBeUndefined()
  })

  it('has a UI / API row only with versions, and carries the mismatch flag', () => {
    const none = must(deployResult(deploy(), { fw_version: '1.5.0' }, null, NOW))
    expect(row(none, 'UI / API')).toBeUndefined()
    expect(none.versionsDiffer).toBe(false)
    const differ = must(deployResult(deploy(), { fw_version: '1.5.0' }, { ...versions, differ: true }, NOW))
    expect(differ.versionsDiffer).toBe(true)
  })

  it('records a missing before as "not recorded"', () => {
    const result = must(deployResult(deploy({ from_version: null }), { fw_version: '1.5.0' }, versions, NOW))
    expect(row(result, 'Before')).toBe('not recorded')
  })

  it.each([
    ['good', deploy(), '1.5.0'],
    ['drift', deploy({ artifact_version: null }), null],
    ['rolled back', deploy({ state: 'rolled_back', artifact_version: null, from_version: null }), null],
    ['failed', deploy({ state: 'failed', artifact_version: null, from_version: null, detail: null }), null],
    ['other', deploy({ state: 'exploded', artifact_version: null }), null],
  ])('never prints null, undefined, NaN, a percent or an arrow (%s)', (_name, d, fw) => {
    const result = must(deployResult({ ...d, pct: 100, steps: [step('requested', 5)] }, { fw_version: fw }, null, NOW))
    for (const s of strings(result)) {
      expect(s).not.toMatch(/null|undefined|NaN|%|→/)
    }
  })
})

describe('deployResult — send again (R2b-fe-11)', () => {
  const fw = { fw_version: '1.4.2' }
  const before = [step('requested', 120), step('downloading', 115), step('failed', 35)]
  const failed = (overrides: Partial<DeploySummary> = {}) =>
    deploy({ state: 'failed', detail: 'download stalled', steps: before, ...overrides })
  const again = (d: DeploySummary) => must(deployResult(d, fw, versions, NOW)).sendAgain

  it('is the failed version for a failure before reboot', () => {
    expect(again(failed())).toBe('1.5.0')
  })

  it('finds the entry by prefix, and falls back for unknown words and a null detail', () => {
    expect(again(failed({ detail: 'download failed: ConnectionError' }))).toBe('1.5.0')
    expect(again(failed({ detail: 'flux capacitor' }))).toBe('1.5.0')
    expect(again(failed({ detail: 'constructor' }))).toBe('1.5.0')
    expect(again(failed({ detail: null }))).toBe('1.5.0')
  })

  it('is null for a build defect, and the failure card is unchanged', () => {
    for (const detail of ['artifact larger than the ota slot', 'image validation failed']) {
      const result = must(deployResult(failed({ detail }), fw, versions, NOW))
      expect(result.sendAgain).toBeNull()
      expect(result.word).toBe('failed before reboot')
      expect(result.next).toBe(FAILURE_REASONS[detail].next)
    }
  })

  it('is null when the steps do not prove the reboot did not happen', () => {
    expect(again(failed({ steps: undefined }))).toBeNull()
    expect(again(failed({ steps: [] }))).toBeNull()
    expect(again(failed({ steps: [step('requested', 90), step('rebooting', 60), step('failed', 10)] }))).toBeNull()
  })

  it('is null for a rollback, a confirmed deploy, drift and an unknown state', () => {
    expect(again(deploy({ state: 'rolled_back', steps: before }))).toBeNull()
    expect(again(deploy({ state: 'rolled_back', steps: [step('requested', 90), step('rebooting', 60)] }))).toBeNull()
    expect(again(deploy({ state: 'confirmed', steps: before }))).toBeNull()
    expect(
      must(deployResult(deploy({ state: 'confirmed' }), { fw_version: '9.9.9' }, versions, NOW)).sendAgain,
    ).toBeNull()
    expect(again(deploy({ state: 'exploded', steps: before }))).toBeNull()
  })

  it('is null without a known version', () => {
    expect(again(failed({ artifact_version: null }))).toBeNull()
  })

  it('is offered exactly when the next action says to send it again', () => {
    for (const [key, entry] of Object.entries(FAILURE_REASONS)) {
      expect(entry.resend, key).toBe(/send it again/i.test(entry.next))
    }
    for (const detail of [null, 'flux capacitor']) {
      const result = must(deployResult(failed({ detail }), fw, versions, NOW))
      expect(result.sendAgain !== null).toBe(/send it again/i.test(result.next ?? ''))
    }
  })
})
