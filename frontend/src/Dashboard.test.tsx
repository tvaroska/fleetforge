// The signed-in page: one fleet hook shared by the table and the strip, and the rule for
// which board the strip shows (picked, deployed to, or the only one).

import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Dashboard } from './Dashboard'
import { type DeviceSummary } from './api'
import { type EventSourceLike } from './fleet'
import { type BoardFlasher, type FlasherFactory } from './flasher'

const ui = { version: '0.4.2', commit: 'unknown', builtAt: 'unknown' }
const health = { phase: 'ok', health: { status: 'ok', version: '0.4.2' } } as const

function device(over: Partial<DeviceSummary> = {}): DeviceSummary {
  return {
    device_id: 'a4cf12b3de90',
    name: null,
    group_id: null,
    platform_type: 'esp32c6',
    fw_version: '1.4.2',
    agent_version: '0.1.0',
    link_type: 'wifi',
    power_class: 'always_on',
    expected_wake_interval_s: null,
    parent_device_id: null,
    partition_layout: 'ab-4m-v1',
    ota_slot_size: 1966080,
    capabilities: ['ota'],
    last_seen: '2026-09-10T11:59:30Z',
    enrolled_at: '2026-09-10T11:00:00Z',
    broker_provisioned_at: '2026-09-10T11:00:00Z',
    online: true,
    deploy: null,
    ...over,
  }
}

const artifact = {
  target: 'esp32c6',
  version: '1.5.0',
  sha256: 'a'.repeat(64),
  size_bytes: 230_000,
  partition_layout: 'ab-4m-v1',
  kind: 'user_firmware',
  created_at: '2026-09-10T11:00:00Z',
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function mockApi(devices: DeviceSummary[]) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input?: unknown, init?: RequestInit) => {
    const url = String(input)
    if (url === '/v1/artifact') return json({ artifacts: [artifact] })
    if (url.startsWith('/v1/devices/') && init?.method === 'POST') {
      return json({ cmd_id: 'c', device_id: 'x', version: '1.5.0', apply: 'auto', reused: false })
    }
    if (url === '/v1/devices') return json({ devices, arrivals: [] })
    if (url === '/v1/agent/manifest') {
      return json({
        agent_version: '0.4.5',
        builds: [{ chip_family: 'ESP32', agent_version: '0.4.5', partition_layout: 'ab-4m-v1', parts: [] }],
      })
    }
    if (url === '/v1/enrollment-tokens') return json({ tokens: [] })
    return json({})
  })
}

function renderDashboard(createFlasher?: FlasherFactory) {
  const sources = vi.fn(
    (): EventSourceLike => ({
      readyState: 0,
      close: () => {},
      onopen: null,
      onmessage: null,
      onerror: null,
    }),
  )
  render(
    <Dashboard
      me={{ token_id: 't', subject: 'admin' } as never}
      expire={vi.fn()}
      signOut={vi.fn()}
      health={health}
      ui={ui}
      createEventSource={sources}
      createFlasher={createFlasher}
    />,
  )
  return { sources }
}

afterEach(() => vi.restoreAllMocks())

describe('Dashboard', () => {
  it('asks for a pick when two boards exist, then follows the click', async () => {
    mockApi([device(), device({ device_id: 'b26a938324ab', fw_version: '1.4.3' })])
    renderDashboard()

    expect(await screen.findByText(/none selected/)).toBeInTheDocument()
    const row = (await screen.findAllByTestId('device-row'))[1]
    await userEvent.click(within(row).getByRole('button', { name: 'b26a938324ab' }))

    const strip = screen.getByTestId('strip-board')
    expect(strip).toHaveTextContent('b26a938324ab · esp32c6 · fw 1.4.3 · online')
    expect(row).toHaveAttribute('data-selected', 'true')
  })

  it('implies the only board without a click', async () => {
    mockApi([device()])
    renderDashboard()
    await waitFor(() =>
      expect(screen.getByTestId('strip-board')).toHaveTextContent('a4cf12b3de90 · esp32c6'),
    )
  })

  it('makes the deployed row the strip board', async () => {
    mockApi([device(), device({ device_id: 'b26a938324ab' })])
    renderDashboard()
    const row = (await screen.findAllByTestId('device-row'))[1]
    await waitFor(() => expect(within(row).getByRole('button', { name: 'Deploy' })).toBeEnabled())
    await userEvent.click(within(row).getByRole('button', { name: 'Deploy' }))
    await waitFor(() =>
      expect(screen.getByTestId('strip-board')).toHaveTextContent('b26a938324ab'),
    )
  })

  it('opens exactly one event stream', async () => {
    mockApi([device()])
    const { sources } = renderDashboard()
    await screen.findByTestId('strip-board')
    expect(sources).toHaveBeenCalledTimes(1)
  })

  describe('pre-flight card', () => {
    const fakeFlasher: FlasherFactory = async () =>
      ({
        detect: async () => ({
          chipName: 'ESP32',
          description: 'ESP32-D0WD-V3',
          macAddress: 'A4:CF:12:B3:DE:90',
          flashSizeBytes: 4 * 1024 * 1024,
          features: [],
        }),
        write: async () => {},
        finish: async () => {},
        close: async () => {},
      }) as BoardFlasher

    async function detectOnDashboard(devices: DeviceSummary[]) {
      Object.defineProperty(navigator, 'serial', { value: {}, configurable: true })
      Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true })
      mockApi(devices)
      const { sources } = renderDashboard(fakeFlasher)
      await screen.findByTestId('strip-board')
      await userEvent.click(screen.getByRole('button', { name: /select port and detect/i }))
      return { sources, card: await screen.findByTestId('preflight-card') }
    }

    it('is known, with the listed firmware, from the page fleet', async () => {
      const { sources, card } = await detectOnDashboard([device()])
      await waitFor(() => expect(card).toHaveAttribute('data-kind', 'known'))
      expect(card).toHaveTextContent('fw 1.4.2')
      expect(sources).toHaveBeenCalledTimes(1)
    })

    it('is new for an empty fleet', async () => {
      const { sources, card } = await detectOnDashboard([])
      await waitFor(() => expect(card).toHaveAttribute('data-kind', 'new'))
      expect(sources).toHaveBeenCalledTimes(1)
    })
  })
})
