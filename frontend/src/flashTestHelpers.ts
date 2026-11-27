// Shared test fixtures, partition-table builder, and mock factories for `flash*.test.tsx`.

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createElement } from 'react'
import { vi } from 'vitest'
import { FlashBoard } from './FlashBoard'
import type { AgentManifest } from './api'
import type { BoardConsole, ConsoleFactory } from './boardConsole'
import type { BoardFlasher, ChipInfo, FlashPart, WriteOptions } from './flasher'

export const TOKEN_ID = '11111111-1111-4111-8111-111111111111'
export const PLAINTEXT = `ffe_${TOKEN_ID}.s3cr3tflashersecretvalue`
export const TOKEN_ID_2 = '22222222-2222-4222-8222-222222222222'
export const PLAINTEXT_2 = `ffe_${TOKEN_ID_2}.s3cr3trecoverysecretvalue`
export const PASSPHRASE = 'correct-horse-battery-staple'
export const SSID = 'fleetforge-test'
export const BROKER_URI = 'mqtts://fleet:s3cr3tbrokerpw@bench.local:8883'

export const CONFIG_OFFSET = 0x12000
export const NVS_OFFSET = 0x9000
export const NVS_SIZE = 0x6000

function partitionTable(): Uint8Array {
  const rows: Array<[string, number, number, number, number]> = [
    ['nvs', 1, 2, NVS_OFFSET, NVS_SIZE],
    ['otadata', 1, 0, 0xf000, 0x2000],
    ['phy_init', 1, 1, 0x11000, 0x1000],
    ['ff_cfg', 1, 0x40, CONFIG_OFFSET, 0x1000],
    ['ota_0', 0, 0x10, 0x20000, 0x1e0000],
  ]
  const bytes = new Uint8Array(rows.length * 32 + 32)
  const view = new DataView(bytes.buffer)
  rows.forEach(([label, type, subtype, offset, size], index) => {
    const at = index * 32
    view.setUint16(at, 0x50aa, true)
    view.setUint8(at + 2, type)
    view.setUint8(at + 3, subtype)
    view.setUint32(at + 4, offset, true)
    view.setUint32(at + 8, size, true)
    bytes.set(new TextEncoder().encode(label), at + 12)
  })
  view.setUint16(rows.length * 32, 0xebeb, true)
  return bytes
}

export const PART_BYTES: Record<string, Uint8Array> = {
  app: new Uint8Array(512).fill(0xa5),
  bootloader: new Uint8Array(64).fill(0x5a),
  'partition-table': partitionTable(),
}

export const PART_OFFSETS: Record<string, number> = {
  app: 0x20000,
  bootloader: 0x1000,
  'partition-table': 0x8000,
}

export async function sha256(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new Uint8Array(bytes))
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}

export async function manifest(
  overrides: Partial<AgentManifest['builds'][number]> = {},
): Promise<AgentManifest> {
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
        config_sha256: 'c0'.repeat(32),
        build_digest: 'b1'.repeat(32),
        config_partition: { label: 'ff_cfg', offset: CONFIG_OFFSET, size: 4096 },
        parts: await Promise.all(
          Object.entries(PART_BYTES).map(async ([name, bytes]) => ({
            name,
            offset: PART_OFFSETS[name]!,
            size: bytes.length,
            sha256: await sha256(bytes),
          })),
        ),
        ...overrides,
      },
    ],
  }
}

export function chipInfo(overrides: Partial<ChipInfo> = {}): ChipInfo {
  return {
    chipName: 'ESP32',
    description: 'ESP32-D0WD-V3 (revision v3.1)',
    macAddress: 'A4:CF:12:B3:DE:90',
    flashSizeBytes: 4 * 1024 * 1024,
    features: ['WiFi', 'BT', 'Dual Core'],
    ...overrides,
  }
}

export class FakeFlasher implements BoardFlasher {
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

export type Routes = Record<string, () => Response | Promise<Response>>

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

export function mockFetch(routes: Routes) {
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

export async function defaultRoutes(extra: Routes = {}): Promise<Routes> {
  const built = await manifest()
  const routes: Routes = {
    'GET /v1/agent/manifest': () => json(built),
    'GET /v1/healthz': () =>
      json({ status: 'ok', version: '0.3.1', commit: 'unknown', built_at: 'unknown' }),
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
    routes[`GET /v1/agent/esp32/${name}?layout=ab-4m-v1`] = () =>
      new Response(bytes.slice(), { headers: { 'content-type': 'application/octet-stream' } })
  }
  return { ...routes, ...extra }
}

export async function flashWith(flasher: FakeFlasher, onSessionExpired = vi.fn()) {
  render(createElement(FlashBoard, { onSessionExpired, createFlasher: async () => flasher }))
  await userEvent.click(screen.getByRole('button', { name: /select port and detect/i }))
  await screen.findByTestId('chip-info')
  await userEvent.type(screen.getByLabelText(/ssid/i), SSID)
  await userEvent.type(screen.getByLabelText(/passphrase/i), PASSPHRASE)
  await userEvent.click(screen.getByRole('button', { name: /flash this board/i }))
  return { onSessionExpired }
}

export function fakeConsoleSessions(scripts: string[][]) {
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

export const SPENT_TOKEN_BOOT = [
  'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
  'I (900) ff-net: wifi link up, ip 192.168.1.40 gw 192.168.1.1 mask 255.255.255.0',
  'I (1500) ff-time: sntp: 1970-01-01T00:00:02Z -> 2026-09-11T08:14:05Z (via pool.ntp.org)',
  'E (2600) ff-enroll: enroll 409: this token is already used, revoked or expired.',
  "E (2900) ff-agent: halted: this board's enrollment token was refused for good — re-flash " +
    'ff_cfg with a fresh ffe_ token (POST /v1/enrollment-tokens)',
]

export const RECOVERED_BOOT = [
  'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
  'I (900) ff-net: wifi link up, ip 192.168.1.40 gw 192.168.1.1 mask 255.255.255.0',
  'I (1500) ff-time: sntp: 1970-01-01T00:00:02Z -> 2026-09-11T08:15:10Z (via pool.ntp.org)',
  'I (2600) ff-enroll: enroll 200 https://bingo.tvaroska.sk/v1/enroll',
  'I (3100) ff-mqtt: mqtt connected as a4cf12b3de90 (mqtts://bingo.tvaroska.sk:8883)',
]

export function setupWebSerialJsdom(): void {
  Object.defineProperty(navigator, 'serial', { value: {}, configurable: true })
  Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true })
}
