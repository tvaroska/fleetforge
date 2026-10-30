// Unit tests for Assembly-Line Batch Prefix Mode (`FlashBoard.tsx`, P0-4 / P2-3)
// and the Full-Width `RULE E301` Pit & Carrier Dock Cockpit (`PitDockCockpit.tsx`, P2-1 / H6).

import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { FlashBoard } from './FlashBoard'
import { PitDockCockpit } from './PitDockCockpit'
import type { AgentManifest, DeviceGroupSummary, DeviceSummary } from './api'
import type { BoardFlasher, ChipInfo, FlashPart, WriteOptions } from './flasher'

const CONFIG_OFFSET = 0x12000
const NVS_OFFSET = 0x9000
const NVS_SIZE = 0x6000

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

const PART_BYTES: Record<string, Uint8Array> = {
  app: new Uint8Array(512).fill(0xa5),
  bootloader: new Uint8Array(64).fill(0x5a),
  'partition-table': partitionTable(),
}
const PART_OFFSETS: Record<string, number> = {
  app: 0x20000,
  bootloader: 0x1000,
  'partition-table': 0x8000,
}

async function sha256(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new Uint8Array(bytes))
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}

async function buildManifest(): Promise<AgentManifest> {
  return {
    agent_version: '0.4.6',
    builds: [
      {
        target: 'esp32',
        chip_family: 'ESP32',
        agent_version: '0.4.6',
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
            offset: PART_OFFSETS[name],
            size: bytes.length,
            sha256: await sha256(bytes),
          })),
        ),
      },
    ],
  }
}

function chipInfo(): ChipInfo {
  return {
    chipName: 'ESP32',
    description: 'ESP32-D0WD-V3 (revision v3.1)',
    macAddress: 'A4:CF:12:B3:DE:90',
    flashSizeBytes: 4 * 1024 * 1024,
    features: ['WiFi', 'BT', 'Dual Core'],
  }
}

class FakeFlasher implements BoardFlasher {
  writes: FlashPart[][] = []
  async detect(): Promise<ChipInfo> {
    return chipInfo()
  }
  async write(parts: FlashPart[], options: WriteOptions): Promise<void> {
    this.writes.push(parts)
    options.onProgress(0, 10, 100)
  }
  async finish(): Promise<void> {}
  async close(): Promise<void> {}
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

const GROUPS: DeviceGroupSummary[] = [
  {
    id: 'grp-canary',
    project_id: 'proj-1',
    name: '1. Canary Ring',
    description: 'Canary cohort',
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
  },
]

beforeEach(() => {
  Object.defineProperty(navigator, 'serial', { value: {}, configurable: true })
  Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true })
})

describe('FlashBoard — Assembly-Line Batch Prefix Mode (P0-4, P2-3)', () => {
  it('auto-increments sequential board names (gh-node-01 -> gh-node-02) and retains cohort groupId on Flash another board', async () => {
    const manifestData = await buildManifest()
    const tokenPayloads: unknown[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url === '/v1/agent/manifest') return jsonResponse(manifestData)
      if (url === '/v1/healthz') return jsonResponse({ status: 'ok', version: '0.7.2' })
      if (url.startsWith('/v1/enrollment-tokens') && init?.method === 'POST') {
        tokenPayloads.push(JSON.parse(String(init.body)))
        return jsonResponse({
          id: '11111111-1111-4111-8111-111111111111',
          token: 'ffe_11111111-1111-4111-8111-111111111111.secret',
          group_id: 'grp-canary',
          expires_at: '2026-10-26T00:00:00Z',
          created_at: '2026-10-25T00:00:00Z',
        })
      }
      for (const [name, bytes] of Object.entries(PART_BYTES)) {
        if (url.includes(`/v1/agent/esp32/${name}`)) {
          return new Response(bytes.slice(), {
            headers: { 'content-type': 'application/octet-stream' },
          })
        }
      }
      return new Response('not found', { status: 404 })
    })

    const flasher = new FakeFlasher()
    render(
      <FlashBoard
        onSessionExpired={vi.fn()}
        createFlasher={async () => flasher}
        groups={GROUPS}
      />,
    )

    await userEvent.click(screen.getByRole('button', { name: /select port and detect/i }))
    await screen.findByTestId('chip-info')
    await userEvent.type(screen.getByLabelText(/ssid/i), 'bench-wifi')
    await userEvent.type(screen.getByLabelText(/passphrase/i), 'secret-passphrase')

    // Select cohort group and enter batch name prefix
    await userEvent.selectOptions(screen.getByLabelText(/^Group/i), 'grp-canary')
    const prefixInput = screen.getByTestId('batch-name-prefix')
    await userEvent.type(prefixInput, 'gh-node-')

    expect(screen.getByTestId('batch-next-name')).toHaveTextContent('#01')
    expect(screen.getByTestId('batch-next-name')).toHaveTextContent('gh-node-01')

    await userEvent.click(screen.getByRole('button', { name: /flash this board/i }))
    await screen.findByRole('heading', { name: 'Flashed' })
    expect(screen.getByTestId('batch-flashed-name')).toHaveTextContent('gh-node-01')
    expect(tokenPayloads).toEqual([{ group_id: 'grp-canary' }])

    // Click "Flash another board" -> advances to gh-node-02 and retains group_id
    await userEvent.click(screen.getByRole('button', { name: /flash another board/i }))
    expect(screen.getByTestId('batch-next-name')).toHaveTextContent('#02')
    expect(screen.getByTestId('batch-next-name')).toHaveTextContent('gh-node-02')
    expect(screen.getByLabelText(/^Group/i)).toHaveValue('grp-canary')
  })
})

describe('PitDockCockpit — RULE E301 Venue Offline & USB-C Pit Cockpit (P2-1, H6)', () => {
  it('renders EMCON radio lock pills, detects Peer Mesh sub_versions skew, and replays a dock bundle to the server', async () => {
    const devices: DeviceSummary[] = [
      {
        device_id: '94a9900009a4',
        name: 'pit',
        group_id: null,
        platform_type: 'esp32s3',
        fw_version: '1.5.0',
        agent_version: '0.7.2',
        link_type: 'dock',
        ssid: null,
        known_networks: null,
        power_class: 'always_on',
        expected_wake_interval_s: null,
        parent_device_id: null,
        partition_layout: 'ab-4m-v1',
        ota_slot_size: 1966080,
        capabilities: ['ota'],
        last_seen: '2026-10-25T14:00:00Z',
        enrolled_at: '2026-10-20T10:00:00Z',
        broker_provisioned_at: '2026-10-20T10:00:01Z',
        online: true,
        sub_versions: { batteries_csv: 'v15', mcu_fw: 'v3' },
        deploy: null,
      },
      {
        device_id: '7b128800c309',
        name: 'scouting-chief',
        group_id: null,
        platform_type: 'esp32s3',
        fw_version: '1.5.0',
        agent_version: '0.7.2',
        link_type: 'lora',
        ssid: null,
        known_networks: null,
        power_class: 'always_on',
        expected_wake_interval_s: null,
        parent_device_id: null,
        partition_layout: 'ab-4m-v1',
        ota_slot_size: 1966080,
        capabilities: ['ota'],
        last_seen: '2026-10-25T14:00:00Z',
        enrolled_at: '2026-10-20T10:00:00Z',
        broker_provisioned_at: '2026-10-20T10:00:01Z',
        online: false,
        sub_versions: { batteries_csv: 'v14', mcu_fw: 'v3' },
        deploy: null,
      },
    ]

    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      jsonResponse({
        device_id: '94a9900009a4',
        link_type: 'dock',
        fw_version: '1.5.0',
        sub_versions: { batteries_csv: 'v15', mcu_fw: 'v3' },
        statuses_recorded: 1,
        telemetry_batches_recorded: 1,
        log_batches_recorded: 1,
        device: devices[0],
      }),
    )

    const onSynced = vi.fn()
    render(<PitDockCockpit devices={devices} onSynced={onSynced} />)

    const banner = screen.getByTestId('emcon-posture-banner')
    expect(banner).toHaveTextContent('⊘ VENUE OFFLINE / EMCON MODE (RULE E301)')
    expect(banner).toHaveTextContent('[⊘ 802.11 Wi-Fi: OFF]')
    expect(banner).toHaveTextContent('[⊘ Improv BLE: OFF]')
    expect(banner).toHaveTextContent('[✓ Shared NimBLE GATT: LIVE]')

    const strip = screen.getByTestId('pit-mini-fleet-strip')
    expect(within(strip).getByTestId('peer-mesh-skew')).toHaveTextContent('! SKEW')
    expect(within(strip).getByTestId('peer-mesh-skew')).toHaveTextContent('batteries_csv:v14 != v15')

    // Panel 3: Reported self-test outcome
    expect(screen.getByTestId('pit-selftest-checklist')).toHaveTextContent(
      'No self-test result reported for the selected board yet.',
    )

    // Panel 1: Insert Empty Skeleton -> Replay Dock Bundle to Server
    await userEvent.click(screen.getByTestId('dock-generate-sample'))
    await userEvent.click(screen.getByTestId('dock-sync-submit'))
    const receipt = await screen.findByTestId('dock-sync-receipt')
    expect(within(receipt).getByTestId('receipt-ingested-statuses')).toHaveTextContent('1')
    expect(within(receipt).getByTestId('receipt-ingested-telemetry')).toHaveTextContent('1')
    expect(within(receipt).getByTestId('receipt-ingested-logs')).toHaveTextContent('1')
    expect(within(receipt).getByTestId('receipt-corrupted-lines')).toHaveTextContent('0')
    expect(onSynced).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith(
      '/v1/devices/94a9900009a4/dock-sync',
      expect.objectContaining({ method: 'POST' }),
    )
  })
})
