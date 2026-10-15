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
import { LAST_ON_TITLE } from './network'
import { COALESCE_MS, type EventSourceLike } from './fleet'

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
    ssid: null,
    known_networks: null,
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
    // Present and null, mirroring `DeviceSummary.deploy` — a board that has never been
    // deployed to. Leaving it off would render the Deploy column from `undefined`.
    deploy: null,
    ...overrides,
  }
}

function deploy(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    cmd_id: 'cmd-a',
    state: 'confirmed',
    at: '2026-09-10T11:59:40Z',
    is_terminal: true,
    artifact_version: '1.5.0',
    from_version: '1.4.2',
    pct: null,
    detail: null,
    ...overrides,
  }
}

// A FACTORY, never a shared `Response`: a body can be read once, so handing the same
// object to two `fetch` calls makes the second one throw and reads as the API being down.
//
// URL-aware because `FleetView` now reads `/v1/artifact` too (R1-fe-1). The artifact
// list is a separate argument to `renderFleet`; the cell's own behaviour is tested in
// `DeployCell.test.tsx` and this file stays about the table.
function responds(body: unknown, status = 200, artifacts: unknown[] = []) {
  return async (input?: unknown) => {
    if (String(input) === '/v1/artifact') {
      return new Response(JSON.stringify({ artifacts }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    }
    return new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    })
  }
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
  devices: unknown[] | ((input?: unknown) => Promise<Response>),
  arrivals: unknown[] = [],
  artifacts: unknown[] = [],
) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(
    typeof devices === 'function' ? devices : responds({ devices, arrivals }, 200, artifacts),
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

  it('shows a named board as its name, with the device id beneath', async () => {
    await renderFleet([device({ name: 'coop door' }), device({ device_id: 'b2000000dead' })])

    const rows = screen.getAllByTestId('device-row')
    expect(rows[0]).toHaveTextContent('coop door')
    expect(within(rows[0]).getByText('a4cf12b3de90').tagName).toBe('CODE')
    // An unnamed board is still its id.
    expect(within(rows[1]).getByText('b2000000dead')).toBeInTheDocument()
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

  // R1-fe-1. The cell's own behaviour lives in `DeployCell.test.tsx`; what belongs here
  // is that the column exists, that every row has one, and that the state and the
  // version line up — which is the whole "the version changed" claim.
  it('gives every row a Deploy cell under a Deploy column', async () => {
    await renderFleet([device({ device_id: 'a4cf12b3de90' }), device({ device_id: 'b2000000dead' })])

    expect(screen.getByRole('columnheader', { name: 'Deploy' })).toBeInTheDocument()
    const rows = screen.getAllByTestId('device-row')
    expect(within(rows[0]).getByTestId('deploy-cell')).toBeInTheDocument()
    expect(within(rows[1]).getByTestId('deploy-cell')).toBeInTheDocument()
  })

  it('shows a confirmed deploy next to the firmware version it delivered', async () => {
    await renderFleet([device({ fw_version: '1.5.0', deploy: deploy({ artifact_version: '1.5.0' }) })])

    const row = screen.getAllByTestId('device-row')[0]
    expect(within(row).getByTestId('deploy-state')).toHaveTextContent('good')
    expect(row).toHaveTextContent('running 1.5.0')
    expect(row).toHaveTextContent('1.5.0')
  })

  it('shows good and rolled back per device', async () => {
    await renderFleet([
      device({ device_id: 'a4cf12b3de90', fw_version: '1.5.0', deploy: deploy() }),
      device({
        device_id: 'b2000000dead',
        fw_version: '1.4.2',
        deploy: deploy({ state: 'rolled_back' }),
      }),
    ])

    const rows = screen.getAllByTestId('device-row')
    const good = rows.find((r) => r.getAttribute('data-device-id') === 'a4cf12b3de90')!
    const bad = rows.find((r) => r.getAttribute('data-device-id') === 'b2000000dead')!
    expect(within(good).getByTestId('deploy-state').textContent).toBe('good')
    expect(within(bad).getByTestId('deploy-state').textContent).toBe('rolled back')
    expect(within(bad).getAllByRole('cell')[3]).toHaveTextContent('1.4.2')
  })

  it('bounces a dead session to the login screen instead of showing a page error', async () => {
    const { onSessionExpired } = await renderFleet(responds({ detail: 'not authenticated' }, 401))

    expect(onSessionExpired).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('the network (R2b-fe-13)', () => {
  const rowOf = () => screen.getAllByTestId('device-row')[0]

  it('says where an online board is, and how many networks it knows', async () => {
    await renderFleet([device({ ssid: 'shed', known_networks: 2 })])
    expect(within(rowOf()).getByTestId('device-network').textContent).toBe(', on: shed')
    expect(rowOf()).toHaveTextContent('online, on: shed')
    expect(rowOf()).toHaveTextContent('knows 2 networks')
    expect(within(rowOf()).getByLabelText('online')).toBeInTheDocument()
  })

  it('says "last on" for an offline board and never a cause', async () => {
    await renderFleet([device({ ssid: 'shed', known_networks: 2, online: false })])
    expect(rowOf()).toHaveTextContent('offline, last on: shed')
    expect(within(rowOf()).getByTestId('device-network')).toHaveAttribute('title', LAST_ON_TITLE)
    expect(rowOf().textContent).not.toMatch(/in range|out of range|no network/i)
  })

  it('says nothing when the board reported nothing', async () => {
    await renderFleet([device({ online: false })])
    expect(within(rowOf()).queryByTestId('device-network')).toBeNull()
    expect(within(rowOf()).queryByTestId('device-networks-known')).toBeNull()
    expect(within(rowOf()).getByLabelText('offline')).toBeInTheDocument()
  })

  it('uses the singular for one network', async () => {
    await renderFleet([device({ ssid: 'shed', known_networks: 1 })])
    expect(rowOf()).toHaveTextContent('knows 1 network')
    expect(rowOf().textContent).not.toMatch(/knows 1 networks/)
  })

  it('isolates a device-controlled SSID in <bdi>', async () => {
    await renderFleet([device({ ssid: '\u202Eevil', known_networks: 1 })])
    expect(within(rowOf()).getByText('\u202Eevil').tagName).toBe('BDI')
  })
})

describe('health (R4-fe-1)', () => {
  const rowOf = () => screen.getAllByTestId('device-row')[0]
  const cell = () => within(rowOf()).getByTestId('health-cell')

  const health = (overrides: Partial<Record<string, unknown>> = {}) => ({
    at: '2026-09-10T11:59:55Z',
    uptime_s: 20,
    rssi: -61,
    free_heap: 142000,
    min_free_heap: 98000,
    boot_ok: true,
    ...overrides,
  })
  const faults = (overrides: Partial<Record<string, unknown>> = {}) => ({
    window_s: 86400,
    panic: 0,
    task_wdt: 0,
    brownout: 0,
    ...overrides,
  })

  it('adds a Health column after Last seen, and a cell on every row', async () => {
    await renderFleet([device(), device({ device_id: 'b2000000dead' })])
    const headers = screen.getAllByRole('columnheader').map((h) => h.textContent)
    expect(headers).toEqual(['Status', 'Device', 'Platform', 'Firmware', 'Last seen', 'Health', 'Power', 'Deploy'])
    expect(screen.getAllByTestId('health-cell')).toHaveLength(2)
  })

  it('badges a panic reset with the word, a weight class and the raw reason', async () => {
    await renderFleet([device({ last_reset_reason: 'panic' })])
    const badge = within(rowOf()).getByTestId('reset-badge')
    expect(badge).toHaveTextContent('last reset: panic')
    expect(badge).toHaveClass('warn')
    expect(badge).toHaveAttribute('data-reason', 'panic')
    expect(badge).toHaveAttribute('title')
  })

  it('shows poweron and an unknown name without a badge', async () => {
    await renderFleet([device({ last_reset_reason: 'poweron' }), device({ device_id: 'b2000000dead', last_reset_reason: 'quantum_flip' })])
    const rows = screen.getAllByTestId('device-row')
    expect(within(rows[0]).queryByTestId('reset-badge')).toBeNull()
    expect(within(rows[0]).getByTestId('reset-reason')).toHaveTextContent('last reset: poweron')
    expect(within(rows[0]).getByTestId('reset-reason')).toHaveClass('muted')
    expect(within(rows[1]).queryByTestId('reset-badge')).toBeNull()
    expect(within(rows[1]).getByTestId('reset-reason')).toHaveTextContent('last reset: quantum_flip')
  })

  it('says "boot not confirmed" for boot_ok false and "boot ok" for true', async () => {
    await renderFleet([device({ health: health({ boot_ok: false }) }), device({ device_id: 'b2000000dead', health: health() })])
    const rows = screen.getAllByTestId('device-row')
    const bad = within(rows[0]).getByText('boot not confirmed')
    expect(bad).toHaveClass('bad')
    expect(within(rows[1]).getByTestId('boot-state')).toHaveTextContent('boot ok')
  })

  it('shows the heap low-water mark, uptime and signal', async () => {
    await renderFleet([device({ health: health() })])
    const vitals = within(cell()).getByTestId('device-vitals')
    expect(vitals).toHaveTextContent('heap low 95.7 KiB')
    expect(vitals).toHaveTextContent('up 20 s')
    expect(vitals).toHaveTextContent('-61 dBm')
    expect(vitals).not.toHaveClass('muted')
  })

  it('shows the server fault count, and nothing when it is all zero', async () => {
    await renderFleet([
      device({ fault_resets: faults({ panic: 2 }) }),
      device({ device_id: 'b2000000dead', fault_resets: faults() }),
    ])
    const rows = screen.getAllByTestId('device-row')
    expect(within(rows[0]).getByTestId('fault-resets')).toHaveTextContent('panic ×2 in 24 h')
    expect(within(rows[1]).queryByTestId('fault-resets')).toBeNull()
  })

  it('lists metrics by name', async () => {
    await renderFleet([
      device({
        metrics: [
          { name: 'sim_counter', value: 16, at: '2026-09-10T11:59:50Z' },
          { name: 'temp_c', value: 21.5, at: '2026-09-10T11:59:50Z' },
        ],
      }),
    ])
    const metrics = within(cell()).getAllByTestId('metric')
    expect(metrics.map((m) => m.getAttribute('data-name'))).toEqual(['sim_counter', 'temp_c'])
    expect(metrics[0]).toHaveTextContent('sim_counter 16')
    expect(metrics[1]).toHaveTextContent('temp_c 21.5')
    expect(within(cell()).queryByText(/more$/)).toBeNull()
  })

  it('folds metrics past the fourth behind "+N more"', async () => {
    const metrics = ['a', 'b', 'c', 'd', 'e', 'f'].map((name, i) => ({ name: `m_${name}`, value: i, at: '2026-09-10T11:59:50Z' }))
    await renderFleet([device({ metrics })])
    expect(within(cell()).getAllByTestId('metric')).toHaveLength(6)
    const more = within(cell()).getByText('+2 more')
    expect(more.tagName).toBe('SUMMARY')
    expect(more.closest('details')).toHaveTextContent('m_e')
    expect(more.closest('details')).not.toHaveTextContent('m_d')
  })

  it('says "last reported" and mutes the vitals of an offline board', async () => {
    await renderFleet([device({ online: false, health: health() })])
    const vitals = within(cell()).getByTestId('device-vitals')
    expect(vitals.textContent).toMatch(/^last reported /)
    expect(vitals).toHaveClass('muted')
    expect(vitals.getAttribute('title')).toContain('reported ')
  })

  it('renders an old-API row (none of the new keys) as a dash', async () => {
    await renderFleet([device()])
    expect(cell()).toHaveTextContent('—')
    expect(within(cell()).queryByTestId('reset-badge')).toBeNull()
  })
})

// The same controllable source as `fleet.test.tsx`, minimal: one `emit()`.
class FakeSource implements EventSourceLike {
  readyState = 1
  onopen: ((ev: Event) => unknown) | null = null
  onmessage: ((ev: MessageEvent<string>) => unknown) | null = null
  onerror: ((ev: Event) => unknown) | null = null
  close() {
    this.readyState = 2
  }
  emit(payload: unknown) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(payload) }))
  }
}

describe('health, live (R4-fe-1)', () => {
  async function live(first: string) {
    let reason = first
    const source = new FakeSource()
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input?: unknown) => {
      const body =
        String(input) === '/v1/artifact'
          ? { artifacts: [] }
          : { devices: [device({ last_reset_reason: reason })], arrivals: [] }
      return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
    })
    render(<FleetView onSessionExpired={vi.fn()} createEventSource={() => source} />)
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    const frame = async (payload: unknown) => {
      await act(async () => {
        source.emit(payload)
        await vi.advanceTimersByTimeAsync(COALESCE_MS + 1)
      })
    }
    return { frame, set: (r: string) => (reason = r) }
  }

  const ANNOUNCE = { v: 1, type: 'device.announce', device_id: 'a4cf12b3de90', at: '2026-09-10T12:00:00Z', online: true }

  it('shows the badge after a frame triggers a re-read, with no reload', async () => {
    const { frame, set } = await live('poweron')
    expect(screen.queryByTestId('reset-badge')).toBeNull()
    set('panic')
    await frame(ANNOUNCE)
    expect(screen.getByTestId('reset-badge')).toHaveAttribute('data-reason', 'panic')
  })

  it('never renders a reset reason carried by the frame body', async () => {
    const { frame } = await live('poweron')
    await frame({ ...ANNOUNCE, reset_reason: 'panic' })
    expect(screen.queryByTestId('reset-badge')).toBeNull()
    expect(screen.getByTestId('reset-reason')).toHaveTextContent('last reset: poweron')
  })
})
