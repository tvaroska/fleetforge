// What these tests defend:
//
// 1. `formatAgo` shows single seconds. A wider "just now" bucket would swallow a whole
//    heartbeat interval (5 s in `spec/prd.md` and in the simulator), leaving the column
//    frozen — and a frozen column is indistinguishable from a page that has stopped
//    updating, which is the one thing the fleet view has to make obvious.
// 2. A missing or unparseable timestamp degrades to something readable, never to
//    "Invalid Date" or "NaN s ago".

import { describe, expect, it } from 'vitest'
import { formatAgo, formatWhen } from './format'

const NOW = Date.parse('2026-09-10T12:00:00Z')
const ago = (seconds: number) => new Date(NOW - seconds * 1000).toISOString()

describe('formatAgo', () => {
  it('ticks one second at a time across a heartbeat interval', () => {
    expect([1, 2, 3, 4, 5].map((s) => formatAgo(ago(s), NOW))).toEqual([
      '1 s ago',
      '2 s ago',
      '3 s ago',
      '4 s ago',
      '5 s ago',
    ])
  })

  it('coarsens as the age grows', () => {
    expect(formatAgo(ago(0), NOW)).toBe('just now')
    expect(formatAgo(ago(59), NOW)).toBe('59 s ago')
    expect(formatAgo(ago(60), NOW)).toBe('1 min ago')
    expect(formatAgo(ago(3599), NOW)).toBe('59 min ago')
    expect(formatAgo(ago(3600), NOW)).toBe('1 h ago')
    expect(formatAgo(ago(86400 * 3), NOW)).toBe('3 d ago')
  })

  it('says "never" for a board that has enrolled and not yet published', () => {
    expect(formatAgo(null, NOW)).toBe('never')
  })

  it('reads a clock skew into the future as "just now", never as a negative age', () => {
    expect(formatAgo(ago(-30), NOW)).toBe('just now')
  })

  it('passes an unparseable timestamp through rather than rendering NaN', () => {
    expect(formatAgo('not a date', NOW)).toBe('not a date')
    expect(formatWhen('not a date')).toBe('not a date')
  })
})
