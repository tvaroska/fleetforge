// End-to-end FlashBoard serial flows: one-button recovery (`S0-fe-6`), pre-flight card
// (`R2b-fe-2`), write failure / success result card (`R2b-fe-3`), server+console baseline
// tracking (`R2b-fe-5`), and multi-network `ff_cfg` rows (`R2b-fe-12`).

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DeviceSummary } from './api'
import type { ConsoleFactory } from './boardConsole'
import { FlashBoard } from './FlashBoard'
import type { ChipInfo } from './flasher'
import {
  CONFIG_OFFSET,
  FakeFlasher,
  NVS_OFFSET,
  PASSPHRASE,
  PLAINTEXT,
  PLAINTEXT_2,
  RECOVERED_BOOT,
  SPENT_TOKEN_BOOT,
  SSID,
  TOKEN_ID,
  TOKEN_ID_2,
  chipInfo,
  defaultRoutes,
  fakeConsoleSessions,
  flashWith,
  json,
  manifest,
  mockFetch,
  setupWebSerialJsdom,
  type Routes,
} from './flashTestHelpers'

beforeEach(() => {
  setupWebSerialJsdom()
})

describe('FlashBoard — recovery is a button (S0-fe-6)', () => {
  it('a spent token is fixed by pressing a button, not by following an instruction', async () => {
    const user = userEvent.setup()
    let mints = 0
    const { calls } = mockFetch(
      await defaultRoutes({
        'POST /v1/enrollment-tokens': () => {
          mints += 1
          const [id, token] = mints === 1 ? [TOKEN_ID, PLAINTEXT] : [TOKEN_ID_2, PLAINTEXT_2]
          return json({
            id,
            token,
            group_id: null,
            expires_at: '2026-09-12T00:00:00Z',
            created_at: '2026-09-11T00:00:00Z',
          })
        },
      }),
    )

    const flashers: FakeFlasher[] = []
    const createFlasher = async () => {
      const flasher = new FakeFlasher(chipInfo())
      flashers.push(flasher)
      return flasher
    }
    const { factory: createConsole, state: console_ } = fakeConsoleSessions([
      SPENT_TOKEN_BOOT,
      RECOVERED_BOOT,
    ])

    render(
      <FlashBoard
        onSessionExpired={vi.fn()}
        createFlasher={createFlasher}
        createConsole={createConsole}
      />,
    )
    await user.click(screen.getByRole('button', { name: /select port and detect/i }))
    await screen.findByTestId('chip-info')
    await user.type(screen.getByLabelText(/ssid/i), SSID)
    await user.type(screen.getByLabelText(/passphrase/i), PASSPHRASE)
    await user.click(screen.getByRole('button', { name: /flash this board/i }))

    const fault = await screen.findByTestId('console-fault')
    await waitFor(() => expect(fault).toHaveTextContent(/single-use/))

    const button = await screen.findByRole('button', { name: /re-flash the board/i })
    await user.click(button)
    await waitFor(() => expect(flashers).toHaveLength(2))
    expect(console_.closed).toBe(1)

    await waitFor(() => expect(flashers[1]!.writes).toHaveLength(1))
    expect(calls.filter((c) => c === 'POST /v1/enrollment-tokens')).toHaveLength(2)
    const recovery = flashers[1]!.writes[0]!
    expect(recovery.map((part) => part.label).filter((label) => /nvs/.test(label))).toEqual([])
    expect(recovery.map((part) => part.address)).not.toContain(NVS_OFFSET)
    expect(recovery.map((part) => part.address)).not.toContain(0x11000)

    const config = flashers[1]!.writes[0]!.find((part) => part.address === CONFIG_OFFSET)!
    const payload = new TextDecoder().decode(config.data)
    expect(payload).toContain(PLAINTEXT_2)
    expect(payload).not.toContain(PLAINTEXT)

    await waitFor(() => expect(console_.opened).toBe(2))
    expect(console_.acquires).toEqual(['granted', 'granted'])
    const milestones = screen.getByTestId('boot-milestones')
    await waitFor(() => {
      expect(milestones.querySelectorAll('[data-state="done"]')).toHaveLength(5)
    })
    expect(await screen.findByTestId('console-online')).toBeInTheDocument()
    expect(screen.queryByTestId('console-fault')).not.toBeInTheDocument()

    const body = document.body.textContent ?? ''
    expect(body).not.toContain(PLAINTEXT)
    expect(body).not.toContain(PLAINTEXT_2)
    expect(body).not.toContain(PASSPHRASE)
  })

  it('says a fresh click is needed when the activation window closed, and works on the next one', async () => {
    const user = userEvent.setup()
    mockFetch(await defaultRoutes())
    let attempts = 0
    const createFlasher = async () => {
      attempts += 1
      if (attempts === 1) {
        throw new DOMException(
          "Failed to execute 'requestPort' on 'Serial': Must be handling a user gesture to " +
            'show a permission request.',
          'SecurityError',
        )
      }
      return new FakeFlasher(chipInfo())
    }
    render(<FlashBoard onSessionExpired={vi.fn()} createFlasher={createFlasher} />)

    await user.click(screen.getByRole('button', { name: /select port and detect/i }))
    expect(await screen.findByText(/needs a fresh click/i)).toBeInTheDocument()
    expect(screen.queryByText(/HTTPS or localhost/i)).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /select port and detect/i }))
    await screen.findByTestId('chip-info')
  })
})

describe('FlashBoard — pre-flight card (R2b-fe-2)', () => {
  const NOW = Date.parse('2026-09-10T12:00:00Z')
  const row = (over: Partial<DeviceSummary> = {}): DeviceSummary => ({
    device_id: 'a4cf12b3de90',
    name: null,
    group_id: null,
    platform_type: 'esp32',
    fw_version: '1.4.2',
    agent_version: '0.4.5',
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
    broker_provisioned_at: null,
    online: true,
    deploy: null,
    ...over,
  })
  const fleetOf = (devices: DeviceSummary[] | null) => ({
    devices,
    arrivals: [],
    error: null,
    now: NOW,
  })

  async function detect(
    devices: DeviceSummary[] | null | undefined,
    info: ChipInfo = chipInfo(),
    flasher = new FakeFlasher(info),
  ) {
    const mocked = mockFetch(await defaultRoutes())
    render(
      <FlashBoard
        onSessionExpired={vi.fn()}
        createFlasher={async () => flasher}
        fleet={devices === undefined ? undefined : fleetOf(devices)}
      />,
    )
    await userEvent.click(screen.getByRole('button', { name: /select port and detect/i }))
    await screen.findByTestId('chip-info')
    return { ...mocked, flasher }
  }

  it('shows the known-board card before the button and mints nothing on detect', async () => {
    const { calls } = await detect([row()])
    const card = screen.getByTestId('preflight-card')
    expect(card).toHaveAttribute('data-kind', 'known')
    expect(card).toHaveTextContent(
      'Re-flashing issues a new token and re-enrols it; its current baseline ends.',
    )
    expect(card).toHaveTextContent('fw 1.4.2')
    expect(card).toHaveTextContent('online')
    const button = screen.getByRole('button', { name: 'Re-flash and re-enrol this board' })
    expect(card.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(calls).not.toContain('POST /v1/enrollment-tokens')
  })

  it('says new board for an id that is not on the fleet', async () => {
    await detect([row({ device_id: 'b26a938324ab' })])
    const card = screen.getByTestId('preflight-card')
    expect(card).toHaveAttribute('data-kind', 'new')
    expect(card).toHaveTextContent(/new board/i)
    expect(screen.getByRole('button', { name: 'Flash this board' })).toBeInTheDocument()
  })

  it('does not block Flash while the fleet is loading', async () => {
    await detect(null)
    expect(screen.getByTestId('preflight-card')).toHaveAttribute('data-kind', 'checking')
    await userEvent.type(screen.getByLabelText(/ssid/i), SSID)
    await userEvent.type(screen.getByLabelText(/passphrase/i), PASSPHRASE)
    expect(screen.getByRole('button', { name: 'Flash this board' })).toBeEnabled()
  })

  it('says unknown-id when no MAC was read', async () => {
    await detect([row()], chipInfo({ macAddress: null }))
    expect(screen.getByTestId('preflight-card')).toHaveAttribute('data-kind', 'unknown-id')
  })

  it('warns about an update in progress and a layout change', async () => {
    await detect([
      row({
        partition_layout: 'other-layout',
        deploy: {
          cmd_id: 'c',
          state: 'downloading',
          at: '2026-09-10T11:59:00Z',
          is_terminal: false,
          artifact_version: '1.5.0',
          from_version: '1.4.2',
          pct: null,
          detail: null,
          steps: [],
          confirm_timeout_s: 300,
        },
      }),
    ])
    const card = screen.getByTestId('preflight-card')
    expect(card).toHaveTextContent(/update is in progress/i)
    expect(card).toHaveTextContent('Partition layout changes: other-layout → ab-4m-v1.')
  })

  it('is gone once the flash has finished', async () => {
    const flasher = new FakeFlasher(chipInfo())
    await detect([row()], chipInfo(), flasher)
    await userEvent.type(screen.getByLabelText(/ssid/i), SSID)
    await userEvent.type(screen.getByLabelText(/passphrase/i), PASSPHRASE)
    await userEvent.click(screen.getByRole('button', { name: /re-flash and re-enrol this board/i }))
    await screen.findByRole('heading', { name: 'Flashed' })
    expect(screen.queryByTestId('preflight-card')).toBeNull()
  })

  it('renders no card without the fleet prop', async () => {
    await detect(undefined)
    expect(screen.queryByTestId('preflight-card')).toBeNull()
  })
})

describe('FlashBoard — result card (R2b-fe-3)', () => {
  function failingFactory(shouldFail: () => boolean, message = 'No serial data received.') {
    const bauds: number[] = []
    const createFlasher = async ({ baudRate }: { baudRate: number }) => {
      bauds.push(baudRate)
      const fail = shouldFail()
      return new FakeFlasher(
        chipInfo(),
        fail
          ? () => {
              throw new Error(message)
            }
          : null,
      )
    }
    return { bauds, createFlasher }
  }

  async function connectAndFlash(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByRole('button', { name: /select port and detect/i }))
    await screen.findByTestId('chip-info')
    await user.type(screen.getByLabelText(/ssid/i), SSID)
    await user.type(screen.getByLabelText(/passphrase/i), PASSPHRASE)
    await user.click(screen.getByRole('button', { name: /flash this board/i }))
  }

  it('a failed write at 921600 says cable and port first, never the chip, never BOOT', async () => {
    const user = userEvent.setup()
    const { calls } = mockFetch(await defaultRoutes())
    const { createFlasher } = failingFactory(() => true)
    render(<FlashBoard onSessionExpired={vi.fn()} createFlasher={createFlasher} />)
    await connectAndFlash(user)

    const card = await screen.findByTestId('result-card')
    expect(card).toHaveAttribute('data-outcome', 'flash-failed')
    expect(card).toHaveAttribute('role', 'alert')
    expect(card).toHaveTextContent(/stopped answering/i)
    expect(card).toHaveTextContent(/cable/i)
    expect(card).toHaveTextContent(/port/i)
    expect(card).toHaveTextContent(/retry at 115200/i)
    expect(card).not.toHaveTextContent(/flash chip/i)
    expect(card).not.toHaveTextContent(/hold boot/i)
    expect(card).toHaveTextContent(/No serial data received/)
    expect(card).toHaveTextContent(/token was revoked/i)
    expect(within(card).getByRole('button', { name: 'Try the flash again' })).toBeEnabled()
    expect(
      calls.filter((c) => c === `POST /v1/enrollment-tokens/${TOKEN_ID}/revoke`),
    ).toHaveLength(1)
    expect(card.textContent ?? '').not.toContain(PLAINTEXT)
  })

  it('escalates: try again, then retry at 115200, then suspect the flash chip', async () => {
    const user = userEvent.setup()
    const { calls } = mockFetch(await defaultRoutes())
    const { bauds, createFlasher } = failingFactory(() => true)
    render(<FlashBoard onSessionExpired={vi.fn()} createFlasher={createFlasher} />)
    await connectAndFlash(user)

    let card = await screen.findByTestId('result-card')
    await user.click(within(card).getByRole('button', { name: 'Try the flash again' }))

    await waitFor(() => expect(bauds).toEqual([921600, 921600]))
    card = await screen.findByTestId('result-card')
    const retry = await within(card).findByRole('button', { name: 'Retry at 115200' })
    expect(card).toHaveTextContent(/failed again/i)
    expect(card).not.toHaveTextContent(/flash chip/i)

    await user.click(retry)
    await waitFor(() => expect(bauds).toEqual([921600, 921600, 115200]))
    expect(screen.getByLabelText(/baud rate/i)).toHaveValue('115200')

    await waitFor(() =>
      expect(screen.getByTestId('result-card')).toHaveTextContent(/flash chip may be defective/i),
    )
    expect(screen.getByTestId('result-card')).toHaveTextContent(/failed 3 times/)
    expect(
      calls.filter((c) => c === `POST /v1/enrollment-tokens/${TOKEN_ID}/revoke`),
    ).toHaveLength(3)
  })

  it('a successful write clears the card and resets the count', async () => {
    const user = userEvent.setup()
    mockFetch(await defaultRoutes())
    let failNext = true
    const { createFlasher } = failingFactory(() => {
      const fail = failNext
      failNext = false
      return fail
    })
    const { factory: createConsole } = fakeConsoleSessions([[], []])
    render(
      <FlashBoard
        onSessionExpired={vi.fn()}
        createFlasher={createFlasher}
        createConsole={createConsole}
      />,
    )
    await connectAndFlash(user)

    const card = await screen.findByTestId('result-card')
    await user.click(within(card).getByRole('button', { name: 'Try the flash again' }))
    await screen.findByRole('heading', { name: 'Flashed' })
    expect(screen.queryByTestId('result-card')).toBeNull()

    failNext = true
    await user.click(screen.getByRole('button', { name: /flash another board/i }))
    await user.click(screen.getByRole('button', { name: /select port and detect/i }))
    await screen.findByTestId('chip-info')
    await user.click(screen.getByRole('button', { name: /flash this board/i }))
    const again = await screen.findByTestId('result-card')
    expect(again).toHaveAttribute('data-outcome', 'flash-failed')
    expect(within(again).getByRole('button', { name: 'Try the flash again' })).toBeInTheDocument()
    expect(again).not.toHaveTextContent(/failed again/i)
  })

  it('keeps the plain alert paragraph for a failure before the write', async () => {
    mockFetch(await defaultRoutes())
    await flashWith(new FakeFlasher(chipInfo({ chipName: 'ESP32-S2' })))
    expect(await screen.findByRole('alert')).toHaveTextContent(/no agent bundle for ESP32-S2/i)
    expect(screen.queryByTestId('result-card')).toBeNull()
  })

  it('ends a good flash in one success card with every fact, and no secret', async () => {
    const user = userEvent.setup()
    mockFetch(await defaultRoutes())
    const { factory: createConsole } = fakeConsoleSessions([RECOVERED_BOOT])
    const device: DeviceSummary = {
      device_id: 'a4cf12b3de90',
      name: null,
      group_id: null,
      platform_type: 'esp32',
      fw_version: '0.3.0',
      agent_version: '0.3.0',
      link_type: 'wifi',
      ssid: null,
      known_networks: null,
      power_class: 'always_on',
      expected_wake_interval_s: null,
      parent_device_id: null,
      partition_layout: 'ab-4m-v1',
      ota_slot_size: 1966080,
      capabilities: ['ota'],
      last_seen: '2026-09-11T08:15:30Z',
      enrolled_at: '2026-09-11T08:15:20Z',
      broker_provisioned_at: null,
      online: true,
      deploy: null,
    }
    render(
      <FlashBoard
        onSessionExpired={vi.fn()}
        createFlasher={async () => new FakeFlasher(chipInfo())}
        createConsole={createConsole}
        fleet={{ devices: [device], arrivals: [], error: null, now: Date.now() }}
        versions={{ ui: '0.4.2', uiTitle: '', api: '0.4.2', differ: false }}
      />,
    )
    await user.click(screen.getByRole('button', { name: /select port and detect/i }))
    await screen.findByTestId('chip-info')
    await user.type(screen.getByLabelText(/ssid/i), SSID)
    await user.type(screen.getByLabelText(/passphrase/i), PASSPHRASE)
    await user.click(screen.getByRole('button', { name: /re-flash and re-enrol this board/i }))

    const card = await screen.findByTestId('result-card')
    await waitFor(() => expect(card).toHaveAttribute('data-outcome', 'success'))
    const rows = within(card).getByTestId('result-rows')
    const row = (label: string) =>
      within(rows).getByText(label).nextElementSibling?.textContent ?? ''
    expect(row('Device id')).toBe('a4cf12b3de90')
    expect(row('Firmware')).toBe('0.3.0')
    expect(row('Partition layout')).toBe('ab-4m-v1')
    expect(row('Link')).toBe(`Wi-Fi ${SSID} · ip 192.168.1.40`)
    expect(row('Clock source')).toBe('NTP (pool.ntp.org)')
    expect(row('Enrolled')).toBe('yes')
    expect(row('On the fleet')).toBe('yes · server: online')
    expect(row('UI / API')).toBe('UI 0.4.2 · API 0.4.2')
    expect(within(card).getByTestId('console-online')).toBeInTheDocument()
    expect(screen.getAllByTestId('result-card')).toHaveLength(1)

    const body = document.body.textContent ?? ''
    expect(body).not.toContain(PLAINTEXT)
    expect(body).not.toContain(PASSPHRASE)
  })

  it('names the board from the success card: one PATCH with only the name, then a re-read', async () => {
    const user = userEvent.setup()
    const device: DeviceSummary = {
      device_id: 'a4cf12b3de90',
      name: null,
      group_id: null,
      platform_type: 'esp32',
      fw_version: '0.3.0',
      agent_version: '0.3.0',
      link_type: 'wifi',
      ssid: null,
      known_networks: null,
      power_class: 'always_on',
      expected_wake_interval_s: null,
      parent_device_id: null,
      partition_layout: 'ab-4m-v1',
      ota_slot_size: 1966080,
      capabilities: ['ota'],
      last_seen: '2026-09-11T08:15:30Z',
      enrolled_at: '2026-09-11T08:15:20Z',
      broker_provisioned_at: null,
      online: true,
      deploy: null,
    }
    const { calls, spy } = mockFetch(
      await defaultRoutes({
        'PATCH /v1/devices/a4cf12b3de90': () => json({ ...device, name: 'coop door' }),
      }),
    )
    const refresh = vi.fn()
    const { factory: createConsole } = fakeConsoleSessions([RECOVERED_BOOT])
    render(
      <FlashBoard
        onSessionExpired={vi.fn()}
        createFlasher={async () => new FakeFlasher(chipInfo())}
        createConsole={createConsole}
        fleet={{ devices: [device], arrivals: [], error: null, now: Date.now(), refresh }}
      />,
    )
    await user.click(screen.getByRole('button', { name: /select port and detect/i }))
    await screen.findByTestId('chip-info')
    await user.type(screen.getByLabelText(/ssid/i), SSID)
    await user.type(screen.getByLabelText(/passphrase/i), PASSPHRASE)
    await user.click(screen.getByRole('button', { name: /re-flash and re-enrol this board/i }))

    const card = await screen.findByTestId('result-card')
    await waitFor(() => expect(card).toHaveAttribute('data-outcome', 'success'))
    await user.type(within(card).getByLabelText('Board name'), 'coop door')
    await user.click(within(card).getByRole('button', { name: 'Save name' }))

    expect(await within(card).findByTestId('name-board-message')).toHaveTextContent(
      'Saved. The fleet table shows “coop door”',
    )
    expect(calls.filter((c) => c.startsWith('PATCH '))).toEqual(['PATCH /v1/devices/a4cf12b3de90'])
    expect(refresh).toHaveBeenCalledTimes(1)
    const patches = spy.mock.calls.filter(([, init]) => init?.method === 'PATCH')
    expect(patches.map(([, init]) => init?.body)).toEqual(['{"name":"coop door"}'])
  })
})

describe('FlashBoard — the console and the server together (R2b-fe-5)', () => {
  const E0 = '2026-10-04T10:00:00Z'
  const E1 = '2026-10-04T12:00:00Z'
  const NO_BOARD =
    'No board is available to watch. Plug it back in, then use “Watch a board” to pick the port.'
  const known: DeviceSummary = {
    device_id: 'a4cf12b3de90',
    name: null,
    group_id: null,
    platform_type: 'esp32',
    fw_version: '0.3.0',
    agent_version: '0.3.0',
    link_type: 'wifi',
    ssid: null,
    known_networks: null,
    power_class: 'always_on',
    expected_wake_interval_s: null,
    parent_device_id: null,
    partition_layout: 'ab-4m-v1',
    ota_slot_size: 1966080,
    capabilities: ['ota'],
    last_seen: '2026-10-04T11:59:00Z',
    enrolled_at: E0,
    broker_provisioned_at: '2026-10-04T10:00:01Z',
    online: true,
    deploy: null,
  }

  it('re-flash of a known online board: success only after the new enrolment, from the server', async () => {
    const user = userEvent.setup()
    mockFetch(await defaultRoutes())
    let acquires = 0
    const createConsole: ConsoleFactory = async () => {
      acquires += 1
      throw new Error(NO_BOARD)
    }
    const page = (devices: DeviceSummary[]) => (
      <FlashBoard
        onSessionExpired={vi.fn()}
        createFlasher={async () => new FakeFlasher(chipInfo())}
        createConsole={createConsole}
        fleet={{ devices, arrivals: [], error: null, now: Date.now() }}
      />
    )
    const { rerender } = render(page([known]))
    await user.click(screen.getByRole('button', { name: /select port and detect/i }))
    await screen.findByTestId('chip-info')
    await user.type(screen.getByLabelText(/ssid/i), SSID)
    await user.type(screen.getByLabelText(/passphrase/i), PASSPHRASE)
    await user.click(screen.getByRole('button', { name: /re-flash and re-enrol this board/i }))

    const view = await screen.findByTestId('console-server-view')
    expect(view).toHaveTextContent('Watching the server for a4cf12b3de90')
    expect(screen.queryByTestId('result-card')).toBeNull()
    expect(document.querySelector('[data-source="server"]')).toBeNull()

    const reEnrolled: DeviceSummary = {
      ...known,
      enrolled_at: E1,
      broker_provisioned_at: '2026-10-04T12:00:01Z',
      last_seen: '2026-10-04T12:00:09Z',
    }
    rerender(page([reEnrolled]))
    const card = screen.getByTestId('result-card')
    expect(card).toHaveAttribute('data-outcome', 'success')
    expect(within(card).getByTestId('console-online')).toBeInTheDocument()
    expect(within(card).getByText('On the fleet').nextElementSibling).toHaveTextContent(
      'the console did not see it',
    )

    await user.click(screen.getByRole('button', { name: 'Watch a board' }))
    await waitFor(() => expect(acquires).toBe(2))
    await waitFor(() =>
      expect(screen.getByTestId('result-card')).toHaveAttribute('data-outcome', 'success'),
    )

    await user.click(screen.getByRole('button', { name: /flash another board/i }))
    expect(screen.queryByTestId('console-server-view')).toBeNull()
  })
})

describe('FlashBoard — known networks (R2b-fe-12)', () => {
  const SHED_PSK = 's3cr3t-shed-passphrase'
  const CAR_PSK = 's3cr3t-car-passphrase'

  function row(n: number) {
    return within(screen.getByRole('group', { name: `Network ${n}` }))
  }

  async function detect(routes?: Routes) {
    const fetched = mockFetch(routes ?? (await defaultRoutes()))
    const flasher = new FakeFlasher(chipInfo())
    render(<FlashBoard onSessionExpired={vi.fn()} createFlasher={async () => flasher} />)
    await userEvent.click(screen.getByRole('button', { name: /select port and detect/i }))
    await screen.findByTestId('chip-info')
    return { flasher, fetched }
  }

  async function fillNetworks(extra: Array<{ ssid: string; psk?: string }>) {
    await userEvent.type(row(1).getByLabelText('SSID'), SSID)
    await userEvent.type(row(1).getByLabelText('Passphrase'), PASSPHRASE)
    for (const [i, net] of extra.entries()) {
      await userEvent.click(screen.getByRole('button', { name: 'Add another network' }))
      await userEvent.type(row(i + 2).getByLabelText('SSID'), net.ssid)
      if (net.psk) await userEvent.type(row(i + 2).getByLabelText('Passphrase'), net.psk)
    }
  }

  function payloadOf(flasher: FakeFlasher): Record<string, unknown> {
    const config = flasher.writes[0]!.find((part) => part.address === CONFIG_OFFSET)!
    const view = new DataView(config.data.buffer, config.data.byteOffset, config.data.byteLength)
    const length = view.getUint32(8, true)
    return JSON.parse(new TextDecoder().decode(config.data.slice(16, 16 + length)))
  }

  it('writes three networks into the blob, in form order, an open one without a psk', async () => {
    const { flasher } = await detect()
    await fillNetworks([{ ssid: 'shed', psk: SHED_PSK }, { ssid: 'bench' }])
    await userEvent.click(screen.getByRole('button', { name: /flash this board/i }))
    await waitFor(() => expect(flasher.writes).toHaveLength(1))

    const payload = payloadOf(flasher)
    expect(payload.ssid).toBe(SSID)
    expect(payload.psk).toBe(PASSPHRASE)
    expect(payload.token).toBe(PLAINTEXT)
    expect(payload.nets).toEqual([{ ssid: 'shed', psk: SHED_PSK }, { ssid: 'bench' }])
  })

  it('stops at four networks and says so', async () => {
    await detect()
    const add = screen.getByRole('button', { name: 'Add another network' })
    expect(screen.queryByText(/at most 4 networks\./)).toBeNull()
    for (let i = 0; i < 3; i += 1) await userEvent.click(add)
    expect(screen.getByRole('group', { name: 'Network 4' })).toBeInTheDocument()
    expect(add).toBeDisabled()
    expect(screen.getByText(/A board knows at most 4 networks/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove network 1' })).toBeNull()
  })

  it('removing a row keeps every typed value attached to its own network', async () => {
    await detect()
    await fillNetworks([
      { ssid: 'shed', psk: SHED_PSK },
      { ssid: 'car', psk: CAR_PSK },
    ])
    await userEvent.click(screen.getByRole('button', { name: 'Remove network 2' }))
    expect(screen.queryByRole('group', { name: 'Network 3' })).toBeNull()
    expect(row(2).getByLabelText('SSID')).toHaveValue('car')
    expect(row(2).getByLabelText('Passphrase')).toHaveValue(CAR_PSK)
    expect(row(1).getByLabelText('SSID')).toHaveValue(SSID)
  })

  it('refuses a duplicate SSID by its network number, without a passphrase, before any mint', async () => {
    const { fetched } = await detect()
    await fillNetworks([
      { ssid: 'shed', psk: SHED_PSK },
      { ssid: 'shed', psk: CAR_PSK },
    ])
    const error = screen.getByTestId('config-error')
    expect(error).toHaveTextContent(/network 3/)
    expect(error).toHaveTextContent(/repeats/)
    for (const secret of [PASSPHRASE, SHED_PSK, CAR_PSK]) {
      expect(error.textContent).not.toContain(secret)
    }
    expect(screen.getByRole('button', { name: /flash this board/i })).toBeDisabled()
    expect(fetched.calls).not.toContain('POST /v1/enrollment-tokens')
  })

  it('refuses a blank extra row rather than silently dropping it', async () => {
    await detect()
    await userEvent.type(row(1).getByLabelText('SSID'), SSID)
    await userEvent.click(screen.getByRole('button', { name: 'Add another network' }))
    expect(screen.getByTestId('config-error')).toHaveTextContent(/network 2 needs an SSID/)
  })

  it('marks each network for the password manager, one form per row, and Enter does nothing', async () => {
    const { flasher, fetched } = await detect()
    await fillNetworks([{ ssid: 'shed', psk: SHED_PSK }, { ssid: 'bench' }])

    const forms = new Set<Element>()
    for (const n of [1, 2, 3]) {
      const ssid = row(n).getByLabelText('SSID')
      const psk = row(n).getByLabelText('Passphrase')
      expect(ssid).toHaveAttribute('autocomplete', 'username')
      expect(psk).toHaveAttribute('type', 'password')
      expect(psk).toHaveAttribute('autocomplete', 'current-password')
      const form = ssid.closest('form')
      expect(form).not.toBeNull()
      expect(psk.closest('form')).toBe(form)
      forms.add(form!)
    }
    expect(forms.size).toBe(3)
    expect(document.querySelectorAll('input[autocomplete="off"]')).toHaveLength(0)

    const before = fetched.calls.length
    for (const form of forms) fireEvent.submit(form)
    expect(fetched.calls.length).toBe(before)
    expect(flasher.writes).toHaveLength(0)
    expect(row(2).getByLabelText('Passphrase')).toHaveValue(SHED_PSK)
  })

  it('stores nothing: no Storage write, no cookie, no passphrase on the page', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    const { flasher } = await detect()
    await fillNetworks([
      { ssid: 'shed', psk: SHED_PSK },
      { ssid: 'car', psk: CAR_PSK },
    ])
    await userEvent.click(screen.getByRole('button', { name: /flash this board/i }))
    await waitFor(() => expect(flasher.writes).toHaveLength(1))

    expect(setItem).not.toHaveBeenCalled()
    expect(window.localStorage.length).toBe(0)
    expect(window.sessionStorage.length).toBe(0)
    for (const secret of [PASSPHRASE, SHED_PSK, CAR_PSK]) {
      expect(document.cookie).not.toContain(secret)
      expect(document.body.textContent ?? '').not.toContain(secret)
      expect(screen.queryByTestId('flash-log')?.textContent ?? '').not.toContain(secret)
    }
  })

  it('writes no Wi-Fi field on Ethernet, whatever was typed before switching', async () => {
    const { flasher } = await detect()
    await fillNetworks([{ ssid: 'shed', psk: SHED_PSK }])
    await userEvent.click(screen.getByRole('radio', { name: 'Ethernet' }))
    await userEvent.click(screen.getByRole('button', { name: /flash this board/i }))
    await waitFor(() => expect(flasher.writes).toHaveLength(1))

    const payload = payloadOf(flasher)
    expect(payload.link).toBe('ethernet')
    expect('ssid' in payload).toBe(false)
    expect('psk' in payload).toBe(false)
    expect('nets' in payload).toBe(false)
  })

  it('warns, without blocking, that an older agent joins only network 1', async () => {
    const old = await manifest({ agent_version: '0.4.5' })
    await detect(await defaultRoutes({ 'GET /v1/agent/manifest': () => json(old) }))
    await userEvent.type(row(1).getByLabelText('SSID'), SSID)
    await screen.findByTestId('agent-targets')
    expect(screen.queryByTestId('old-agent-warning')).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: 'Add another network' }))
    await userEvent.type(row(2).getByLabelText('SSID'), 'shed')
    expect(screen.getByTestId('old-agent-warning')).toHaveTextContent(
      'This board gets agent 0.4.5, which joins only network 1. Networks 2-2 need agent 0.4.6 or later.',
    )
    expect(screen.getByRole('button', { name: /flash this board/i })).toBeEnabled()
  })

  it('does not warn when the build this board gets reads the list', async () => {
    const current = await manifest({ agent_version: '0.4.6' })
    await detect(await defaultRoutes({ 'GET /v1/agent/manifest': () => json(current) }))
    await screen.findByTestId('agent-targets')
    await fillNetworks([{ ssid: 'shed' }])
    expect(screen.queryByTestId('old-agent-warning')).toBeNull()
  })
})
