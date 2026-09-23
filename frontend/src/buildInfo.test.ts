import { describe, expect, it } from 'vitest'

import { UNKNOWN, describeBuild } from './buildInfo'

describe('describeBuild', () => {
  it('shows version and short commit when provenance was baked in', () => {
    expect(describeBuild('0.4.0', '815596b2c0ffee1234567890abcdef0123456789')).toBe(
      '0.4.0 · 815596b2',
    )
  })

  it('falls back to the bare version rather than printing "unknown" as a commit', () => {
    expect(describeBuild('0.4.0', UNKNOWN)).toBe('0.4.0')
    expect(describeBuild('0.4.0', '')).toBe('0.4.0')
  })
})
