// What these tests defend (R4-fe-1):
//
// 1. The reset-reason vocabulary is the spec's 16 names, as a lookup with a fallback: an
//    unknown name is rendered as itself and never badged (we do not claim a fault we
//    cannot name).
// 2. Only spontaneous / hardware-fault resets earn the warning badge.
// 3. Fault counts, boot state and vitals are formatted from the server's answers; there
//    is no threshold anywhere.

import { describe, expect, it } from 'vitest'
import {
  FAULT_RESET_REASONS,
  RESET_REASON_LABELS,
  describeBoot,
  describeFaults,
  describeReset,
  describeVitals,
  formatMetricValue,
  formatWindow,
} from './deviceHealth'

// The 16 names of `spec/device-protocol.md` -> up/announce, *reset_reason* paragraph. Not
// read from the spec: the frontend image's build context has no `spec/`.
const SPEC_NAMES = [
  'poweron', 'ext', 'sw', 'panic', 'int_wdt', 'task_wdt', 'wdt', 'deepsleep',
  'brownout', 'sdio', 'usb', 'jtag', 'efuse', 'pwr_glitch', 'cpu_lockup', 'unknown',
]

const health = (overrides: Record<string, unknown> = {}) => ({
  at: '2026-09-10T11:59:55Z',
  uptime_s: 7380,
  rssi: -61,
  free_heap: 142000,
  min_free_heap: 98000,
  boot_ok: true,
  ...overrides,
})

describe('the reset vocabulary', () => {
  it('has exactly the spec names', () => {
    expect(Object.keys(RESET_REASON_LABELS).sort()).toEqual([...SPEC_NAMES].sort())
  })

  it('badges faults only', () => {
    for (const name of FAULT_RESET_REASONS) expect(SPEC_NAMES).toContain(name)
    for (const name of ['panic', 'task_wdt', 'brownout']) expect(FAULT_RESET_REASONS.has(name)).toBe(true)
    for (const name of ['poweron', 'sw', 'ext', 'deepsleep', 'unknown']) {
      expect(FAULT_RESET_REASONS.has(name)).toBe(false)
    }
  })
})

describe('describeReset', () => {
  it('knows a fault, a calm reason and an unknown one', () => {
    expect(describeReset('panic')).toEqual({ reason: 'panic', label: 'crashed (panic)', fault: true })
    expect(describeReset('poweron')).toMatchObject({ reason: 'poweron', fault: false })
    expect(describeReset('quantum_flip')).toEqual({ reason: 'quantum_flip', label: null, fault: false })
  })

  it('treats a name that is an Object.prototype key as unknown', () => {
    expect(describeReset('constructor')).toEqual({ reason: 'constructor', label: null, fault: false })
  })

  it('says nothing for not reported', () => {
    expect(describeReset(null)).toBeNull()
    expect(describeReset(undefined)).toBeNull()
    expect(describeReset('')).toBeNull()
  })
})

describe('describeBoot', () => {
  it('maps the three states', () => {
    expect(describeBoot(health({ boot_ok: false }))).toBe('unconfirmed')
    expect(describeBoot(health({ boot_ok: true }))).toBe('ok')
    expect(describeBoot(health({ boot_ok: null }))).toBeNull()
    expect(describeBoot(null)).toBeNull()
    expect(describeBoot(undefined)).toBeNull()
  })
})

describe('describeFaults', () => {
  const faults = (o: Record<string, number> = {}) => ({ window_s: 86400, panic: 0, task_wdt: 0, brownout: 0, ...o })

  it('lists non-zero counts in a fixed order', () => {
    expect(describeFaults(faults({ panic: 2, brownout: 1 }))).toBe('panic ×2, brownout ×1 in 24 h')
    expect(describeFaults(faults({ brownout: 3, task_wdt: 1 }))).toBe('task_wdt ×1, brownout ×3 in 24 h')
  })

  it('says nothing when all zero or absent', () => {
    expect(describeFaults(faults())).toBeNull()
    expect(describeFaults(null)).toBeNull()
    expect(describeFaults(undefined)).toBeNull()
  })

  it('words an odd window as a duration', () => {
    expect(describeFaults(faults({ panic: 1, window_s: 5400 }))).toBe('panic ×1 in 1 h 30 min')
    expect(formatWindow(3600)).toBe('1 h')
  })
})

describe('describeVitals', () => {
  it('lists heap low-water, uptime and signal', () => {
    const v = describeVitals(health(), true)
    expect(v?.parts).toEqual(['heap low 95.7 KiB', 'up 2 h 3 min', '-61 dBm'])
    expect(v?.title).toContain('138.7 KiB')
    expect(v?.stale).toBe(false)
  })

  it('omits what was not reported, and everything when nothing was', () => {
    expect(describeVitals(health({ uptime_s: null, rssi: null }), true)?.parts).toEqual(['heap low 95.7 KiB'])
    expect(
      describeVitals(health({ uptime_s: null, rssi: null, min_free_heap: null, free_heap: null }), true),
    ).toBeNull()
    expect(describeVitals(null, true)).toBeNull()
  })

  it('marks an offline board stale and dates the report', () => {
    const v = describeVitals(health(), false, (iso) => `at ${iso}`)
    expect(v?.stale).toBe(true)
    expect(v?.title).toContain('reported at 2026-09-10T11:59:55Z')
  })
})

describe('formatMetricValue', () => {
  it('prints integers whole and others to 6 significant digits', () => {
    expect(formatMetricValue(13)).toBe('13')
    expect(formatMetricValue(0.1 + 0.2)).toBe('0.3')
    expect(formatMetricValue(1234567.891)).toBe('1234570')
    expect(formatMetricValue(-2.5)).toBe('-2.5')
    expect(formatMetricValue(Number.NaN)).toBe('NaN')
  })
})
