// A board's name, checked before the round trip (R2b-fe-6) so the operator reads the
// reason at once. The SERVER is authoritative: this mirrors `DeviceUpdate._normalize_name`
// in api/schemas.py, sentence for sentence, and the two can differ at the edges (JS
// `trim()` and Python `strip()` disagree on a few control characters). That is accepted;
// a name this lets through and the server refuses comes back as the server's own 422.
//
// Duplicates are NOT checked here: only the server sees the other boards, and its 409
// names the one that holds the name.
//
// Pure: no React, no fetch.

export const BOARD_NAME_MAX = 64

export type NameCheck = { ok: true; name: string | null } | { ok: false; reason: string }

export function checkBoardName(raw: string): NameCheck {
  const s = raw.trim()
  if (s === '') return { ok: true, name: null }
  // Code points, not UTF-16 units: an emoji is 2 units in JS and 1 character in Python.
  if ([...s].length > BOARD_NAME_MAX) {
    return { ok: false, reason: `a name is at most ${BOARD_NAME_MAX} characters` }
  }
  if (/[\p{Cc}\p{Cf}]/u.test(s)) {
    return { ok: false, reason: 'a name cannot contain control characters' }
  }
  if (/^[0-9a-f]{12}$/i.test(s)) {
    return { ok: false, reason: 'a name cannot look like a device id (12 hex characters)' }
  }
  return { ok: true, name: s }
}
