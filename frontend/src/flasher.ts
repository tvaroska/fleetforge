// The seam between "what flashing a board means" and "esptool-js over Web Serial".
//
// Same idiom and the same reason as `EventSourceFactory` in `fleet.ts`: jsdom has no
// `navigator.serial` at all, so the engine has to take its flasher as an argument rather
// than reach for a global. Everything here is structural — a test double needs no DOM,
// and the real adapter satisfies these types without a cast.
//
// **Nothing in this file imports esptool-js.** `src/esptoolFlasher.ts` is the only file
// in the app that does, and the only one that touches `navigator.serial`.

export type ChipInfo = {
  /** `ESPLoader.chip.CHIP_NAME`, which is EXACTLY the manifest's `chip_family`. */
  chipName: string
  /** `ESPLoader.main()`'s return value — chip name plus revision, for display. */
  description: string
  /** `aa:bb:cc:dd:ee:ff`, or null if the ROM would not say. */
  macAddress: string | null
  /** From `detectFlashSize()`. The check that stops a 2 MB board getting an A/B layout. */
  flashSizeBytes: number
  features: string[]
}

/** One write: bytes at an address that came out of the manifest, never out of a constant. */
export type FlashPart = { label: string; address: number; data: Uint8Array }

export type WriteOptions = {
  /**
   * Erase before writing. Default ON in the UI: re-flashing a board whose NVS still holds
   * a broker credential does NOT re-enroll it (R0-fw-1 logs "reusing the stored
   * credential"), so a freshly minted token baked into it is simply never spent.
   */
  eraseAll: boolean
  /** esptool-js reports COMPRESSED bytes when `compress: true` — `total` is not `part.size`. */
  onProgress: (partIndex: number, written: number, total: number) => void
}

export interface BoardFlasher {
  detect(): Promise<ChipInfo>
  write(parts: FlashPart[], options: WriteOptions): Promise<void>
  /** Hard reset so the board runs what was just written, then release the port. */
  finish(): Promise<void>
  /** Idempotent; safe in a `finally`. */
  close(): Promise<void>
}

/**
 * Requests the port and connects.
 *
 * MUST be called synchronously from a click handler: `navigator.serial.requestPort()`
 * needs a live user gesture, and any `await` before it spends one.
 */
export type FlasherFactory = (options: {
  baudRate: number
  onLog: (line: string) => void
}) => Promise<BoardFlasher>

export const webSerialSupported = (): boolean =>
  typeof navigator !== 'undefined' && 'serial' in navigator

/**
 * Web Serial and esptool-js describe the protocol. This describes the bench.
 *
 * It lives HERE, not in `esptoolFlasher.ts`, because the most common failure of all —
 * the operator dismissing the port chooser — is thrown by `requestPort()` before the
 * adapter object exists, and is therefore caught by the engine (`flash.ts`), which must
 * not import esptool-js. This function touches nothing but `Error` fields.
 *
 * An unrecognised failure is returned verbatim rather than swallowed; the raw esptool
 * output is in the log panel either way.
 *
 * It reads `name`/`message` off the value instead of testing `instanceof Error`: the one
 * throw that matters most is a `DOMException`, which is an `Error` in Chromium but NOT
 * across realms (jsdom's, for one) — and an `instanceof` that is false there would send
 * the commonest failure of all down the "unknown fault" path.
 */
export function explainFlashError(error: unknown): string {
  const fault = (error ?? {}) as { name?: unknown; message?: unknown }
  const name = typeof fault.name === 'string' ? fault.name : ''
  const message = typeof fault.message === 'string' ? fault.message : String(error)

  // Chromium throws NotFoundError both when no port matches and when the operator
  // dismisses the chooser, which is by far the common case.
  if (name === 'NotFoundError') return 'No board selected.'
  if (name === 'SecurityError') {
    return 'The browser blocked serial access; the page must be on HTTPS or localhost.'
  }
  if (name === 'InvalidStateError' || /failed to open serial port/i.test(message)) {
    return 'The port is already open — close any serial monitor and try again.'
  }
  if (/no serial data received/i.test(message)) {
    return (
      'The board did not answer. Hold BOOT while plugging it in, or try a slower baud rate ' +
      '(115200 — some USB-serial chips, CH340 especially, do not survive 921600).'
    )
  }
  if (/timed out|timeout/i.test(message)) {
    return `${message} — try 115200; some USB-serial chips do not survive 921600.`
  }
  return message
}

/**
 * The real adapter, loaded on demand.
 *
 * The dynamic import is deliberate: esptool-js pulls in pako and the ROM stubs, and a
 * Firefox or Safari visitor who can never flash anything should never download them.
 */
export const defaultFlasherFactory: FlasherFactory = async (options) =>
  (await import('./esptoolFlasher')).createEsptoolFlasher(options)
