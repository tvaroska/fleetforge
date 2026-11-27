// Core FlashBoard unit tests: write plan offsets & NVS protection, pre-mint refusals,
// credential redaction & non-persistence, failure revocation, capability checks, and
// diagnostic bundle scrubbing.

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentBuildInfo } from './api'
import { planWrite, predictDeviceId } from './flash'
import { FlashBoard } from './FlashBoard'
import { BUILT_IN_PORT, checkChosenPort } from './flasher'
import {
  BROKER_URI,
  CONFIG_OFFSET,
  FakeFlasher,
  NVS_OFFSET,
  NVS_SIZE,
  PART_BYTES,
  PASSPHRASE,
  PLAINTEXT,
  SPENT_TOKEN_BOOT,
  SSID,
  TOKEN_ID,
  chipInfo,
  defaultRoutes,
  fakeConsoleSessions,
  flashWith,
  json,
  manifest,
  mockFetch,
  setupWebSerialJsdom,
} from './flashTestHelpers'

beforeEach(() => {
  setupWebSerialJsdom()
})

describe('FlashBoard — the write plan', () => {
  it('writes exactly the manifest offsets, ascending, with the 4 KB blob at the config offset', async () => {
    await mockFetch(await defaultRoutes())
    const flasher = new FakeFlasher(chipInfo())
    await flashWith(flasher)

    await waitFor(() => expect(flasher.writes).toHaveLength(1))
    const plan = flasher.writes[0]!
    expect(plan.map((part) => part.address)).toEqual([0x1000, 0x8000, CONFIG_OFFSET, 0x20000])
    expect(plan.map((part) => part.label)).toEqual([
      'bootloader',
      'partition-table',
      'ff_cfg',
      'app',
    ])
    const config = plan.find((part) => part.address === CONFIG_OFFSET)
    expect(config?.data.length).toBe(4096)
    expect(String.fromCharCode(...config!.data.slice(0, 4))).toBe('FFCF')

    expect(plan.map((part) => part.address)).not.toContain(NVS_OFFSET)
    expect(plan.map((part) => part.label).filter((label) => /nvs/.test(label))).toEqual([])
    expect(plan.map((part) => part.address)).not.toContain(0x11000)
    expect(flasher.finishes).toBe(1)
    expect(flasher.closes).toBeGreaterThan(0)
  })

  it('refuses a plan that would write into nvs, naming the offending part', async () => {
    const built = await manifest()
    const downloaded = new Map(Object.entries(PART_BYTES))
    const inNvs: AgentBuildInfo = {
      ...built.builds[0]!,
      config_partition: { label: 'ff_cfg', offset: NVS_OFFSET, size: 4096 },
    }
    expect(() => planWrite(inNvs, downloaded, new Uint8Array(4096))).toThrow(/'ff_cfg'/)
    expect(() => planWrite(inNvs, downloaded, new Uint8Array(4096))).toThrow(/nvs/)

    const justBefore: AgentBuildInfo = {
      ...built.builds[0]!,
      config_partition: { label: 'ff_cfg', offset: NVS_OFFSET - 0x1000, size: 4096 },
    }
    expect(() => planWrite(justBefore, downloaded, new Uint8Array(4096))).not.toThrow()
    const straddling: AgentBuildInfo = {
      ...built.builds[0]!,
      config_partition: { label: 'ff_cfg', offset: NVS_OFFSET + NVS_SIZE - 1, size: 4096 },
    }
    expect(() => planWrite(straddling, downloaded, new Uint8Array(4096))).toThrow(/nvs/)
  })
})

describe('FlashBoard — refusals, all before a token is minted', () => {
  it('refuses a chip with no bundle and names what is available', async () => {
    const { calls } = mockFetch(await defaultRoutes())
    const flasher = new FakeFlasher(chipInfo({ chipName: 'ESP32-S2' }))
    await flashWith(flasher)

    expect(await screen.findByRole('alert')).toHaveTextContent(/no agent bundle for ESP32-S2/i)
    expect(await screen.findByRole('alert')).toHaveTextContent(/esp32 \(ESP32\)/)
    expect(flasher.writes).toHaveLength(0)
    expect(calls).not.toContain('POST /v1/enrollment-tokens')
  })

  it('refuses a board whose flash is too small for the layout', async () => {
    const built = await manifest()
    built.builds[0]!.parts = built.builds[0]!.parts.map((part) =>
      part.name === 'app' ? { ...part, size: 3_000_000 } : part,
    )
    const { calls } = mockFetch(
      await defaultRoutes({ 'GET /v1/agent/manifest': () => json(built) }),
    )
    const flasher = new FakeFlasher(chipInfo({ flashSizeBytes: 2 * 1024 * 1024 }))
    await flashWith(flasher)

    expect(await screen.findByRole('alert')).toHaveTextContent(/2 MB of flash/i)
    expect(flasher.writes).toHaveLength(0)
    expect(calls).not.toContain('POST /v1/enrollment-tokens')
  })

  it('refuses a bundle with no config partition and says to rebuild it', async () => {
    const built = await manifest({ config_partition: null })
    const { calls } = mockFetch(
      await defaultRoutes({ 'GET /v1/agent/manifest': () => json(built) }),
    )
    const flasher = new FakeFlasher(chipInfo())
    await flashWith(flasher)

    expect(await screen.findByRole('alert')).toHaveTextContent(/no ff_cfg partition/i)
    expect(await screen.findByRole('alert')).toHaveTextContent(/just agent-build esp32/)
    expect(flasher.writes).toHaveLength(0)
    expect(calls).not.toContain('POST /v1/enrollment-tokens')
  })

  it('aborts on a sha256 mismatch without minting', async () => {
    const { calls } = mockFetch(
      await defaultRoutes({
        'GET /v1/agent/esp32/app': () => new Response(new Uint8Array(512).fill(0x00)),
        'GET /v1/agent/esp32/app?layout=ab-4m-v1': () =>
          new Response(new Uint8Array(512).fill(0x00)),
      }),
    )
    const flasher = new FakeFlasher(chipInfo())
    await flashWith(flasher)

    expect(await screen.findByRole('alert')).toHaveTextContent(/does not match its manifest sha256/i)
    expect(flasher.writes).toHaveLength(0)
    expect(calls).not.toContain('POST /v1/enrollment-tokens')
  })

  it('runs validateFfCfg before the mint — sleepy with no wake interval', async () => {
    const { calls } = mockFetch(await defaultRoutes())
    const flasher = new FakeFlasher(chipInfo())
    render(<FlashBoard onSessionExpired={vi.fn()} createFlasher={async () => flasher} />)
    await userEvent.click(screen.getByRole('button', { name: /select port and detect/i }))
    await screen.findByTestId('chip-info')
    await userEvent.type(screen.getByLabelText(/ssid/i), SSID)
    await userEvent.click(screen.getByRole('radio', { name: /sleepy/i }))
    await userEvent.click(screen.getByRole('button', { name: /flash this board/i }))

    expect(await screen.findByTestId('config-error')).toHaveTextContent(
      /sleepy needs a positive wake/i,
    )
    expect(screen.getByRole('button', { name: /flash this board/i })).toBeDisabled()
    expect(calls).not.toContain('POST /v1/enrollment-tokens')
    expect(flasher.writes).toHaveLength(0)
  })
})

describe('FlashBoard — onBoardIdentified', () => {
  it('reports the predicted device id once a chip is detected', async () => {
    mockFetch(await defaultRoutes())
    const onBoardIdentified = vi.fn()
    const flasher = new FakeFlasher(chipInfo())
    render(
      <FlashBoard
        onSessionExpired={vi.fn()}
        createFlasher={async () => flasher}
        onBoardIdentified={onBoardIdentified}
      />,
    )
    await userEvent.click(screen.getByRole('button', { name: /select port and detect/i }))
    await screen.findByTestId('chip-info')
    expect(onBoardIdentified).toHaveBeenCalledWith(predictDeviceId(chipInfo()))
    expect(onBoardIdentified).toHaveBeenCalledWith('a4cf12b3de90')
  })
})

describe('FlashBoard — the credentials', () => {
  it('bakes the token into the blob and leaks it nowhere else', async () => {
    await mockFetch(await defaultRoutes())
    const flasher = new FakeFlasher(chipInfo())
    await flashWith(flasher)
    await waitFor(() => expect(flasher.writes).toHaveLength(1))

    const config = flasher.writes[0]!.find((part) => part.address === CONFIG_OFFSET)!
    const payload = new TextDecoder().decode(config.data)
    expect(payload).toContain(PLAINTEXT)

    expect(document.body.textContent ?? '').not.toContain(PLAINTEXT)
    expect(screen.queryByTestId('flash-log')?.textContent ?? '').not.toContain(PLAINTEXT)
    expect(JSON.stringify(window.localStorage)).not.toContain(PLAINTEXT)
    expect(JSON.stringify(window.sessionStorage)).not.toContain(PLAINTEXT)
    expect(window.localStorage.length).toBe(0)
    expect(window.sessionStorage.length).toBe(0)
  })

  it('bakes the Wi-Fi passphrase into the blob and leaks it nowhere else', async () => {
    await mockFetch(await defaultRoutes())
    const flasher = new FakeFlasher(chipInfo())
    await flashWith(flasher)
    await waitFor(() => expect(flasher.writes).toHaveLength(1))

    const config = flasher.writes[0]!.find((part) => part.address === CONFIG_OFFSET)!
    const payload = new TextDecoder().decode(config.data)
    expect(payload).toContain(`"psk":"${PASSPHRASE}"`)
    expect(payload).toContain(`"ssid":"${SSID}"`)

    expect(document.body.textContent ?? '').not.toContain(PASSPHRASE)
    expect(screen.queryByTestId('flash-log')?.textContent ?? '').not.toContain(PASSPHRASE)
    expect(JSON.stringify(window.localStorage)).not.toContain(PASSPHRASE)
    expect(JSON.stringify(window.sessionStorage)).not.toContain(PASSPHRASE)
  })

  it('omits the heartbeat key when the operator left it blank', async () => {
    await mockFetch(await defaultRoutes())
    const flasher = new FakeFlasher(chipInfo())
    await flashWith(flasher)
    await waitFor(() => expect(flasher.writes).toHaveLength(1))

    const config = flasher.writes[0]!.find((part) => part.address === CONFIG_OFFSET)!
    expect(new TextDecoder().decode(config.data)).not.toContain('hb_s')
  })
})

describe('FlashBoard — failure handling', () => {
  it('revokes the minted token when the write fails, and still releases the port', async () => {
    const { calls } = mockFetch(await defaultRoutes())
    const flasher = new FakeFlasher(chipInfo(), () => {
      throw new Error('flash write failed at 0x20000')
    })
    await flashWith(flasher)

    expect(await screen.findByRole('alert')).toHaveTextContent(/flash write failed/i)
    expect(await screen.findByRole('alert')).toHaveTextContent(/token was revoked/i)
    expect(
      calls.filter((c) => c === `POST /v1/enrollment-tokens/${TOKEN_ID}/revoke`),
    ).toHaveLength(1)
    expect(flasher.closes).toBeGreaterThan(0)
  })

  it('says the token is still live when the revoke itself fails', async () => {
    mockFetch(
      await defaultRoutes({
        [`POST /v1/enrollment-tokens/${TOKEN_ID}/revoke`]: () => json({ detail: 'boom' }, 500),
      }),
    )
    const flasher = new FakeFlasher(chipInfo(), () => {
      throw new Error('flash write failed')
    })
    await flashWith(flasher)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(new RegExp(TOKEN_ID))
    expect(alert).toHaveTextContent(/still live/i)
    expect(alert.textContent ?? '').not.toContain(PLAINTEXT)
  })

  it('bounces a 401 mid-flash to the login gate instead of rendering a page error', async () => {
    mockFetch(
      await defaultRoutes({
        'GET /v1/agent/manifest': () => json({ detail: 'not authenticated' }, 401),
      }),
    )
    const flasher = new FakeFlasher(chipInfo())
    const { onSessionExpired } = await flashWith(flasher)

    await waitFor(() => expect(onSessionExpired).toHaveBeenCalled())
    expect(screen.queryByRole('alert')).toBeNull()
    expect(flasher.writes).toHaveLength(0)
  })
})

describe('FlashBoard — capability branches', () => {
  it('renders the Chrome/Edge explanation and nothing clickable without Web Serial', () => {
    // @ts-expect-error deleting an own property jsdom does not define natively
    delete navigator.serial
    render(<FlashBoard onSessionExpired={vi.fn()} />)

    expect(screen.getByRole('heading', { name: /flash a board/i })).toBeInTheDocument()
    expect(screen.getByText(/Web Serial API/i)).toBeInTheDocument()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })

  it('explains an insecure context rather than blaming the browser', () => {
    Object.defineProperty(window, 'isSecureContext', { value: false, configurable: true })
    render(<FlashBoard onSessionExpired={vi.fn()} />)

    expect(screen.getByText(/insecure connection/i)).toBeInTheDocument()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })

  it('stays usable when the operator cancels the port chooser', async () => {
    mockFetch(await defaultRoutes())
    const createFlasher = vi.fn(async () => {
      throw new DOMException(
        "Failed to execute 'requestPort' on 'Serial': No port selected by the user.",
        'NotFoundError',
      )
    })
    render(<FlashBoard onSessionExpired={vi.fn()} createFlasher={createFlasher} />)

    await userEvent.click(screen.getByRole('button', { name: /select port and detect/i }))
    expect(await screen.findByText('No board selected.')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /select port and detect/i }))
    await waitFor(() => expect(createFlasher).toHaveBeenCalledTimes(2))
  })

  it('offers "My board isn\'t listed", closed, with both bridge drivers linked (S0-fe-8)', async () => {
    mockFetch(await defaultRoutes())
    render(
      <FlashBoard
        onSessionExpired={vi.fn()}
        createFlasher={async () => new FakeFlasher(chipInfo())}
      />,
    )

    const help = screen.getByTestId('port-help')
    expect(help).not.toHaveAttribute('open')
    expect(screen.getByText(/my board isn't listed/i)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /CP210x driver/i })).toHaveAttribute(
      'href',
      expect.stringContaining('silabs.com'),
    )
    expect(screen.getByRole('link', { name: /CH340 driver/i })).toHaveAttribute(
      'href',
      expect.stringContaining('wch-ic.com'),
    )
    expect(help).toHaveTextContent(/COM1 is never your board/)
    expect(help).toHaveTextContent(
      /macOS and Linux have both drivers built in; Windows usually does not/,
    )
  })

  it("opens the help when the operator dismisses the chooser — the stuck operator's next move", async () => {
    mockFetch(await defaultRoutes())
    const createFlasher = vi.fn(async () => {
      throw new DOMException(
        "Failed to execute 'requestPort' on 'Serial': No port selected by the user.",
        'NotFoundError',
      )
    })
    render(<FlashBoard onSessionExpired={vi.fn()} createFlasher={createFlasher} />)

    await userEvent.click(screen.getByRole('button', { name: /select port and detect/i }))
    await screen.findByText('No board selected.')
    await waitFor(() => expect(screen.getByTestId('port-help')).toHaveAttribute('open'))
  })

  it('refuses COM1 by name and opens the help, instead of blaming the board', async () => {
    mockFetch(await defaultRoutes())
    const createFlasher = vi.fn(async () => {
      checkChosenPort({}, () => {})
      return new FakeFlasher(chipInfo())
    })
    render(<FlashBoard onSessionExpired={vi.fn()} createFlasher={createFlasher} />)

    await userEvent.click(screen.getByRole('button', { name: /select port and detect/i }))
    expect(await screen.findByText(BUILT_IN_PORT)).toBeInTheDocument()
    expect(screen.queryByTestId('chip-info')).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('port-help')).toHaveAttribute('open'))
  })

  it('lists the available targets from the manifest', async () => {
    mockFetch(await defaultRoutes())
    render(
      <FlashBoard
        onSessionExpired={vi.fn()}
        createFlasher={async () => new FakeFlasher(chipInfo())}
      />,
    )

    expect(await screen.findByTestId('agent-targets')).toHaveTextContent('Agent 0.1.0')
    expect(screen.getByTestId('agent-targets')).toHaveTextContent('esp32 (ESP32)')
  })

  it('refuses to flash when the manifest could not be read, and says why', async () => {
    const detail =
      'the agent image store cannot be reached, so there is no firmware to offer. ' +
      'No board can be flashed until it is back.'
    mockFetch(await defaultRoutes({ 'GET /v1/agent/manifest': () => json({ detail }, 503) }))
    render(
      <FlashBoard
        onSessionExpired={vi.fn()}
        createFlasher={async () => new FakeFlasher(chipInfo())}
      />,
    )

    expect(await screen.findByText(detail)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /flash this board/i })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: /select port and detect/i }))
    await screen.findByTestId('chip-info')
    expect(screen.getByRole('button', { name: /flash this board/i })).toBeDisabled()
    expect(screen.queryByTestId('agent-targets')).toBeNull()
  })

  it('names the version of each target when a publish has left them out of step', async () => {
    const built = await manifest()
    const older: AgentBuildInfo = {
      ...built.builds[0]!,
      target: 'esp32c6',
      chip_family: 'ESP32-C6',
      agent_version: '0.0.9',
    }
    mockFetch(
      await defaultRoutes({
        'GET /v1/agent/manifest': () => json({ ...built, builds: [...built.builds, older] }),
      }),
    )
    render(
      <FlashBoard
        onSessionExpired={vi.fn()}
        createFlasher={async () => new FakeFlasher(chipInfo())}
      />,
    )

    const targets = await screen.findByTestId('agent-targets')
    expect(targets).toHaveTextContent('esp32 (ESP32, 0.1.0)')
    expect(targets).toHaveTextContent('esp32c6 (ESP32-C6, 0.0.9)')
    expect(targets).toHaveTextContent('Agent per target')
    expect(targets).not.toHaveTextContent('Agent 0.1.0')
  })
})

describe('FlashBoard — the diagnostic bundle', () => {
  it('carries the chip, the config and the versions, and neither secret', async () => {
    const user = userEvent.setup()
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    mockFetch(await defaultRoutes())

    const { factory: createConsole } = fakeConsoleSessions([
      [
        ...SPENT_TOKEN_BOOT,
        `W (3200) ff-wifi: joining ${SSID} with psk ${PASSPHRASE}`,
        `W (3300) ff-enroll: token ${PLAINTEXT} was refused`,
      ],
    ])
    render(
      <FlashBoard
        onSessionExpired={vi.fn()}
        createFlasher={async () => new FakeFlasher(chipInfo())}
        createConsole={createConsole}
      />,
    )
    await user.click(screen.getByRole('button', { name: /select port and detect/i }))
    await screen.findByTestId('chip-info')
    await user.type(screen.getByLabelText(/ssid/i), SSID)
    await user.type(screen.getByLabelText(/passphrase/i), PASSPHRASE)
    await user.clear(screen.getByLabelText(/broker uri/i))
    await user.type(screen.getByLabelText(/broker uri/i), BROKER_URI)
    await user.click(screen.getByRole('button', { name: /flash this board/i }))
    await screen.findByTestId('console-fault')

    await user.click(screen.getByRole('button', { name: /copy diagnostic bundle/i }))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    const bundle = writeText.mock.calls[0]![0] as string

    expect(bundle).toContain('ESP32-D0WD-V3 (revision v3.1)')
    expect(bundle).toContain(SSID)
    expect(bundle).toContain('layout ab-4m-v1')
    expect(bundle).toContain('agent        0.1.0')
    expect(bundle).toContain('server       0.3.1')
    expect(bundle).toContain('enroll 409')
    expect(bundle).toContain('single-use')

    expect(bundle).not.toContain(PASSPHRASE)
    expect(bundle).not.toContain(PLAINTEXT)
    expect(bundle).not.toContain('s3cr3tbrokerpw')
    expect(bundle).toContain('ffe_[REDACTED]')
    expect(bundle).toContain('mqtts://fleet:[REDACTED]@bench.local:8883')
    expect(bundle).toContain(`passphrase ${PASSPHRASE.length} chars (never printed)`)
  })
})
