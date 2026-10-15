import { describe, expect, it } from 'vitest'
import { type DeviceLogLine } from './api'
import { describeLevel, formatDropped, tailRows } from './deviceLog'

function line(over: Partial<DeviceLogLine> = {}): DeviceLogLine {
  return {
    id: 1,
    logged_at: '2026-10-15T10:00:00Z',
    received_at: '2026-10-15T10:00:00Z',
    level: 'I',
    text: 'hello',
    dropped: 0,
    ...over,
  }
}

describe('describeLevel', () => {
  it('maps the ESP-IDF letters to a label and a tone', () => {
    expect(describeLevel('E')).toEqual({ level: 'E', label: 'error', tone: 'bad' })
    expect(describeLevel('W')).toEqual({ level: 'W', label: 'warning', tone: 'warn' })
    expect(describeLevel('I')).toEqual({ level: 'I', label: 'info', tone: null })
    expect(describeLevel('D')).toEqual({ level: 'D', label: 'debug', tone: 'muted' })
    expect(describeLevel('V')).toEqual({ level: 'V', label: 'verbose', tone: 'muted' })
  })

  it('renders an unknown level as itself', () => {
    expect(describeLevel('X9')).toEqual({ level: 'X9', label: null, tone: null })
  })
})

describe('tailRows', () => {
  it('puts a gap below the line that carries dropped', () => {
    const rows = tailRows([
      line({ id: 3, text: 'L3', dropped: 3 }),
      line({ id: 2, text: 'L2' }),
      line({ id: 1, text: 'L1' }),
    ])
    expect(rows.map((r) => (r.kind === 'line' ? `line:${r.line.text}` : `gap:${r.dropped}`))).toEqual([
      'line:L3',
      'gap:3',
      'line:L2',
      'line:L1',
    ])
  })

  it('renders a marker as a gap only', () => {
    const rows = tailRows([line({ id: 9, text: null, dropped: 7 }), line({ id: 8 })])
    expect(rows.map((r) => r.kind)).toEqual(['gap', 'line'])
    expect(rows[0]).toMatchObject({ kind: 'gap', dropped: 7, key: 'gap-9' })
  })

  it('skips a marker with nothing dropped', () => {
    expect(tailRows([line({ text: null, dropped: 0 })])).toEqual([])
  })

  it('keeps the input order even when logged_at is not monotonic', () => {
    const rows = tailRows([
      line({ id: 1, text: 'a', logged_at: '2026-10-15T09:00:00Z' }),
      line({ id: 2, text: 'b', logged_at: '2026-10-15T11:00:00Z' }),
    ])
    expect(rows.map((r) => (r.kind === 'line' ? r.line.text : '?'))).toEqual(['a', 'b'])
  })

  it('is empty for no lines', () => {
    expect(tailRows([])).toEqual([])
  })
})

describe('formatDropped', () => {
  it('pluralises', () => {
    expect(formatDropped(1)).toBe('1 line missing here')
    expect(formatDropped(3)).toBe('3 lines missing here')
  })
})
