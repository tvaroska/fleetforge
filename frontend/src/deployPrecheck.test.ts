// What these tests defend: a refusal never becomes sendable; `override` only ever names a
// code the server raised as gating AND the operator ticked; the lines around the server's
// sentences never print `null` or `undefined`.

import { describe, expect, it } from 'vitest'
import { type DeployPrecheck } from './api'
import {
  canSend,
  fitLine,
  gatingCodes,
  isGating,
  overrideFor,
  rollbackLine,
  sendLabel,
  summaryLine,
} from './deployPrecheck'

function precheck(overrides: Partial<DeployPrecheck> = {}): DeployPrecheck {
  return {
    device_id: 'a4cf12b3de90',
    target: 'esp32c6',
    version: '1.5.0',
    from_version: '1.4.2',
    sha256: 'a'.repeat(64),
    size_bytes: 230_000,
    artifact_partition_layout: 'ab-4m-v1',
    device_partition_layout: 'ab-4m-v1',
    ota_slot_size: 1_966_080,
    power_class: 'always_on',
    expected_wake_interval_s: null,
    device_online: true,
    confirm_timeout_s: 300,
    deployable: true,
    refusals: [],
    warnings: [],
    ...overrides,
  }
}

const gating = { code: 'rollback_incapable', message: 'no rollback', needs_override: true }
const plain = { code: 'offline', message: 'this board is offline' }

describe('canSend', () => {
  it('is true with no findings', () => {
    expect(canSend(precheck(), new Set())).toBe(true)
  })

  it('is false with a refusal, ticked or not', () => {
    const refused = precheck({
      deployable: false,
      refusals: [{ code: 'layout_mismatch', message: 'x' }],
    })
    expect(canSend(refused, new Set())).toBe(false)
    expect(canSend(refused, new Set(['layout_mismatch', 'rollback_incapable']))).toBe(false)
  })

  it('is false when a refusal is listed even if the server said deployable', () => {
    expect(canSend(precheck({ refusals: [{ code: 'x', message: 'x' }] }), new Set())).toBe(false)
  })

  it('needs every gating warning ticked', () => {
    const p = precheck({ warnings: [gating] })
    expect(canSend(p, new Set())).toBe(false)
    expect(canSend(p, new Set(['rollback_incapable']))).toBe(true)
  })

  it('does not need a tick for non-gating warnings', () => {
    expect(canSend(precheck({ warnings: [plain] }), new Set())).toBe(true)
  })
})

describe('gating', () => {
  it('treats an absent needs_override as not gating', () => {
    expect(isGating(plain)).toBe(false)
    expect(isGating({ ...plain, needs_override: false })).toBe(false)
    expect(isGating(gating)).toBe(true)
    expect(gatingCodes(precheck({ warnings: [plain] }))).toEqual([])
  })

  it('overrideFor never returns an unraised or non-gating code', () => {
    const p = precheck({ warnings: [plain, gating] })
    expect(overrideFor(p, new Set(['offline', 'made_up', 'rollback_incapable']))).toEqual([
      'rollback_incapable',
    ])
    expect(overrideFor(p, new Set())).toEqual([])
    expect(overrideFor(precheck(), new Set(['rollback_incapable']))).toEqual([])
  })
})

describe('sendLabel', () => {
  it('says Send with no warning and Send anyway with any', () => {
    expect(sendLabel(precheck())).toBe('Send')
    expect(sendLabel(precheck({ warnings: [plain] }))).toBe('Send anyway')
    expect(sendLabel(precheck({ warnings: [gating] }))).toBe('Send anyway')
  })
})

describe('rollbackLine', () => {
  it('is null when the deploy is not deployable', () => {
    expect(rollbackLine(precheck({ deployable: false }))).toBeNull()
  })

  it('names the window and where the board returns to', () => {
    expect(rollbackLine(precheck())).toBe(
      'If 1.5.0 never reconnects within 300 s of its reboot, the board rolls back on its own to 1.4.2.',
    )
  })

  it('says the version it runs now when it never reported one', () => {
    expect(rollbackLine(precheck({ from_version: null }))).toBe(
      'If 1.5.0 never reconnects within 300 s of its reboot, the board rolls back on its own to the version it runs now.',
    )
  })
})

describe('summaryLine and fitLine', () => {
  it('reads current to target with the board state', () => {
    expect(summaryLine(precheck())).toBe('1.4.2 → 1.5.0 — esp32c6 · online')
    expect(summaryLine(precheck({ device_online: false, power_class: 'sleepy' }))).toBe(
      '1.4.2 → 1.5.0 — esp32c6 · offline · sleepy',
    )
  })

  it('shows layout and size against the slot', () => {
    expect(fitLine(precheck())).toBe(
      'layout: board ab-4m-v1, build ab-4m-v1 · 230,000 of 1,966,080 bytes',
    )
  })

  it('drops "of" when the slot is unknown and the size when the size is', () => {
    expect(fitLine(precheck({ ota_slot_size: null }))).toBe(
      'layout: board ab-4m-v1, build ab-4m-v1 · 230,000 bytes',
    )
    expect(fitLine(precheck({ size_bytes: null }))).toBe('layout: board ab-4m-v1, build ab-4m-v1')
  })

  it('has no fit line without a build', () => {
    expect(fitLine(precheck({ sha256: null }))).toBeNull()
  })

  it('never prints null or undefined with every nullable null', () => {
    const empty = precheck({
      from_version: null,
      sha256: null,
      size_bytes: null,
      artifact_partition_layout: null,
      device_partition_layout: null,
      ota_slot_size: null,
      expected_wake_interval_s: null,
    })
    const text = [summaryLine(empty), fitLine(empty) ?? '', rollbackLine(empty) ?? ''].join('\n')
    expect(text).not.toMatch(/null|undefined/)
    expect(summaryLine(empty)).toContain('— → 1.5.0')

    const layoutless = precheck({ artifact_partition_layout: null, device_partition_layout: null })
    expect(fitLine(layoutless)).toContain('not reported')
    expect(fitLine(layoutless)).not.toMatch(/null|undefined/)
  })
})
