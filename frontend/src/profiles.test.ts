// The pure helpers over the profile list, and the one read: mount, hint, 401, failure.

import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { type PartitionProfileSummary } from './api'
import { adoptedNames, effectiveLayout, pendingProfiles, shortSha, useProfiles } from './profiles'

const SHA_A = 'a'.repeat(64)
const SHA_P = 'f'.repeat(64)

function profile(over: Partial<PartitionProfileSummary> = {}): PartitionProfileSummary {
  return {
    partition_table_sha256: '1'.repeat(64),
    layout_id: 'ab-4m-v1',
    origin: 'builtin',
    deployable: true,
    ota_slot_size: 1_966_080,
    flash_chip_size: 4_194_304,
    detected_device_id: null,
    device_ids: [],
    created_at: '2026-10-09T10:00:00Z',
    adopted_at: '2026-10-09T10:00:00Z',
    ...over,
  }
}

const builtin = profile()
const adopted = profile({
  partition_table_sha256: SHA_A,
  layout_id: 'be2-map',
  origin: 'detected',
})
const pending = profile({
  partition_table_sha256: SHA_P,
  layout_id: null,
  origin: 'detected',
  deployable: false,
  adopted_at: null,
})

describe('adoptedNames', () => {
  it('lists deployable names only, sorted and de-duplicated', () => {
    expect(adoptedNames([adopted, pending, builtin, profile({ layout_id: 'ab-4m-v1' })])).toEqual([
      'ab-4m-v1',
      'be2-map',
    ])
  })
})

describe('pendingProfiles', () => {
  it('keeps the non-deployable rows in the server order', () => {
    const second = profile({ ...pending, partition_table_sha256: 'e'.repeat(64) })
    expect(pendingProfiles([pending, builtin, second])).toEqual([pending, second])
  })
})

describe('effectiveLayout', () => {
  const all = [builtin, adopted, pending]

  it('is the announced id when it is an adopted name', () => {
    expect(effectiveLayout({ partition_layout: 'ab-4m-v1', partition_table_sha256: null }, all)).toBe(
      'ab-4m-v1',
    )
  })

  it('resolves an unknown board on an adopted map to that name', () => {
    expect(
      effectiveLayout({ partition_layout: 'unknown', partition_table_sha256: SHA_A }, all),
    ).toBe('be2-map')
  })

  it('is null for an unknown board on a pending map', () => {
    expect(
      effectiveLayout({ partition_layout: 'unknown', partition_table_sha256: SHA_P }, all),
    ).toBeNull()
  })

  it('is null for a non-adopted announced id with no fingerprint', () => {
    expect(effectiveLayout({ partition_layout: 'mystery', partition_table_sha256: null }, all)).toBeNull()
    expect(effectiveLayout({ partition_layout: null, partition_table_sha256: null }, all)).toBeNull()
  })
})

describe('shortSha', () => {
  it('keeps 12 hex characters and an ellipsis', () => {
    expect(shortSha(SHA_A)).toBe('aaaaaaaaaaaa…')
  })
})

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

describe('useProfiles', () => {
  afterEach(() => vi.restoreAllMocks())

  it('reads on mount, and again when the hint goes 0 to 1', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockImplementation(async () => json({ profiles: [builtin] }))
    const { result, rerender } = renderHook(
      ({ hint }) => useProfiles({ onSessionExpired: vi.fn(), hint }),
      { initialProps: { hint: 0 } },
    )
    await waitFor(() => expect(result.current.profiles).toEqual([builtin]))
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(String(fetchMock.mock.calls[0][0])).toBe('/v1/partition-profiles')

    rerender({ hint: 1 })
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  })

  it('sends a 401 to the login gate', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json({ detail: 'no' }, 401))
    const expired = vi.fn()
    renderHook(() => useProfiles({ onSessionExpired: expired, hint: 0 }))
    await waitFor(() => expect(expired).toHaveBeenCalledTimes(1))
  })

  it('shows a 500 as an error and keeps the last list', async () => {
    vi.spyOn(globalThis, 'fetch')
      .mockImplementationOnce(async () => json({ profiles: [builtin] }))
      .mockImplementation(async () => json({ detail: 'database is down' }, 500))
    const { result } = renderHook(() => useProfiles({ onSessionExpired: vi.fn(), hint: 0 }))
    await waitFor(() => expect(result.current.profiles).toEqual([builtin]))

    act(() => result.current.reload())
    await waitFor(() => expect(result.current.error).toBe('database is down'))
    expect(result.current.profiles).toEqual([builtin])
  })
})
