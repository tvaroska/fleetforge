// The device log tail: rows, gaps, untrusted text, and the read rules (switching boards,
// the hint, 401, a failed re-read). The Dashboard wiring is in `Dashboard.test.tsx`.

import { render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { DeviceLog } from './DeviceLog'
import { type DeviceLogLine, type DeviceSummary } from './api'

const ID = 'a4cf12b3de90'

function device(over: Partial<DeviceSummary> = {}): DeviceSummary {
  return {
    device_id: ID,
    name: null,
    group_id: null,
    platform_type: 'esp32c6',
    fw_version: '1.4.2',
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
    last_seen: '2026-10-15T10:00:00Z',
    enrolled_at: '2026-10-15T09:00:00Z',
    broker_provisioned_at: '2026-10-15T09:00:00Z',
    online: true,
    deploy: null,
    ...over,
  }
}

function line(over: Partial<DeviceLogLine> = {}): DeviceLogLine {
  return {
    id: 1,
    logged_at: '2026-10-15T10:00:00Z',
    received_at: '2026-10-15T10:00:01Z',
    level: 'I',
    text: 'hello',
    dropped: 0,
    ...over,
  }
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

type Handler = (url: string) => Response | Promise<Response>

function mockFetch(handler: Handler) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input?: unknown) => handler(String(input)))
}

function renderLog(
  props: Partial<Parameters<typeof DeviceLog>[0]> = {},
) {
  const onSessionExpired = vi.fn()
  const ui = (p: Partial<Parameters<typeof DeviceLog>[0]>) => (
    <DeviceLog
      deviceId={ID}
      device={device()}
      hasBoards
      onSessionExpired={onSessionExpired}
      {...props}
      {...p}
    />
  )
  const view = render(ui({}))
  return { onSessionExpired, rerender: (p: Partial<Parameters<typeof DeviceLog>[0]>) => view.rerender(ui(p)) }
}

const cell = (row: HTMLElement, n: number) => (row as HTMLTableRowElement).cells[n]!

const logCalls = (spy: ReturnType<typeof mockFetch>) =>
  spy.mock.calls.map(([input]) => String(input)).filter((u) => u.includes('/logs'))

afterEach(() => vi.restoreAllMocks())

describe('DeviceLog', () => {
  it('requests the board with the explicit limit, encoding the id', async () => {
    const spy = mockFetch(() => json({ device_id: ID, lines: [] }))
    renderLog()
    await screen.findByTestId('device-log-empty')
    expect(logCalls(spy)).toEqual([`/v1/devices/${ID}/logs?limit=200`])

    spy.mockClear()
    renderLog({ deviceId: 'a/b c', device: device({ device_id: 'a/b c' }) })
    await waitFor(() => expect(logCalls(spy)).toEqual(['/v1/devices/a%2Fb%20c/logs?limit=200']))
  })

  it('renders the server order, levels, and a gap directly under the line that carried dropped', async () => {
    mockFetch(() =>
      json({
        device_id: ID,
        lines: [
          line({ id: 3, level: 'E', text: 'boom', dropped: 3 }),
          line({ id: 2, level: 'W', text: 'sensor' }),
          line({ id: 1, level: 'I', text: 'morse' }),
        ],
      }),
    )
    renderLog()
    const table = await screen.findByTestId('device-log-lines')
    const rows = within(table)
      .getAllByRole('row')
      .slice(1)
    expect(rows.map((r) => r.getAttribute('data-testid'))).toEqual([
      'log-line',
      'log-gap',
      'log-line',
      'log-line',
    ])
    expect(rows[0]).toHaveAttribute('data-level', 'E')
    expect(cell(rows[0]!, 1)).toHaveClass('bad')
    expect(cell(rows[2]!, 1)).toHaveClass('warn')
    expect(cell(rows[3]!, 1)).not.toHaveClass('bad')
    expect(rows[1]).toHaveAttribute('data-dropped', '3')
    expect(rows[1]).toHaveTextContent('3 lines missing here')
    expect(rows[0]).toHaveTextContent('boom')
    expect(rows[2]).toHaveTextContent('sensor')
  })

  it('renders a marker row as a gap only', async () => {
    mockFetch(() => json({ device_id: ID, lines: [line({ id: 5, text: null, dropped: 7 })] }))
    renderLog()
    const gap = await screen.findByTestId('log-gap')
    expect(gap).toHaveAttribute('data-dropped', '7')
    expect(screen.queryByTestId('log-line')).toBeNull()
  })

  it('renders device text literally, never as markup', async () => {
    const text = '<img src=x onerror="window.__pwned=1"> <url>'
    mockFetch(() => json({ device_id: ID, lines: [line({ text })] }))
    renderLog()
    const table = await screen.findByTestId('device-log-lines')
    expect(within(table).getByText(text)).toBeInTheDocument()
    expect(table.querySelector('img')).toBeNull()
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined()
  })

  it('shows the empty state, and survives a body with no lines key', async () => {
    mockFetch(() => json({}))
    renderLog()
    expect(await screen.findByTestId('device-log-empty')).toBeInTheDocument()
  })

  it('notes the cap at 200 lines', async () => {
    const lines = Array.from({ length: 200 }, (_, i) => line({ id: 200 - i, text: `l${i}` }))
    mockFetch(() => json({ device_id: ID, lines }))
    renderLog()
    expect(await screen.findByTestId('device-log-capped')).toBeInTheDocument()
  })

  it('bounces a 401 to the login gate once', async () => {
    mockFetch(() => json({ detail: 'not authenticated' }, 401))
    const { onSessionExpired } = renderLog()
    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1))
  })

  it('keeps the last lines when a re-read fails, and says so', async () => {
    let fail = false
    mockFetch(() =>
      fail ? json({ detail: 'boom' }, 500) : json({ device_id: ID, lines: [line({ text: 'kept' })] }),
    )
    const { rerender } = renderLog()
    await screen.findByText('kept')
    fail = true
    rerender({ device: device({ last_seen: '2026-10-15T10:00:05Z' }) })
    expect(await screen.findByTestId('device-log-error')).toHaveTextContent('boom')
    expect(screen.getByText('kept')).toBeInTheDocument()
  })

  it('never shows one board’s lines under another', async () => {
    const OTHER = 'b26a938324ab'
    let release: (r: Response) => void = () => {}
    mockFetch((url) =>
      url.includes(OTHER)
        ? new Promise<Response>((resolve) => {
            release = resolve
          })
        : json({ device_id: ID, lines: [line({ text: 'from A' })] }),
    )
    const { rerender } = renderLog()
    await screen.findByText('from A')

    rerender({ deviceId: OTHER, device: device({ device_id: OTHER }) })
    expect(screen.queryByText('from A')).toBeNull()
    expect(screen.getByText('Loading…')).toBeInTheDocument()

    release(json({ device_id: OTHER, lines: [line({ id: 9, text: 'from B' })] }))
    expect(await screen.findByText('from B')).toBeInTheDocument()
  })

  it('re-reads when the hint changes, and not when it does not', async () => {
    let lines = [line({ id: 1, text: 'old' })]
    const spy = mockFetch(() => json({ device_id: ID, lines }))
    const { rerender } = renderLog()
    await screen.findByText('old')
    expect(logCalls(spy)).toHaveLength(1)

    rerender({})
    rerender({ device: device() })
    expect(logCalls(spy)).toHaveLength(1)

    lines = [line({ id: 2, text: 'new' }), ...lines]
    rerender({ device: device({ last_seen: '2026-10-15T10:00:07Z' }) })
    await screen.findByText('new')
    expect(logCalls(spy)).toHaveLength(2)
    const texts = screen.getAllByTestId('log-line').map((r) => cell(r, 2).textContent)
    expect(texts).toEqual(['new', 'old'])
  })

  it('asks for a pick, with no fetch, when boards exist and none is selected', () => {
    const spy = mockFetch(() => json({}))
    renderLog({ deviceId: null, device: null })
    expect(screen.getByTestId('device-log-none')).toBeInTheDocument()
    expect(logCalls(spy)).toEqual([])
  })

  it('does not fetch for a board that is not in the fleet yet', () => {
    const spy = mockFetch(() => json({}))
    renderLog({ device: null })
    expect(screen.getByTestId('device-log-pending')).toBeInTheDocument()
    expect(logCalls(spy)).toEqual([])
  })

  it('renders nothing for an empty fleet', () => {
    mockFetch(() => json({}))
    renderLog({ hasBoards: false, deviceId: null, device: null })
    expect(screen.queryByTestId('device-log')).toBeNull()
  })
})
