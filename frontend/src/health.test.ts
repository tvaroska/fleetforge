import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { HEALTH_POLL_MS, useHealth } from './health'

function respondWith(version: string) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
    new Response(JSON.stringify({ status: 'ok', version }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }),
  )
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('useHealth', () => {
  it('goes from loading to ok after the first read', async () => {
    respondWith('0.4.2')
    const { result } = renderHook(() => useHealth())
    expect(result.current).toEqual({ phase: 'loading' })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current).toMatchObject({ phase: 'ok', health: { version: '0.4.2' } })
  })

  it('re-polls and shows a changed version', async () => {
    const fetchMock = respondWith('0.4.2')
    const { result } = renderHook(() => useHealth())
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    respondWith('0.4.3')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(HEALTH_POLL_MS)
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(result.current).toMatchObject({ phase: 'ok', health: { version: '0.4.3' } })
  })

  it('reads a failed fetch as unreachable', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('down'))
    const { result } = renderHook(() => useHealth())
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current).toEqual({ phase: 'unreachable' })
  })

  it('stops polling on unmount', async () => {
    const fetchMock = respondWith('0.4.2')
    const { unmount } = renderHook(() => useHealth())
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    unmount()
    await vi.advanceTimersByTimeAsync(HEALTH_POLL_MS * 3)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
