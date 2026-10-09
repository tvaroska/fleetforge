// A partition profile's name, checked before the round trip (R3-fe-1). The SERVER is
// authoritative: this mirrors `PROFILE_LAYOUT_ID_PATTERN` and the reserved-name check
// (`_not_reserved`) in api/schemas.py, and a name this lets through and the server refuses
// comes back as the server's own sentence (the `boardName.ts` posture).
//
// Duplicates are NOT checked here: only the server sees the other profiles, and its 409
// names the problem. A name is final once adopted (R3-be-2 D4), so a typo cannot be
// renamed; the form says so and this check is the operator's one chance to read the rule.
//
// Pure: no React, no fetch.

export const PROFILE_NAME_MAX = 32

/** What a board announces when its flash map has no name; the server refuses it as a name. */
export const RESERVED_PROFILE_NAME = 'unknown'

export type ProfileNameCheck = { ok: true; name: string } | { ok: false; reason: string }

export function checkProfileName(raw: string): ProfileNameCheck {
  const s = raw.trim()
  if (s === '') return { ok: false, reason: 'give this flash map a name' }
  if (s.length > PROFILE_NAME_MAX) {
    return { ok: false, reason: `a profile name is at most ${PROFILE_NAME_MAX} characters` }
  }
  if (!/^[a-z0-9][a-z0-9-]*$/.test(s)) {
    return {
      ok: false,
      reason:
        'a profile name is lowercase letters, digits and hyphens, and starts with a letter or ' +
        'digit: it travels in upload URLs and artifact labels',
    }
  }
  if (s === RESERVED_PROFILE_NAME) {
    return {
      ok: false,
      reason: 'unknown is reserved: it is what a board announces when its map has no name',
    }
  }
  return { ok: true, name: s }
}
