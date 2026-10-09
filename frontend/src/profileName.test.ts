// The client check mirrors `PROFILE_LAYOUT_ID_PATTERN` and the reserved-name sentence in
// api/schemas.py. The server stays authoritative; these pin the sentences an operator reads
// before any round trip.

import { describe, expect, it } from 'vitest'
import { PROFILE_NAME_MAX, checkProfileName } from './profileName'

const reason = (raw: string) => {
  const check = checkProfileName(raw)
  if (check.ok) throw new Error(`expected a refusal for ${JSON.stringify(raw)}`)
  return check.reason
}

describe('checkProfileName', () => {
  it('trims, and accepts a lowercase hyphenated name', () => {
    expect(checkProfileName(' be2-map ')).toEqual({ ok: true, name: 'be2-map' })
  })

  it('refuses an empty name', () => {
    expect(reason('')).toBe('give this flash map a name')
    expect(reason('   ')).toBe('give this flash map a name')
  })

  it('refuses more than 32 characters, and accepts exactly 32', () => {
    expect(reason('a'.repeat(PROFILE_NAME_MAX + 1))).toBe('a profile name is at most 32 characters')
    expect(checkProfileName('a'.repeat(PROFILE_NAME_MAX))).toEqual({
      ok: true,
      name: 'a'.repeat(PROFILE_NAME_MAX),
    })
  })

  it('refuses upper case, a leading hyphen and other characters', () => {
    const sentence =
      'a profile name is lowercase letters, digits and hyphens, and starts with a letter or ' +
      'digit: it travels in upload URLs and artifact labels'
    expect(reason('Be2-map')).toBe(sentence)
    expect(reason('-x')).toBe(sentence)
    expect(reason('a b')).toBe(sentence)
    expect(reason('a_b')).toBe(sentence)
  })

  it("refuses 'unknown' with the server's own sentence", () => {
    expect(reason('unknown')).toBe(
      'unknown is reserved: it is what a board announces when its map has no name',
    )
  })
})
