// What these tests defend, in order of how much a regression would cost:
//
// 1. Presence is rendered from the server's `online`, with an accessible name — the
//    status is not colour-only, and it is not recomputed from `last_seen` here.
// 2. `fw_version` is on screen. It is how an operator knows an OTA landed, and it is
//    the field the task line names.
// 3. A 401 on the first read bounces to login rather than rendering as a page error.
//
// The refresh rules themselves live in `fleet.test.tsx`; this file is rendering.

import { render, screen, within } from '@testing-library/react'
import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { FleetView } from './FleetView'
import { type EventSourceLike } from './fleet'

const NOW = new Date('2026-09-10T12:00:00Z')

function device(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    device_id: 'a4cf12b3de90',
    name: null,
    group_id: null,
    platform_type: 'esp32',
    fw_version: '0.1.0',
    agent_version: '0.1.0',
    link_type: 'wifi',
    power_class: 'always_on',
    expected_wake_interval_s: null,
    parent_device_id: null,
    partition_layout: 'ab-4m-v1',
    ota_slot_size: 1966080,
    capabilities: [],
    last_seen: '2026-09-10T11:59:30Z',
    enrolled_at: '2026-09-10T11:00:00Z',
    broker_provisioned_at: '2026-09-10T11:00:00Z',
    online: true,
    ...overrides,
  }
}

// A FACTORY, never a shared `Response`: a body can be read once, so handing the same
// object to two `fetch` calls makes the second one throw and reads as the API being down.
function responds(body: unknown, status = 200) {
  return async () =>
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

// A source that does nothing: these tests never fire a frame.
const inertSource = (): EventSourceLike => ({
  readyState: 0,
  close: () => {},
  onopen: null,
  onmessage: null,
  onerror: null,
})

function arrival(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    device_id: 'c0ffee000001',
    stage: 'enrolling',
    detail: null,
    at: '2026-09-10T11:59:50Z',
    stalled: false,
    ...overrides,
  }
}

async function renderFleet(
  devices: unknown[] | (() => Promise<Response>),
  arrivals: unknown[] = [],
) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(
    typeof devices === 'function' ? devices : responds({ devices, arrivals }),
  )
  const onSessionExpired = vi.fn()
  render(<FleetView onSessionExpired={onSessionExpired} createEventSource={inertSource} />)
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
  return { onSessionExpired }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('FleetView', () => {
  it('renders one row per device, with the server-computed presence', async () => {
    await renderFleet([device({ device_id: 'a4cf12b3de90' }), device({ device_id: 'b2000000dead', online: false })])

    const rows = screen.getAllByTestId('device-row')
    expect(rows).toHaveLength(2)
    expect(within(rows[0]).getByLabelText('online')).toBeInTheDocument()
    expect(within(rows[1]).getByLabelText('offline')).toBeInTheDocument()
    expect(rows[1]).toHaveAttribute('data-device-id', 'b2000000dead')
  })

  it('shows the announced firmware version, and an em dash before the board announces', async () => {
    await renderFleet([device({ fw_version: '1.4.2' }), device({ device_id: 'b2000000dead', fw_version: null })])

    const rows = screen.getAllByTestId('device-row')
    expect(rows[0]).toHaveTextContent('1.4.2')
    expect(rows[1]).toHaveTextContent('—')
  })

  it('renders last-seen as a relative age with the absolute time as a tooltip', async () => {
    await renderFleet([device({ last_seen: '2026-09-10T11:59:30Z' })])

    const row = screen.getAllByTestId('device-row')[0]
    expect(row).toHaveTextContent('30 s ago')
    const cell = within(row).getByText('30 s ago')
    expect(cell).toHaveAttribute('title', new Date('2026-09-10T11:59:30Z').toLocaleString())
  })

  it('says "never" for a board that has enrolled and never published', async () => {
    await renderFleet([device({ last_seen: null, fw_version: null, online: false })])
    expect(screen.getAllByTestId('device-row')[0]).toHaveTextContent('never')
  })

  it('ticks the relative age forward once a second without refetching', async () => {
    const { onSessionExpired } = await renderFleet([device({ last_seen: '2026-09-10T11:59:30Z' })])
    const fetchMock = vi.mocked(globalThis.fetch)
    const before = fetchMock.mock.calls.length

    // Advancing the fake clock advances `Date.now()` too — no setSystemTime needed.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })

    expect(screen.getAllByTestId('device-row')[0]).toHaveTextContent('35 s ago')
    expect(fetchMock.mock.calls.length).toBe(before)
    expect(onSessionExpired).not.toHaveBeenCalled()
  })

  it('names the sleepy wake interval, which is WHY such a row goes offline', async () => {
    await renderFleet([device({ power_class: 'sleepy', expected_wake_interval_s: 300, online: false })])

    const row = screen.getAllByTestId('device-row')[0]
    expect(within(row).getByText('sleepy')).toHaveAttribute('title', 'wakes every 300 s')
  })

  it('invites the operator to enroll when the fleet is empty', async () => {
    await renderFleet([])
    expect(screen.getByText(/no boards yet/i)).toBeInTheDocument()
    expect(screen.queryAllByTestId('device-row')).toHaveLength(0)
  })

  // S0-fw-1. What these defend: a board between "flashed" and "online" used to render
  // as nothing at all, and "nothing at all" is what a board that was never flashed
  // looks like too.
  it('shows a board that has reported a stage but is not in the fleet yet', async () => {
    await renderFleet([], [arrival({ device_id: 'c0ffee000001', stage: 'enrolling' })])

    const rows = screen.getAllByTestId('arrival-row')
    expect(rows).toHaveLength(1)
    expect(rows[0]).toHaveAttribute('data-device-id', 'c0ffee000001')
    expect(rows[0]).toHaveTextContent('enrolling')
    expect(rows[0]).toHaveTextContent('10 s ago')
  })

  it('marks a stalled arrival with a WORD, not only a colour', async () => {
    await renderFleet([], [arrival({ stalled: true, detail: 'attempt 3' })])

    const row = screen.getAllByTestId('arrival-row')[0]
    // `getByText` reads the accessible text: a class name alone would not satisfy it.
    expect(within(row).getByText('stalled')).toBeInTheDocument()
    expect(row).toHaveTextContent('attempt 3')
  })

  it('renders a stage this dashboard has never heard of as itself', async () => {
    await renderFleet([], [arrival({ stage: 'calibrating_radio' })])
    expect(screen.getAllByTestId('arrival-row')[0]).toHaveTextContent('calibrating_radio')
  })

  it('shows no arrivals section at all when nothing is arriving', async () => {
    await renderFleet([device()])
    expect(screen.queryByTestId('arrivals')).toBeNull()
  })

  it('does not tell the operator to flash a board while one is mid-arrival', async () => {
    await renderFleet([], [arrival()])
    expect(screen.queryByText(/no boards yet/i)).toBeNull()
    expect(screen.getByText(/no boards have finished enrolling/i)).toBeInTheDocument()
  })

  it('bounces a dead session to the login screen instead of showing a page error', async () => {
    const { onSessionExpired } = await renderFleet(responds({ detail: 'not authenticated' }, 401))

    expect(onSessionExpired).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
