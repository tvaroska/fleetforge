// R2b-fe-3: a failed flash write says what failed and what to try first (cable, port, a
// lower baud), and names the flash chip only when the failure repeats at the lowest baud.

import { describe, expect, it } from 'vitest'
import { classifyWriteError, describeWriteFailure, LOWEST_BAUD } from './flashFailure'

const ctx = (over: Partial<Parameters<typeof describeWriteFailure>[1]> = {}) => ({
  attempt: 1,
  baudRate: 921600,
  part: null,
  address: null,
  ...over,
})

describe('flash write failure: classifyWriteError', () => {
  it.each([
    [new Error('Failed to write compressed data to flash after seq 3 failed with status 1,5'), 'rejected'],
    [new Error('MD5 of file does not match data in flash!'), 'rejected'],
    [new Error('No serial data received.'), 'no-answer'],
    [new Error('Serial data stream stopped: Possible serial noise or corruption.'), 'no-answer'],
    [new DOMException('The device has been lost.', 'NetworkError'), 'lost'],
    [new Error('something else entirely'), 'other'],
    ['a bare string', 'other'],
  ])('%s -> %s', (error, kind) => {
    expect(classifyWriteError(error)).toBe(kind)
  })
})

describe('flash write failure: describeWriteFailure', () => {
  it('attempt 1 at 921600: cable and port first, then 115200, never the chip', () => {
    const failure = describeWriteFailure(new Error('No serial data received.'), ctx())
    expect(failure.kind).toBe('no-answer')
    expect(failure.cause).toMatch(/stopped answering/i)
    expect(failure.tryFirst).toMatch(/cable/i)
    expect(failure.tryFirst).toMatch(/port/i)
    expect(failure.tryFirst).toMatch(/115200/)
    expect(failure.chipSuspect).toBe(false)
    expect(failure.retryBaud).toBeNull()
    expect(`${failure.cause} ${failure.tryFirst}`).not.toMatch(/flash chip/i)
  })

  it('attempt 1 already at 115200 does not suggest lowering the baud', () => {
    const failure = describeWriteFailure(new Error('x'), ctx({ baudRate: LOWEST_BAUD }))
    expect(failure.tryFirst).toMatch(/cable/i)
    expect(failure.tryFirst).not.toMatch(/retry at/i)
  })

  it('attempt 2 at 921600 lowers the baud, still without naming the chip', () => {
    const failure = describeWriteFailure(new Error('x'), ctx({ attempt: 2 }))
    expect(failure.retryBaud).toBe(115200)
    expect(failure.chipSuspect).toBe(false)
    expect(`${failure.cause} ${failure.tryFirst}`).not.toMatch(/flash chip/i)
  })

  it('attempt 2 at 115200 is the only place the flash chip is suspected', () => {
    const failure = describeWriteFailure(new Error('x'), ctx({ attempt: 2, baudRate: 115200 }))
    expect(failure.chipSuspect).toBe(true)
    expect(failure.tryFirst).toMatch(/flash chip may be defective/i)
    expect(failure.tryFirst).toMatch(/failed 2 times/)
    expect(failure.retryBaud).toBeNull()
  })

  it('never gives the connect-time "Hold BOOT" advice mid-write', () => {
    for (const attempt of [1, 2, 3]) {
      for (const baudRate of [921600, 115200]) {
        const failure = describeWriteFailure(
          new Error('No serial data received.'),
          ctx({ attempt, baudRate }),
        )
        expect(`${failure.cause} ${failure.tryFirst}`).not.toMatch(/hold boot/i)
      }
    }
  })

  it('names the part and address being written', () => {
    const failure = describeWriteFailure(
      new DOMException('The device has been lost.', 'NetworkError'),
      ctx({ part: 'app', address: 0x20000 }),
    )
    expect(failure.cause).toMatch(/disconnected/i)
    expect(failure.cause).toContain('app')
    expect(failure.cause).toContain('0x20000')
  })

  it('describes each kind in plain words', () => {
    expect(describeWriteFailure(new Error('Failed to write compressed data to flash after seq 1 failed with status 1,1'), ctx()).cause).toMatch(/did not verify/i)
    expect(describeWriteFailure(new Error('boom'), ctx()).cause).toMatch(/failed partway/i)
  })
})
