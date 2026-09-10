// The only file that opens a serial port for the CONSOLE. (`esptoolFlasher.ts` is the only
// one that opens a port for flashing.) Nothing here is exercised in jsdom — there is no
// `navigator.serial` and no board — so everything worth testing lives in `boardConsole.ts`.

import type { BoardConsole, ConsoleAcquire, ConsoleFactory } from './boardConsole'
import { explainFlashError } from './flasher'

/**
 * How long to keep trying to open a port after a flash.
 *
 * On the native-USB parts (C3/C6/S3 over USB-JTAG-Serial) `hard_reset` makes the device
 * drop off the bus and come back as a NEW `SerialPort`; see the comment in
 * `esptoolFlasher.ts:83`. For a second or two `getPorts()` either omits it or lists a
 * handle that throws on `open()`. Giving up in that window would tell the operator their
 * board is dead when it is merely rebooting.
 */
const ACQUIRE_TIMEOUT_MS = 8000
const ACQUIRE_RETRY_MS = 250

/** Long enough for EN to actually fall. esptool uses 100 ms; be generous, it happens once. */
const RESET_PULSE_MS = 150

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

class SerialConsole implements BoardConsole {
  private closed = false
  private reader: ReadableStreamDefaultReader<Uint8Array> | null = null

  constructor(
    private readonly port: SerialPort,
    private readonly onLog: (line: string) => void,
  ) {}

  async *lines(): AsyncGenerator<string> {
    const decoder = new TextDecoder()
    let partial = ''

    // The outer loop exists because a `readable` can end without the port closing (a USB
    // hiccup, a break). The agent will still be talking afterwards.
    while (!this.closed && this.port.readable !== null) {
      const reader = this.port.readable.getReader()
      this.reader = reader
      try {
        for (;;) {
          const { value, done } = await reader.read()
          if (done) break
          partial += decoder.decode(value, { stream: true })
          const parts = partial.split('\n')
          // The last element is whatever came after the final newline — a line still being
          // typed by the board. Hold it; emitting it would split messages mid-word.
          partial = parts.pop() ?? ''
          for (const line of parts) yield line
        }
      } catch (err) {
        // A yanked cable lands here. It is information, not a crash.
        this.onLog(`— the serial link dropped: ${explainFlashError(err)}`)
        break
      } finally {
        try {
          reader.releaseLock()
        } catch {
          // Already released by close(); nothing to do.
        }
        this.reader = null
      }
    }

    if (partial !== '') yield partial
  }

  /**
   * Pulse EN.
   *
   * The dev-board wiring is the standard auto-reset circuit: DTR and RTS drive EN and IO0
   * through two transistors, and EN is asserted when RTS is high while DTR is low. Leaving
   * DTR low the whole time keeps IO0 high, so the board boots the application rather than
   * the ROM bootloader — the operator wants a boot log, not a download prompt.
   */
  async reboot(): Promise<void> {
    await this.port.setSignals({ dataTerminalReady: false, requestToSend: true })
    await sleep(RESET_PULSE_MS)
    await this.port.setSignals({ dataTerminalReady: false, requestToSend: false })
  }

  async close(): Promise<void> {
    if (this.closed) return
    this.closed = true
    try {
      // Cancelling wakes the pending `read()` so `port.close()` is not rejected for a
      // locked stream. Without this the OS device stays ours and `screen` gets
      // "Resource busy" — which is exactly what the release button exists to prevent.
      await this.reader?.cancel()
    } catch {
      // The reader may already be gone; the close below is what matters.
    }
    try {
      await this.port.close()
    } catch (err) {
      this.onLog(`releasing the port: ${explainFlashError(err)}`)
    }
    // NEVER `port.forget()`. Same rule as the flasher: revoking the grant means the next
    // board — or the next click of Watch — needs another trip through the chooser.
  }
}

/**
 * `'prompt'` shows the chooser and MUST be called synchronously from a click.
 * `'granted'` reuses a permission the operator already gave (which is why nothing ever
 * calls `forget()`), so it can run from an effect after a flash with no gesture at all.
 */
async function acquirePort(acquire: ConsoleAcquire, baudRate: number): Promise<SerialPort> {
  if (acquire === 'prompt') {
    const port = await navigator.serial.requestPort()
    await openWithRetry([port], baudRate)
    return port
  }

  const deadline = Date.now() + ACQUIRE_TIMEOUT_MS
  let last: unknown = null
  for (;;) {
    // Re-read the list every pass: a re-enumerating board appears as a different entry.
    const ports = await navigator.serial.getPorts()
    const opened = await openWithRetry(ports, baudRate).catch((err: unknown) => {
      last = err
      return null
    })
    if (opened !== null) return opened
    if (Date.now() >= deadline) {
      throw last instanceof Error
        ? last
        : new Error(
            'No board is available to watch. Plug it back in, then use “Watch a board” ' +
              'to pick the port.',
          )
    }
    await sleep(ACQUIRE_RETRY_MS)
  }
}

/** Opens the first port that will have us. Returns null-free: throws if none opens. */
async function openWithRetry(ports: SerialPort[], baudRate: number): Promise<SerialPort> {
  let last: unknown = new Error('No board is available to watch.')
  for (const port of ports) {
    try {
      await port.open({ baudRate })
      return port
    } catch (err) {
      // "already open" means this handle is the one esptool still holds, or another tab
      // has it. Either way it is not ours; try the next.
      last = err
    }
  }
  throw last
}

export const createSerialConsole: ConsoleFactory = async ({ baudRate, acquire, onLog }) => {
  const port = await acquirePort(acquire, baudRate)
  return new SerialConsole(port, onLog)
}
