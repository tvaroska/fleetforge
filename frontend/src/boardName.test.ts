import { describe, expect, it } from 'vitest'
import { BOARD_NAME_MAX, checkBoardName } from './boardName'

describe('checkBoardName', () => {
  it('trims and returns the normalised name', () => {
    expect(checkBoardName('  coop door  ')).toEqual({ ok: true, name: 'coop door' })
    expect(checkBoardName('coop door 2')).toEqual({ ok: true, name: 'coop door 2' })
  })

  it('maps blank to null (clears the name)', () => {
    expect(checkBoardName('')).toEqual({ ok: true, name: null })
    expect(checkBoardName('   ')).toEqual({ ok: true, name: null })
  })

  it('allows 64 characters and refuses 65', () => {
    expect(BOARD_NAME_MAX).toBe(64)
    expect(checkBoardName('a'.repeat(64))).toEqual({ ok: true, name: 'a'.repeat(64) })
    expect(checkBoardName('a'.repeat(65))).toEqual({
      ok: false,
      reason: 'a name is at most 64 characters',
    })
  })

  it('counts code points, not UTF-16 units', () => {
    expect(checkBoardName('🐔'.repeat(64)).ok).toBe(true)
    expect(checkBoardName('🐔'.repeat(65)).ok).toBe(false)
  })

  it('refuses control and format characters', () => {
    for (const bad of ['a\nb', 'a\tb', 'a‮b']) {
      expect(checkBoardName(bad)).toEqual({
        ok: false,
        reason: 'a name cannot contain control characters',
      })
    }
  })

  it('refuses a name that looks like a device id, either case', () => {
    for (const bad of ['A4CF12B3DE91', 'a4cf12b3de91']) {
      expect(checkBoardName(bad)).toEqual({
        ok: false,
        reason: 'a name cannot look like a device id (12 hex characters)',
      })
    }
    expect(checkBoardName('a4cf12b3de9').ok).toBe(true)
  })
})
