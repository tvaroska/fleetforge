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
    ssid: null,
    known_networks: null,
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

const builtinProfile = {
  partition_table_sha256: '1'.repeat(64),
  layout_id: 'ab-4m-v1',
  origin: 'builtin',
  deployable: true,
  ota_slot_size: 1966080,
  flash_chip_size: 4194304,
  detected_device_id: null,
  device_ids: [],
  created_at: '2026-10-09T10:00:00Z',
  adopted_at: '2026-10-09T10:00:00Z',
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

// The log tail the mocked `GET /v1/devices/{id}/logs` answers with, per device id.
type LogLines = Record<string, unknown[]>

function mockApi(devices: DeviceSummary[] | (() => DeviceSummary[]), logs: () => LogLines = () => ({})) {
  // Once an upload has been POSTed the list read returns the newer version first, the
  // way the server's `created_at DESC` does.
  let uploaded = false
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input?: unknown, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST' && url.startsWith('/v1/artifact?')) {
      uploaded = true
      return json({ ...artifact, version: '1.6.0', created: true }, 201)
    }
    if (url === '/v1/artifact') {
      return json({
        artifacts: uploaded ? [{ ...artifact, version: '1.6.0', sha256: 'b'.repeat(64) }, artifact] : [artifact],
      })
    }
    // Before the generic POST below, or the card would get a DeployAccepted body.
    if (url.endsWith('/deploy/precheck') && init?.method === 'POST') {
      return json({
        device_id: 'x',
        target: 'esp32c6',
        version: '1.5.0',
        from_version: '1.4.2',
        sha256: 'a'.repeat(64),
        size_bytes: 230_000,
        artifact_partition_layout: 'ab-4m-v1',
        device_partition_layout: 'ab-4m-v1',
        ota_slot_size: 1_966_080,
        power_class: 'always_on',
        expected_wake_interval_s: null,
        device_online: true,
        confirm_timeout_s: 300,
        deployable: true,
        refusals: [],
        warnings: [],
      })
    }
    if (url.startsWith('/v1/devices/') && init?.method === 'POST') {
      return json({ cmd_id: 'c', device_id: 'x', version: '1.5.0', apply: 'auto', reused: false })
    }
    if (url === '/v1/partition-profiles') return json({ profiles: [builtinProfile] })
    if (url.startsWith('/v1/devices/') && url.includes('/logs')) {
      const id = url.split('/')[3]!
      return json({ device_id: id, lines: logs()[id] ?? [] })
    }
    if (url === '/v1/devices') {
      return json({ devices: typeof devices === 'function' ? devices() : devices, arrivals: [] })
    }
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
    // The pre-check card; selection happens on a real send (R2b-fe-8).
    await userEvent.click(await within(row).findByRole('button', { name: 'Send' }))
    await waitFor(() =>
      expect(screen.getByTestId('strip-board')).toHaveTextContent('b26a938324ab'),
    )
  })

  it('offers an uploaded build in the Deploy select with no remount (R2b-fe-7)', async () => {
    mockApi([device()])
    renderDashboard()
    const select = (await screen.findByLabelText('Version for a4cf12b3de90')) as HTMLSelectElement
    expect(select).toHaveValue('1.5.0')

    const bytes = new Uint8Array(512)
    bytes[0] = 0xe9
    new DataView(bytes.buffer).setUint16(12, 13, true)
    await userEvent.upload(
      screen.getByLabelText(/Build \(\.bin\)/),
      new File([bytes], 'app.bin'),
    )
    await waitFor(() => expect(screen.getByLabelText('Chip target')).toHaveValue('esp32c6'))
    await userEvent.type(screen.getByLabelText('Version'), '1.6.0')
    await userEvent.click(screen.getByRole('button', { name: 'Upload' }))

    expect(await screen.findByTestId('upload-result')).toHaveTextContent('Uploaded esp32c6 1.6.0')
    await waitFor(() => expect(select).toHaveValue('1.6.0'))
    expect(Array.from(select.options).map((o) => o.value)).toEqual(['1.6.0', '1.5.0'])
    // The very same element: the row was updated in place, not remounted.
    expect(screen.getByLabelText('Version for a4cf12b3de90')).toBe(select)
  })

  it('gives each finished update the status strip’s UI and API versions (R2b-fe-10)', async () => {
    mockApi([
      device({
        fw_version: '1.5.0',
        deploy: {
          cmd_id: 'cmd-a',
          state: 'confirmed',
          at: '2026-09-10T11:59:50Z',
          is_terminal: true,
          artifact_version: '1.5.0',
          from_version: '1.4.2',
          pct: null,
          detail: null,
        },
      }),
    ])
    renderDashboard()
    const card = await screen.findByTestId('deploy-result')
    expect(card).toHaveAttribute('data-outcome', 'good')
    expect(within(card).getByTestId('deploy-result-rows')).toHaveTextContent(
      'UI / APIUI 0.4.2 · API 0.4.2',
    )
  })

  it('opens exactly one event stream', async () => {
    mockApi([device()])
    const { sources } = renderDashboard()
    await screen.findByTestId('strip-board')
    expect(sources).toHaveBeenCalledTimes(1)
  })

  it('renders the partition profiles and re-reads them on a device.announce frame (R3-fe-1)', async () => {
    const fetchSpy = mockApi([device()])
    const { sources } = renderDashboard()
    const section = await screen.findByTestId('profiles')
    await waitFor(() => expect(within(section).getAllByTestId('profile-row')).toHaveLength(1))
    const reads = () =>
      fetchSpy.mock.calls.filter(([input]) => String(input) === '/v1/partition-profiles').length
    expect(reads()).toBe(1)

    const source = sources.mock.results[0]!.value as EventSourceLike
    source.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ v: 1, type: 'device.announce', device_id: 'a4cf12b3de90' }),
      }),
    )
    await waitFor(() => expect(reads()).toBe(2), { timeout: 2000 })
  })

  describe('device log (R4-fe-2)', () => {
    const logLine = (over: Record<string, unknown>) => ({
      id: 1,
      logged_at: '2026-09-10T11:59:00Z',
      received_at: '2026-09-10T11:59:00Z',
      level: 'I',
      text: 'hello',
      dropped: 0,
      ...over,
    })
    const logReads = (spy: ReturnType<typeof mockApi>, id?: string) =>
      spy.mock.calls
        .map(([input]) => String(input))
        .filter((u) => u.includes('/logs') && (id === undefined || u.includes(id)))

    it('follows the implied board of a one-board fleet', async () => {
      const spy = mockApi([device()])
      renderDashboard()
      const section = await screen.findByTestId('device-log')
      expect(within(section).getByRole('heading', { name: 'Device log' })).toBeInTheDocument()
      await waitFor(() => expect(logReads(spy)).toEqual(['/v1/devices/a4cf12b3de90/logs?limit=200']))
    })

    it('asks for a pick with two boards, then reads the picked one', async () => {
      const B = 'b26a938324ab'
      const spy = mockApi([device(), device({ device_id: B })], () => ({
        [B]: [logLine({ text: 'from the second board' })],
      }))
      renderDashboard()
      expect(await screen.findByTestId('device-log-none')).toBeInTheDocument()
      expect(logReads(spy)).toEqual([])

      const row = (await screen.findAllByTestId('device-row'))[1]!
      await userEvent.click(within(row).getByRole('button', { name: B }))
      expect(await screen.findByText('from the second board')).toBeInTheDocument()
      expect(logReads(spy)).toEqual([`/v1/devices/${B}/logs?limit=200`])
    })

    it('re-reads on the board’s last_seen, not on a frame alone', async () => {
      let seen = '2026-09-10T11:59:30Z'
      let lines: unknown[] = [logLine({ id: 1, text: 'first' })]
      const spy = mockApi(
        () => [device({ last_seen: seen })],
        () => ({ a4cf12b3de90: lines }),
      )
      const { sources } = renderDashboard()
      await screen.findByText('first')
      expect(logReads(spy)).toHaveLength(1)

      const source = sources.mock.results[0]!.value as EventSourceLike
      const frame = () =>
        source.onmessage?.(
          new MessageEvent('message', {
            data: JSON.stringify({ v: 1, type: 'device.seen', device_id: 'a4cf12b3de90' }),
          }),
        )

      // A frame with an unchanged last_seen re-reads the fleet but not the log.
      frame()
      await waitFor(
        () => expect(spy.mock.calls.filter(([i]) => String(i) === '/v1/devices').length).toBeGreaterThan(1),
        { timeout: 2000 },
      )
      expect(logReads(spy)).toHaveLength(1)

      seen = '2026-09-10T11:59:55Z'
      lines = [logLine({ id: 2, text: 'second' }), ...lines]
      frame()
      expect(await screen.findByText('second', {}, { timeout: 2000 })).toBeInTheDocument()
      expect(logReads(spy)).toHaveLength(2)
    })
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

  describe('4-tab console navigation & uploaded builds catalog', () => {
    it('lists uploaded artifacts in the Uploaded builds catalog and switches tabs via CTAs', async () => {
      mockApi([])
      renderDashboard()

      expect(screen.getByTestId('tab-fleet')).toHaveAttribute('aria-pressed', 'true')
      expect(await screen.findByTestId('empty-onboarding')).toBeInTheDocument()

      await userEvent.click(screen.getByRole('button', { name: /upload firmware \.bin >/i }))
      expect(screen.getByTestId('tab-builds')).toHaveAttribute('aria-pressed', 'true')
      expect(screen.getByTestId('panel-builds')).toHaveClass('active')

      const catalog = screen.getByTestId('uploaded-builds')
      const rows = await within(catalog).findAllByTestId('uploaded-build-row')
      expect(rows).toHaveLength(1)
      expect(rows[0]).toHaveTextContent('esp32c6')
      expect(rows[0]).toHaveTextContent('1.5.0')

      await userEvent.click(within(catalog).getByRole('button', { name: /deploy in fleet >/i }))
      expect(screen.getByTestId('tab-fleet')).toHaveAttribute('aria-pressed', 'true')

      await userEvent.click(screen.getByRole('button', { name: /flash or enroll a board >/i }))
      expect(screen.getByTestId('tab-onboard')).toHaveAttribute('aria-pressed', 'true')
      expect(screen.getByTestId('flash-stepper')).toBeInTheDocument()
    })
  })
})
