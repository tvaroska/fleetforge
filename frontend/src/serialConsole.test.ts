// @vitest-environment node
import { afterEach, describe, expect, it, vi } from 'vitest'
import { BUILT_IN_PORT } from './flasher'
import { createSerialConsole } from './serialConsole'

type MockPortOptions = {
  info?: SerialPortInfo
  streams?: Array<Array<Uint8Array | Error>>
  openImpl?: (opts: { baudRate: number }) => Promise<void>
  closeImpl?: () => Promise<void>
}

function createMockPort({
  info = { usbVendorId: 0x303a, usbProductId: 0x1001 },
  streams = [],
  openImpl,
  closeImpl,
}: MockPortOptions = {}) {
  let streamIdx = 0
  let currentStream: ReadableStream<Uint8Array> | null | undefined = undefined
  let activeCancels = 0
  let releaseLocks = 0
  const signals: Array<Record<string, boolean>> = []
  const forget = vi.fn()
  const open = vi.fn(openImpl ?? (async () => {}))
  const close = vi.fn(closeImpl ?? (async () => {}))
  const setSignals = vi.fn(async (sig: Record<string, boolean>) => {
    signals.push(sig)
  })

  function buildNextStream(): ReadableStream<Uint8Array> | null {
    if (streamIdx >= streams.length) return null
    const chunks = streams[streamIdx++]!
    let chunkIdx = 0
    let cancelled = false
    return {
      getReader() {
        return {
          async read() {
            if (cancelled || chunkIdx >= chunks.length) {
              return { value: undefined, done: true as const }
            }
            const item = chunks[chunkIdx++]!
            if (item instanceof Error) {
              throw item
            }
            return { value: item, done: false as const }
          },
          async cancel() {
            cancelled = true
            activeCancels += 1
          },
          releaseLock() {
            releaseLocks += 1
            currentStream = undefined
          },
        }
      },
    } as unknown as ReadableStream<Uint8Array>
  }

  const port = {
    getInfo: () => info,
    open,
    close,
    forget,
    setSignals,
    get readable(): ReadableStream<Uint8Array> | null {
      if (currentStream === undefined) {
        currentStream = buildNextStream()
      }
      return currentStream
    },
  } as unknown as SerialPort

  return {
    port,
    open,
    close,
    forget,
    setSignals,
    signals,
    getCancels: () => activeCancels,
    getReleaseLocks: () => releaseLocks,
  }
}

function installNavigatorSerial(serial: Partial<Serial>) {
  Object.defineProperty(globalThis, 'navigator', {
    value: { serial },
    configurable: true,
    writable: true,
  })
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('createSerialConsole', () => {
  it('refuses a built-in port with no USB vendor id in prompt mode before opening', async () => {
    const mock = createMockPort({ info: {} })
    installNavigatorSerial({ requestPort: vi.fn(async () => mock.port) })

    await expect(
      createSerialConsole({ baudRate: 115200, acquire: 'prompt', onLog: vi.fn() }),
    ).rejects.toThrow(BUILT_IN_PORT)
    expect(mock.open).not.toHaveBeenCalled()
  })

  it('reassembles lines across arbitrary chunks, split UTF-8 characters, multiple readable epochs, and trailing partials', async () => {
    const encoder = new TextEncoder()
    // '✓' is 3 UTF-8 bytes (0xe2, 0x9c, 0x93); split across two chunks.
    const fullText = encoder.encode('boot line 1\ncheck ✓ ok\nsecond')
    const chunk1 = fullText.slice(0, 19) // splits right inside '✓'
    const chunk2 = fullText.slice(19)
    const chunk3 = encoder.encode(' epoch\ntrailing-no-newline')

    const mock = createMockPort({
      streams: [[chunk1, chunk2], [chunk3]],
    })
    installNavigatorSerial({ requestPort: vi.fn(async () => mock.port) })

    const session = await createSerialConsole({
      baudRate: 115200,
      acquire: 'prompt',
      onLog: vi.fn(),
    })
    expect(mock.open).toHaveBeenCalledWith({ baudRate: 115200 })

    const collected: string[] = []
    for await (const line of session.lines()) {
      collected.push(line)
    }

    expect(collected).toEqual([
      'boot line 1',
      'check ✓ ok',
      'second epoch',
      'trailing-no-newline',
    ])
    expect(mock.getReleaseLocks()).toBe(2)
  })

  it('logs a dropped USB serial link without crashing and releases the reader lock', async () => {
    const encoder = new TextEncoder()
    const mock = createMockPort({
      streams: [[encoder.encode('first line\n'), new Error('NetworkError: device lost')]],
    })
    installNavigatorSerial({ requestPort: vi.fn(async () => mock.port) })

    const logs: string[] = []
    const session = await createSerialConsole({
      baudRate: 115200,
      acquire: 'prompt',
      onLog: (line) => logs.push(line),
    })

    const collected: string[] = []
    for await (const line of session.lines()) {
      collected.push(line)
    }

    expect(collected).toEqual(['first line'])
    expect(logs.some((l) => l.startsWith('— the serial link dropped:'))).toBe(true)
    expect(mock.getReleaseLocks()).toBe(1)
  })

  it('pulses RTS with DTR held low on reboot and closes idempotently without forgetting the grant', async () => {
    vi.useFakeTimers()
    const mock = createMockPort({
      closeImpl: async () => {
        throw new Error('already closing')
      },
    })
    installNavigatorSerial({ requestPort: vi.fn(async () => mock.port) })
    const logs: string[] = []

    const session = await createSerialConsole({
      baudRate: 115200,
      acquire: 'prompt',
      onLog: (line) => logs.push(line),
    })

    const rebootDone = session.reboot()
    await vi.advanceTimersByTimeAsync(150)
    await rebootDone

    expect(mock.signals).toEqual([
      { dataTerminalReady: false, requestToSend: true },
      { dataTerminalReady: false, requestToSend: false },
    ])

    await session.close()
    await session.close()
    expect(mock.close).toHaveBeenCalledTimes(1)
    expect(mock.forget).not.toHaveBeenCalled()
    expect(logs.some((l) => l.startsWith('releasing the port:'))).toBe(true)
  })

  it('retries getPorts() in granted mode across USB re-enumeration and times out cleanly when absent', async () => {
    vi.useFakeTimers()
    const stalePort = createMockPort({
      openImpl: async () => {
        throw new Error('The port is already open.')
      },
    })
    const readyPort = createMockPort()
    let calls = 0
    installNavigatorSerial({
      getPorts: vi.fn(async () => {
        calls += 1
        if (calls === 1) return []
        if (calls === 2) return [stalePort.port]
        return [stalePort.port, readyPort.port]
      }),
    })

    const logs: string[] = []
    const acquiring = createSerialConsole({
      baudRate: 115200,
      acquire: 'granted',
      onLog: (line) => logs.push(line),
    })
    await vi.advanceTimersByTimeAsync(600)
    const session = await acquiring
    expect(calls).toBe(3)
    expect(logs.some((l) => l.includes('opened on try 3'))).toBe(true)
    await session.close()

    installNavigatorSerial({ getPorts: vi.fn(async () => []) })
    const failing = createSerialConsole({
      baudRate: 115200,
      acquire: 'granted',
      onLog: vi.fn(),
    })
    const assertion = expect(failing).rejects.toThrow(/No board is available to watch/)
    await vi.advanceTimersByTimeAsync(9000)
    await assertion
  })
})
