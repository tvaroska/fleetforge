// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mockState = vi.hoisted(() => ({
  transportInstances: [] as Array<{
    port: unknown
    tracing: boolean
    disconnect: ReturnType<typeof vi.fn>
  }>,
  loaderInstances: [] as Array<{
    options: Record<string, unknown>
    chip: {
      CHIP_NAME: string
      readMac: ReturnType<typeof vi.fn>
      getChipFeatures: ReturnType<typeof vi.fn>
    }
    main: ReturnType<typeof vi.fn>
    detectFlashSize: ReturnType<typeof vi.fn>
    flashSizeBytes: ReturnType<typeof vi.fn>
    writeFlash: ReturnType<typeof vi.fn>
    after: ReturnType<typeof vi.fn>
  }>,
}))

vi.mock('esptool-js', () => {
  class MockTransport {
    port: unknown
    tracing: boolean
    disconnect = vi.fn(async () => {})

    constructor(port: unknown, tracing = false) {
      this.port = port
      this.tracing = tracing
      mockState.transportInstances.push(this)
    }
  }

  class MockESPLoader {
    options: Record<string, unknown>
    chip = {
      CHIP_NAME: 'ESP32-S3',
      readMac: vi.fn(async () => 'aa:bb:cc:dd:ee:ff'),
      getChipFeatures: vi.fn(async () => ['Wi-Fi', 'BLE', 'Embedded PSRAM 8MB']),
    }
    main = vi.fn(async () => 'ESP32-S3 (QFN56) (revision v0.2)')
    detectFlashSize = vi.fn(async () => '8MB')
    flashSizeBytes = vi.fn((label: string) => (label === '8MB' ? 8 * 1024 * 1024 : 4 * 1024 * 1024))
    writeFlash = vi.fn(async () => {})
    after = vi.fn(async () => {})

    constructor(options: Record<string, unknown>) {
      this.options = options
      mockState.loaderInstances.push(this)
    }
  }

  return { Transport: MockTransport, ESPLoader: MockESPLoader }
})

import { createEsptoolFlasher } from './esptoolFlasher'
import { BUILT_IN_PORT } from './flasher'

function installMockSerial(portInfo: SerialPortInfo = { usbVendorId: 0x303a, usbProductId: 0x1001 }) {
  const forget = vi.fn()
  const port = {
    getInfo: () => portInfo,
    forget,
  } as unknown as SerialPort
  const requestPort = vi.fn(async () => port)
  Object.defineProperty(globalThis, 'navigator', {
    value: { serial: { requestPort } },
    configurable: true,
    writable: true,
  })
  return { port, requestPort, forget }
}

beforeEach(() => {
  mockState.transportInstances.length = 0
  mockState.loaderInstances.length = 0
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('createEsptoolFlasher', () => {
  it('requests a port, rejects built-in non-USB ports before constructing Transport', async () => {
    installMockSerial({})
    const onLog = vi.fn()

    await expect(createEsptoolFlasher({ baudRate: 921600, onLog })).rejects.toThrow(BUILT_IN_PORT)
    expect(mockState.transportInstances).toHaveLength(0)
    expect(mockState.loaderInstances).toHaveLength(0)
  })

  it('detects chip metadata and gracefully tolerates a failed MAC read', async () => {
    installMockSerial()
    const logs: string[] = []
    const flasher = await createEsptoolFlasher({
      baudRate: 921600,
      onLog: (line) => logs.push(line),
    })

    const loader = mockState.loaderInstances[0]!
    expect(mockState.transportInstances[0]?.tracing).toBe(false)
    expect(loader.options.baudrate).toBe(921600)

    const info = await flasher.detect()
    expect(info).toEqual({
      chipName: 'ESP32-S3',
      description: 'ESP32-S3 (QFN56) (revision v0.2)',
      macAddress: 'aa:bb:cc:dd:ee:ff',
      flashSizeBytes: 8 * 1024 * 1024,
      features: ['Wi-Fi', 'BLE', 'Embedded PSRAM 8MB'],
    })

    loader.chip.readMac.mockRejectedValueOnce(new Error('efuse read timeout'))
    const noMac = await flasher.detect()
    expect(noMac.macAddress).toBeNull()
    expect(logs.some((l) => l.includes('could not read the MAC'))).toBe(true)
  })

  it('writes parts with keep flash flags, eraseAll: false, compress: true, and progress callback', async () => {
    installMockSerial()
    const onLog = vi.fn()
    const flasher = await createEsptoolFlasher({ baudRate: 460800, onLog })
    const loader = mockState.loaderInstances[0]!

    const progressCalls: Array<[number, number, number]> = []
    loader.writeFlash.mockImplementationOnce(async (opts: Record<string, unknown>) => {
      const cb = opts.reportProgress as (idx: number, written: number, total: number) => void
      cb(0, 512, 1024)
      cb(0, 1024, 1024)
    })

    const bootloader = new Uint8Array([1, 2, 3, 4])
    const app = new Uint8Array([5, 6, 7, 8])
    await flasher.write(
      [
        { label: 'bootloader', address: 0x0, data: bootloader },
        { label: 'app', address: 0x20000, data: app },
      ],
      {
        onProgress: (fileIndex, written, total) => {
          progressCalls.push([fileIndex, written, total])
        },
      },
    )

    expect(loader.writeFlash).toHaveBeenCalledTimes(1)
    expect(loader.writeFlash).toHaveBeenCalledWith(
      expect.objectContaining({
        fileArray: [
          { address: 0x0, data: bootloader },
          { address: 0x20000, data: app },
        ],
        flashMode: 'keep',
        flashFreq: 'keep',
        flashSize: 'keep',
        eraseAll: false,
        compress: true,
      }),
    )
    expect(progressCalls).toEqual([
      [0, 512, 1024],
      [0, 1024, 1024],
    ])
  })

  it('propagates writeFlash errors to the caller so token revocation can run', async () => {
    installMockSerial()
    const flasher = await createEsptoolFlasher({ baudRate: 921600, onLog: vi.fn() })
    const loader = mockState.loaderInstances[0]!
    loader.writeFlash.mockRejectedValueOnce(new Error('Invalid head of packet (0x50)'))

    await expect(
      flasher.write([{ label: 'app', address: 0x20000, data: new Uint8Array([1]) }], {
        onProgress: vi.fn(),
      }),
    ).rejects.toThrow(/Invalid head of packet/)
  })

  it('absorbs native-USB hard_reset disconnect errors on finish and disconnects transport once without forgetting permission', async () => {
    const { forget } = installMockSerial()
    const logs: string[] = []
    const flasher = await createEsptoolFlasher({
      baudRate: 921600,
      onLog: (line) => logs.push(line),
    })
    const loader = mockState.loaderInstances[0]!
    const transport = mockState.transportInstances[0]!
    loader.after.mockRejectedValueOnce(new Error('The device has been lost.'))

    await flasher.finish()
    await flasher.close()

    expect(loader.after).toHaveBeenCalledWith('hard_reset')
    expect(logs.some((l) => l.includes('the image is already written'))).toBe(true)
    expect(transport.disconnect).toHaveBeenCalledTimes(1)
    expect(forget).not.toHaveBeenCalled()
  })

  it('logs rather than throwing when transport.disconnect fails during close', async () => {
    installMockSerial()
    const logs: string[] = []
    const flasher = await createEsptoolFlasher({
      baudRate: 921600,
      onLog: (line) => logs.push(line),
    })
    const transport = mockState.transportInstances[0]!
    transport.disconnect.mockRejectedValueOnce(new Error('port already closed'))

    await expect(flasher.close()).resolves.toBeUndefined()
    expect(logs.some((l) => l.includes('releasing the port:'))).toBe(true)
  })
})
