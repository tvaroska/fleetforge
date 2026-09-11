// What these tests defend, in order of how much a regression would cost:
//
// 1. **Every write address comes from the manifest.** The bootloader is at 0x1000 on
//    ESP32 and 0x0 on the RISC-V parts; a hardcoded offset flashes cleanly and never
//    boots. Test 1 fails the moment anyone types an address.
// 2. **Nothing is written that has not been checked** — chip family, flash size, a config
//    partition, and the sha256 of every downloaded part.
// 3. **The enrollment token is minted only after every check, revoked on failure, and
//    never leaves the blob.** It is a single-use fleet-join credential the server cannot
//    re-derive. Same discipline as `EnrollBoard.test.tsx`, extended to the passphrase.
// 4. **The port is always released**, and a 401 bounces to the login gate.

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { FlashBoard } from './FlashBoard'
import type { AgentManifest } from './api'
import type { BoardConsole, ConsoleFactory } from './boardConsole'
import type { BoardFlasher, ChipInfo, FlashPart, WriteOptions } from './flasher'

const TOKEN_ID = '11111111-1111-4111-8111-111111111111'
const PLAINTEXT = `ffe_${TOKEN_ID}.s3cr3tflashersecretvalue`
/** The token the SECOND mint returns, so the recovery blob can be told from the first. */
const TOKEN_ID_2 = '22222222-2222-4222-8222-222222222222'
const PLAINTEXT_2 = `ffe_${TOKEN_ID_2}.s3cr3trecoverysecretvalue`
const PASSPHRASE = 'correct-horse-battery-staple'
const SSID = 'fleetforge-test'

// Deliberately NOT in ascending offset order: `planWrite` must sort, and the config blob
// (0x12000) lands between the partition table and the app.
const PART_BYTES: Record<string, Uint8Array> = {
  app: new Uint8Array(512).fill(0xa5),
  bootloader: new Uint8Array(64).fill(0x5a),
  'partition-table': new Uint8Array(96).fill(0x3c),
}
const PART_OFFSETS: Record<string, number> = {
  app: 0x20000,
  bootloader: 0x1000,
  'partition-table': 0x8000,
}
const CONFIG_OFFSET = 0x12000

async function sha256(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new Uint8Array(bytes))
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}

async function manifest(overrides: Partial<AgentManifest['builds'][number]> = {}) {
  return {
    agent_version: '0.1.0',
    builds: [
      {
        target: 'esp32',
        chip_family: 'ESP32',
        agent_version: '0.1.0',
        idf_version: 'v5.5.5',
        idf_image: 'espressif/idf:v5.5.5',
        source_commit: 'deadbeef',
        built_at: '2026-09-10T00:00:00Z',
        partition_layout: 'ab-4m-v1',
        ota_slot_size: 1966080,
        flash_size: '4MB',
        config_partition: { label: 'ff_cfg', offset: CONFIG_OFFSET, size: 4096 },
        parts: await Promise.all(
          Object.entries(PART_BYTES).map(async ([name, bytes]) => ({
            name,
            offset: PART_OFFSETS[name],
            size: bytes.length,
            sha256: await sha256(bytes),
          })),
        ),
        ...overrides,
      },
    ],
  } satisfies AgentManifest
}

function chipInfo(overrides: Partial<ChipInfo> = {}): ChipInfo {
  return {
    chipName: 'ESP32',
    description: 'ESP32-D0WD-V3 (revision v3.1)',
    macAddress: 'A4:CF:12:B3:DE:90',
    flashSizeBytes: 4 * 1024 * 1024,
    features: ['WiFi', 'BT', 'Dual Core'],
    ...overrides,
  }
}

class FakeFlasher implements BoardFlasher {
  writes: FlashPart[][] = []
  writeOptions: WriteOptions[] = []
  finishes = 0
  closes = 0

  constructor(
    private readonly info: ChipInfo,
    private readonly onWrite: (() => void) | null = null,
  ) {}

  async detect(): Promise<ChipInfo> {
    return this.info
  }

  async write(parts: FlashPart[], options: WriteOptions): Promise<void> {
    this.writes.push(parts)
    this.writeOptions.push(options)
    options.onProgress(0, 10, 100)
    this.onWrite?.()
  }

  async finish(): Promise<void> {
    this.finishes += 1
    await this.close()
  }

  async close(): Promise<void> {
    this.closes += 1
  }
}

/**
 * A tiny router over `fetch`. A `Response` body can be read once, so every entry is a
 * FACTORY — the same trap `fleet.test.tsx` documents.
 */
type Routes = Record<string, () => Response | Promise<Response>>

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function mockFetch(routes: Routes) {
  const calls: string[] = []
  const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = typeof input === 'string' ? input : String(input)
    const key = `${(init?.method ?? 'GET').toUpperCase()} ${url}`
    calls.push(key)
    const route = routes[key]
    if (route === undefined) return new Response('not found', { status: 404 })
    return route()
  })
  return { calls, spy }
}

async function defaultRoutes(extra: Routes = {}): Promise<Routes> {
  const built = await manifest()
  const routes: Routes = {
    'GET /v1/agent/manifest': () => json(built),
    'POST /v1/enrollment-tokens': () =>
      json({
        id: TOKEN_ID,
        token: PLAINTEXT,
        group_id: null,
        expires_at: '2026-09-11T00:00:00Z',
        created_at: '2026-09-10T00:00:00Z',
      }),
    [`POST /v1/enrollment-tokens/${TOKEN_ID}/revoke`]: () => new Response(null, { status: 204 }),
  }
  for (const [name, bytes] of Object.entries(PART_BYTES)) {
    routes[`GET /v1/agent/esp32/${name}`] = () =>
      new Response(bytes.slice(), { headers: { 'content-type': 'application/octet-stream' } })
  }
  return { ...routes, ...extra }
}

/** Connect, fill the Wi-Fi form, and press Flash. */
async function flashWith(flasher: FakeFlasher, onSessionExpired = vi.fn()) {
  render(<FlashBoard onSessionExpired={onSessionExpired} createFlasher={async () => flasher} />)
  await userEvent.click(screen.getByRole('button', { name: /select port and detect/i }))
  await screen.findByTestId('chip-info')
  await userEvent.type(screen.getByLabelText(/ssid/i), SSID)
  await userEvent.type(screen.getByLabelText(/passphrase/i), PASSPHRASE)
  await userEvent.click(screen.getByRole('button', { name: /flash this board/i }))
  return { onSessionExpired }
}

/**
 * A console that scripts a DIFFERENT boot per session. Lifted from `BoardConsole.test.tsx`
 * — same contract, including the part that matters most: `lines()` yields the script and
 * then BLOCKS rather than returning, because `agent_main.c:166` retries forever and a
 * stream that ended would let the panel invent an inactivity timeout.
 */
function fakeConsoleSessions(scripts: string[][]) {
  const state = { opened: 0, closed: 0, reboots: 0, acquires: [] as string[] }
  const factory: ConsoleFactory = async ({ acquire }) => {
    const script = scripts[state.opened] ?? []
    state.opened += 1
    state.acquires.push(acquire)
    let closed = false
    let release: (() => void) | null = null

    const board: BoardConsole = {
      async *lines() {
        for (const line of script) yield line
        if (closed) return
        await new Promise<void>((resolve) => {
          release = resolve
        })
      },
      async reboot() {
        state.reboots += 1
      },
      async close() {
        if (closed) return
        closed = true
        state.closed += 1
        release?.()
      },
    }
    return board
  }
  return { factory, state }
}

/** The board came up, joined, set its clock — and its token had already been spent. */
const SPENT_TOKEN_BOOT = [
  'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
  'I (900) ff-net: wifi link up, ip 192.168.1.40 gw 192.168.1.1 mask 255.255.255.0',
  'I (1500) ff-time: sntp: 1970-01-01T00:00:02Z -> 2026-09-11T08:14:05Z (via pool.ntp.org)',
  'E (2600) ff-enroll: enroll 409: this token is already used, revoked or expired.',
  'E (2900) ff-agent: halted: this board\'s enrollment token was refused for good — re-flash ' +
    'ff_cfg with a fresh ffe_ token (POST /v1/enrollment-tokens)',
]

/** The same board after the button: a fresh single-use token, all the way to the fleet. */
const RECOVERED_BOOT = [
  'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
  'I (900) ff-net: wifi link up, ip 192.168.1.40 gw 192.168.1.1 mask 255.255.255.0',
  'I (1500) ff-time: sntp: 1970-01-01T00:00:02Z -> 2026-09-11T08:15:10Z (via pool.ntp.org)',
  'I (2600) ff-enroll: enroll 200 https://bingo.tvaroska.sk/v1/enroll',
  'I (3100) ff-mqtt: mqtt connected as a4cf12b3de90 (mqtts://bingo.tvaroska.sk:8883)',
]

beforeEach(() => {
  // jsdom has neither of these. `flasher.ts` exists so the ENGINE needs no DOM; the view
  // still branches on capability, so the capability has to be stated here.
  Object.defineProperty(navigator, 'serial', { value: {}, configurable: true })
  Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true })
})

describe('FlashBoard — the write plan', () => {
  it('writes exactly the manifest offsets, ascending, with the 4 KB blob at the config offset', async () => {
    await mockFetch(await defaultRoutes())
    const flasher = new FakeFlasher(chipInfo())
    await flashWith(flasher)

    await waitFor(() => expect(flasher.writes).toHaveLength(1))
    const plan = flasher.writes[0]
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
    // Erase is on by default: a live NVS credential makes a fresh token dead on arrival.
    expect(flasher.writeOptions[0].eraseAll).toBe(true)
    expect(flasher.finishes).toBe(1)
    expect(flasher.closes).toBeGreaterThan(0)
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
    // A real `ab-4m-v1` app is megabytes; the fixture's is 512 bytes, so the bound has to
    // come from a manifest that declares a real one. The check runs before any download,
    // so these bytes are never fetched.
    const built = await manifest()
    built.builds[0].parts = built.builds[0].parts.map((part) =>
      part.name === 'app' ? { ...part, size: 3_000_000 } : part,
    )
    const { calls } = mockFetch(await defaultRoutes({ 'GET /v1/agent/manifest': () => json(built) }))
    const flasher = new FakeFlasher(chipInfo({ flashSizeBytes: 2 * 1024 * 1024 }))
    await flashWith(flasher)

    expect(await screen.findByRole('alert')).toHaveTextContent(/2 MB of flash/i)
    expect(flasher.writes).toHaveLength(0)
    expect(calls).not.toContain('POST /v1/enrollment-tokens')
  })

  it('refuses a bundle with no config partition and says to rebuild it', async () => {
    const built = await manifest({ config_partition: null })
    const { calls } = mockFetch(await defaultRoutes({ 'GET /v1/agent/manifest': () => json(built) }))
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
        // Right length, wrong bytes: exactly what a corrupted download looks like.
        'GET /v1/agent/esp32/app': () => new Response(new Uint8Array(512).fill(0x00)),
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

    // The form validates as it is typed, so the refusal is on screen before the button is
    // pressed — and the button is dead, because the engine's copy of the same check would
    // otherwise be reached only after a single-use token had been minted.
    expect(await screen.findByTestId('config-error')).toHaveTextContent(
      /sleepy needs a positive wake/i,
    )
    expect(screen.getByRole('button', { name: /flash this board/i })).toBeDisabled()
    expect(calls).not.toContain('POST /v1/enrollment-tokens')
    expect(flasher.writes).toHaveLength(0)
  })
})

describe('FlashBoard — the credentials', () => {
  it('bakes the token into the blob and leaks it nowhere else', async () => {
    await mockFetch(await defaultRoutes())
    const flasher = new FakeFlasher(chipInfo())
    await flashWith(flasher)
    await waitFor(() => expect(flasher.writes).toHaveLength(1))

    const config = flasher.writes[0].find((part) => part.address === CONFIG_OFFSET)!
    const payload = new TextDecoder().decode(config.data)
    expect(payload).toContain(PLAINTEXT)

    // The four places R0-fe-1 established, and for the same reason.
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

    const config = flasher.writes[0].find((part) => part.address === CONFIG_OFFSET)!
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

    const config = flasher.writes[0].find((part) => part.address === CONFIG_OFFSET)!
    // A written key freezes today's firmware default into this board forever.
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
    expect(calls.filter((c) => c === `POST /v1/enrollment-tokens/${TOKEN_ID}/revoke`)).toHaveLength(1)
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
    // The id is safe to show; the plaintext is not.
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

// ── S0-fe-6 ─────────────────────────────────────────────────────────────────────────────
//
// The task line, end to end through the real `FlashBoard` with both seams faked: the
// operator presses ONE button instead of performing three manual steps (walk back up the
// page, re-select the port, press Flash), and the fault the button was attached to is
// resolved. Everything below the click is real code — the console panel, the flash engine,
// the mint-last discipline, the ff_cfg writer.
describe('FlashBoard — recovery is a button (S0-fe-6)', () => {
  it('a spent token is fixed by pressing a button, not by following an instruction', async () => {
    const user = userEvent.setup()
    let mints = 0
    const { calls } = mockFetch(
      await defaultRoutes({
        'POST /v1/enrollment-tokens': () => {
          mints += 1
          // A second, DIFFERENT single-use token, so the recovery blob can be told apart.
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

    // 1. The console opens by itself (S0-fe-5) and names the spent token — the sentence a
    //    non-engineer can act on, not the agent's engineer-facing `halted:` reason.
    const fault = await screen.findByTestId('console-fault')
    await waitFor(() => expect(fault).toHaveTextContent(/single-use/))

    // 2. And the remedy is ON SCREEN as an action. This is the whole task: the operator is
    //    never told to go and do three things.
    const button = await screen.findByRole('button', { name: /re-flash the board/i })

    // 3. One press. The console lets the port go, and esptool takes it.
    await user.click(button)
    await waitFor(() => expect(flashers).toHaveLength(2))
    expect(console_.closed).toBe(1)

    // 4. A FRESH single-use token was minted, and the recovery write erases — a stale NVS
    //    credential would make the new token dead on arrival and the operator would see
    //    the very same fault again.
    await waitFor(() => expect(flashers[1].writes).toHaveLength(1))
    expect(calls.filter((c) => c === 'POST /v1/enrollment-tokens')).toHaveLength(2)
    expect(flashers[1].writeOptions[0].eraseAll).toBe(true)

    // 5. And it is the SECOND token that went into the blob, not a replay of the first.
    const config = flashers[1].writes[0].find((part) => part.address === CONFIG_OFFSET)!
    const payload = new TextDecoder().decode(config.data)
    expect(payload).toContain(PLAINTEXT_2)
    expect(payload).not.toContain(PLAINTEXT)

    // 6. The loop closes itself: `phase` went done again, so the panel re-opens the port
    //    with no second click, and the board it now watches reaches the fleet. The fault
    //    the button was attached to is gone — cleared by progress, not by clearing the log.
    await waitFor(() => expect(console_.opened).toBe(2))
    expect(console_.acquires).toEqual(['granted', 'granted'])
    const milestones = screen.getByTestId('boot-milestones')
    await waitFor(() => {
      expect(milestones.querySelectorAll('[data-state="done"]')).toHaveLength(5)
    })
    expect(await screen.findByTestId('console-online')).toBeInTheDocument()
    expect(screen.queryByTestId('console-fault')).not.toBeInTheDocument()

    // 7. R0-fe-1's rule, re-asserted because a new code path now handles both secrets.
    const body = document.body.textContent ?? ''
    expect(body).not.toContain(PLAINTEXT)
    expect(body).not.toContain(PLAINTEXT_2)
    expect(body).not.toContain(PASSPHRASE)
  })

  it('says a fresh click is needed when the activation window closed, and works on the next one', async () => {
    // `requestPort()` needs transient user activation, and the recovery click awaits the
    // console's `release()` first. Untranslated, Chromium's wording maps to "the page must
    // be on HTTPS or localhost" — wrong, and it sends the operator nowhere.
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

describe('FlashBoard — capability branches', () => {
  it('renders the Chrome/Edge explanation and nothing clickable without Web Serial', async () => {
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
      // EXACTLY what Chromium throws when the chooser is dismissed — verified against a
      // real headless Chromium. Untranslated it reads like a fault, so the engine (not
      // just the adapter) has to run it through `explainFlashError`.
      throw new DOMException(
        "Failed to execute 'requestPort' on 'Serial': No port selected by the user.",
        'NotFoundError',
      )
    })
    render(<FlashBoard onSessionExpired={vi.fn()} createFlasher={createFlasher} />)

    await userEvent.click(screen.getByRole('button', { name: /select port and detect/i }))
    // `findByText`, not `findByRole('alert')`: the untouched form is also complaining that
    // Wi-Fi needs an SSID, and two alerts are two alerts.
    expect(await screen.findByText('No board selected.')).toBeInTheDocument()

    // A second click must open the chooser again.
    await userEvent.click(screen.getByRole('button', { name: /select port and detect/i }))
    await waitFor(() => expect(createFlasher).toHaveBeenCalledTimes(2))
  })

  it('lists the available targets from the manifest', async () => {
    mockFetch(await defaultRoutes())
    render(<FlashBoard onSessionExpired={vi.fn()} createFlasher={async () => new FakeFlasher(chipInfo())} />)

    expect(await screen.findByTestId('agent-targets')).toHaveTextContent('Agent 0.1.0')
    expect(screen.getByTestId('agent-targets')).toHaveTextContent('esp32 (ESP32)')
  })
})
