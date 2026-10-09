// The partition-profiles section (R3-fe-1): the named table, the detected block, and
// naming one. The server's sentences render verbatim; the client refuses a bad name before
// any fetch.

import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { type DeviceSummary, type PartitionProfileSummary } from './api'
import { PartitionProfiles } from './PartitionProfiles'
import { useProfiles } from './profiles'

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
    device_ids: ['a4cf12b3de90'],
    created_at: '2026-10-09T10:00:00Z',
    adopted_at: '2026-10-09T10:00:00Z',
    ...over,
  }
}

const builtin = profile()
const pending = (over: Partial<PartitionProfileSummary> = {}) =>
  profile({
    partition_table_sha256: SHA_P,
    layout_id: null,
    origin: 'detected',
    deployable: false,
    detected_device_id: 'b26a938324ab',
    device_ids: ['b26a938324ab'],
    adopted_at: null,
    ...over,
  })

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function Harness({
  devices = null,
  onSessionExpired = vi.fn(),
}: {
  devices?: DeviceSummary[] | null
  onSessionExpired?: () => void
}) {
  const profiles = useProfiles({ onSessionExpired, hint: 0 })
  return (
    <PartitionProfiles
      profiles={profiles}
      devices={devices}
      now={Date.parse('2026-10-09T10:05:00Z')}
      onSessionExpired={onSessionExpired}
    />
  )
}

/** GET answers the current list; PATCH is `onPatch`. Records every call. */
function mockServer(
  list: () => PartitionProfileSummary[],
  onPatch: (body: Record<string, unknown>) => Response = () => json({}),
) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input?: unknown, init?: RequestInit) => {
    const url = String(input)
    if (url.startsWith('/v1/partition-profiles/') && init?.method === 'PATCH') {
      return onPatch(JSON.parse(String(init.body)))
    }
    if (url === '/v1/partition-profiles') return json({ profiles: list() })
    return json({})
  })
}

const nameInput = () => screen.getByLabelText('Name for this map')
const submit = () => screen.getByRole('button', { name: 'Name and adopt' })
const patches = (spy: ReturnType<typeof mockServer>) =>
  spy.mock.calls.filter(([, init]) => init?.method === 'PATCH')

afterEach(() => vi.restoreAllMocks())

describe('PartitionProfiles', () => {
  it('lists a builtin as named and a detected map as pending', async () => {
    mockServer(() => [builtin, pending()])
    render(<Harness />)

    const rows = await screen.findAllByTestId('profile-row')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveAttribute('data-deployable', 'true')
    expect(rows[0]).toHaveAttribute('data-origin', 'builtin')
    expect(rows[0]).toHaveTextContent('ab-4m-v1')
    expect(rows[0]).toHaveTextContent('built in')
    expect(rows[0]).toHaveTextContent('1,966,080 bytes')
    expect(rows[1]).toHaveAttribute('data-deployable', 'false')
    expect(rows[1]).toHaveAttribute('data-sha', SHA_P)
    expect(rows[1]).toHaveTextContent('1,966,080 bytes')
    expect(rows[1]).toHaveTextContent('4,194,304 bytes')
    expect(rows[1]).toHaveTextContent(SHA_P)
    expect(screen.getByText('Detected, not named yet')).toBeInTheDocument()
  })

  it('shows no detected block when nothing is pending', async () => {
    mockServer(() => [builtin])
    render(<Harness />)
    await screen.findAllByTestId('profile-row')
    expect(screen.queryByText('Detected, not named yet')).not.toBeInTheDocument()
  })

  it('shows the detecting board by name when the fleet has one', async () => {
    mockServer(() => [builtin, pending()])
    render(<Harness devices={[{ device_id: 'b26a938324ab', name: 'coop door' } as DeviceSummary]} />)
    const row = (await screen.findAllByTestId('profile-row'))[1]
    expect(row).toHaveTextContent('First seen on coop door')
    expect(row).not.toHaveTextContent('b26a938324ab')
  })

  it('names a detected map: one PATCH with only the name, then a re-read', async () => {
    let named = false
    const spy = mockServer(
      () => (named ? [builtin, profile({ partition_table_sha256: SHA_P, layout_id: 'be2-map', origin: 'detected' })] : [builtin, pending()]),
      () => {
        named = true
        return json(profile({ partition_table_sha256: SHA_P, layout_id: 'be2-map', origin: 'detected' }))
      },
    )
    render(<Harness />)
    await screen.findByText('Detected, not named yet')

    await userEvent.type(nameInput(), 'be2-map')
    await userEvent.click(submit())

    expect(await screen.findByTestId('profile-adopted')).toHaveTextContent('Named be2-map.')
    const [call] = patches(spy)
    expect(String(call[0])).toBe(`/v1/partition-profiles/${SHA_P}`)
    expect(call[1]?.body).toBe('{"layout_id":"be2-map"}')
    await waitFor(() => {
      const rows = screen.getAllByTestId('profile-row')
      expect(rows[1]).toHaveAttribute('data-deployable', 'true')
      expect(rows[1]).toHaveTextContent('be2-map')
    })
    expect(screen.queryByText('Detected, not named yet')).not.toBeInTheDocument()
  })

  it('asks for the slot size when none was measured, and sends it as a number', async () => {
    const spy = mockServer(
      () => [builtin, pending({ ota_slot_size: null })],
      () => json(profile({ layout_id: 'be2-map' })),
    )
    render(<Harness />)
    await screen.findByText('Detected, not named yet')

    await userEvent.type(nameInput(), 'be2-map')
    await userEvent.click(submit())
    expect(await screen.findByTestId('profile-adopt-error')).toHaveTextContent('OTA slot')
    expect(patches(spy)).toHaveLength(0)

    await userEvent.type(screen.getByLabelText('OTA slot size (bytes)'), '1966080')
    await userEvent.click(submit())
    await waitFor(() => expect(patches(spy)).toHaveLength(1))
    expect(patches(spy)[0][1]?.body).toBe('{"layout_id":"be2-map","ota_slot_size":1966080}')
  })

  it('does not ask for a slot size when one was measured', async () => {
    mockServer(() => [builtin, pending()])
    render(<Harness />)
    await screen.findByText('Detected, not named yet')
    expect(screen.queryByLabelText('OTA slot size (bytes)')).not.toBeInTheDocument()
  })

  it('renders a 409 detail verbatim', async () => {
    const detail = 'the name be2-map is already taken by another partition profile; pick another'
    mockServer(
      () => [builtin, pending()],
      () => json({ detail }, 409),
    )
    render(<Harness />)
    await screen.findByText('Detected, not named yet')
    await userEvent.type(nameInput(), 'be2-map')
    await userEvent.click(submit())

    const alert = await screen.findByTestId('profile-adopt-error')
    expect(alert).toHaveTextContent(detail)
    expect(alert).toHaveAttribute('role', 'alert')
    expect(screen.queryByTestId('profile-adopted')).not.toBeInTheDocument()
  })

  it.each(['Unknown', 'unknown', '-x'])('refuses %s before any fetch', async (bad) => {
    const spy = mockServer(() => [builtin, pending()])
    render(<Harness />)
    await screen.findByText('Detected, not named yet')
    await userEvent.type(nameInput(), bad)
    await userEvent.click(submit())

    expect(await screen.findByTestId('profile-adopt-error')).toBeInTheDocument()
    expect(patches(spy)).toHaveLength(0)
  })

  it('sends a 401 to the login gate', async () => {
    const expired = vi.fn()
    mockServer(
      () => [builtin, pending()],
      () => json({ detail: 'no' }, 401),
    )
    render(<Harness onSessionExpired={expired} />)
    await screen.findByText('Detected, not named yet')
    await userEvent.type(nameInput(), 'be2-map')
    await userEvent.click(submit())
    await waitFor(() => expect(expired).toHaveBeenCalledTimes(1))
  })

  it('says so under the heading when the read fails, and still renders', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json({ detail: 'boom' }, 500))
    render(<Harness />)
    expect(await screen.findByTestId('profiles-error')).toHaveTextContent(
      'boom — the partition profiles could not be read.',
    )
    expect(within(screen.getByTestId('profiles')).getByRole('heading', { name: 'Partition profiles' })).toBeInTheDocument()
  })
})
