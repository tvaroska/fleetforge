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
  /*
   * There is deliberately no `eraseAll` here, and adding it back would put a whole-chip
   * erase one boolean away. A chip-wide erase destroys the cached RF calibration, which
   * lives in NVS under IDF's `phy` namespace — a board that loses it re-runs the cold full
   * calibration, the largest current draw in startup, on every boot.
   *
   * Nothing in the write plan is allowed to touch NVS either: `flash.ts` erases nothing at
   * all now and asserts it (`assertLeavesNvsAlone`). Clearing the stored broker credential
   * is the agent's job — `ff_store_sync_token` drops the `ff` namespace when the `ff_cfg`
   * token changes (S0-fw-4).
   */
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
 * What a dismissed chooser reads as. Exported because the view treats it as a cue, not
 * only as a message: an operator whose board has no COM port opens the chooser, sees
 * nothing that is theirs, and closes it — so this is the moment to open "My board isn't
 * listed" (S0-fe-8).
 */
export const NO_BOARD_SELECTED = 'No board selected.'

/**
 * USB vendor ids of what an ESP32 board actually presents. Everything an ESP32 can be
 * reached through is one of these: the chip's own USB (C3/C6/S3) or the bridge chip on
 * the board. A port with an id outside this table is unusual, not wrong — a board on a
 * Prolific or CH9102-clone bridge still flashes.
 */
/** `checkChosenPort`'s refusal. Exported for the same reason as `NO_BOARD_SELECTED`. */
export const BUILT_IN_PORT =
  'That port is built into the computer (COM1 on most Windows PCs), not a USB board. ' +
  "If it was the only one listed, the board has no port yet — see “My board isn't listed”."

const USB_SERIAL_VENDORS: Record<number, string> = {
  0x303a: 'Espressif native USB (no driver needed)',
  0x10c4: 'Silicon Labs CP210x bridge',
  0x1a86: 'WCH CH340/CH9102 bridge',
  0x0403: 'FTDI bridge',
}

const hex4 = (value: number): string => value.toString(16).padStart(4, '0')

/**
 * Stops the one choice that is plainly not an ESP32, before any handshake. S0-fe-8.
 *
 * A port with NO USB vendor id is built into the computer: the motherboard's `COM1` on
 * Windows, `ttyS0` on Linux. On a Windows box with no bridge driver it is the only entry
 * the chooser lists, so it is what a stuck operator picks — and esptool would then spend
 * its whole sync window on it and report "the board did not answer", which sends them to
 * the BOOT button for a board that was never on the line.
 *
 * Anything with a USB id goes through; a known vendor is named in the log so the
 * bridge chip is on record next to whatever happens next.
 */
export function checkChosenPort(info: SerialPortInfo, onLog: (line: string) => void): void {
  const vendor = info.usbVendorId
  if (vendor === undefined) {
    throw new Error(BUILT_IN_PORT)
  }
  const product = info.usbProductId === undefined ? '' : `:${hex4(info.usbProductId)}`
  const known = USB_SERIAL_VENDORS[vendor]
  onLog(
    known === undefined
      ? `port: USB ${hex4(vendor)}${product} — not a USB-serial chip this page recognises; trying it anyway`
      : `port: ${known} (USB ${hex4(vendor)}${product})`,
  )
}

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
  if (name === 'NotFoundError') return NO_BOARD_SELECTED
  // S0-fe-6. Chromium's wording when the transient user activation window has closed —
  // the recovery click awaits `release()` before `requestPort()`, and a slow release can
  // outlive it. It is not a security misconfiguration, and telling the operator to check
  // HTTPS sends them nowhere. Must stay ABOVE the `SecurityError` branch: this is thrown
  // as a `SecurityError` too.
  if (/user gesture/i.test(message)) {
    return 'The browser needs a fresh click to open the port chooser. Press the button again.'
  }
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
