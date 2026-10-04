// What a failed flash WRITE says to the operator (R2b-fe-3). Pure: no React, no esptool-js.
//
// `spec/flows.md` Flow 1 step 4: "A write or verify failure says what failed and what to try
// first (another cable or port, a lower baud rate); only a failure that repeats on a good
// cable says the flash chip may be defective."
//
// Two things here are easy to undo:
//
// * **"Flash chip" appears only on a repeat at the lowest baud.** A first failure is almost
//   always the cable, the port or a USB-serial bridge that cannot keep up with 921600; a
//   page that blames the chip first sends the operator to throw away a good board. Same
//   rule as `DECISIONS.md` 2026-10-04 "Two claims weakened": name the observation, and make
//   only the claim the evidence supports.
// * **The connect-time BOOT advice is wrong here.** `explainFlashError` maps
//   `No serial data received` to "Hold BOOT while plugging it in", which is right when the
//   ROM never answered the sync. Mid-write the board is already in the stub loader and
//   has been answering for seconds, so that sentence sends the operator the wrong way.

/** What the write failure looked like from the outside. */
export type FlashFailureKind = 'lost' | 'no-answer' | 'rejected' | 'other'

export type FlashFailure = {
  kind: FlashFailureKind
  /** Label of the part being written when it failed, or null when no progress arrived. */
  part: string | null
  address: number | null
  /** Write failures for this board in this tab, including this one. */
  attempt: number
  baudRate: number
  /** Set only on a repeat at the lowest baud: the one place the flash chip is named. */
  chipSuspect: boolean
  /** The one plain-language cause. */
  cause: string
  /** The one thing to try first. */
  tryFirst: string
  /** The baud the action should retry at when it should lower it, else null. */
  retryBaud: number | null
}

/** The slowest rate the page offers; `FlashBoard`'s `BAUD_RATES` ends here. */
export const LOWEST_BAUD = 115200

/**
 * Sorts an esptool-js 0.6.1 write-path error into one of four kinds.
 *
 * Reads `name`/`message` off the value rather than `instanceof`, for the same reason
 * `explainFlashError` does: a `DOMException` is not an `Error` across realms.
 */
export function classifyWriteError(error: unknown): FlashFailureKind {
  const fault = (error ?? {}) as { name?: unknown; message?: unknown }
  const name = typeof fault.name === 'string' ? fault.name : ''
  const message = typeof fault.message === 'string' ? fault.message : String(error)

  // The USB device went away: cable knocked, board reset, hub dropped it.
  if (name === 'NetworkError' || /device has been lost/i.test(message)) return 'lost'
  // The stub answered, and said no: a block failed its checksum or status.
  if (
    /failed to write compressed data/i.test(message) ||
    /failed to leave compressed flash mode/i.test(message) ||
    /only got \d+ bytes/i.test(message) ||
    /md5 of file does not match/i.test(message)
  ) {
    return 'rejected'
  }
  // The stub stopped answering at all.
  if (
    /no serial data received/i.test(message) ||
    /serial data stream stopped/i.test(message) ||
    /timed out|timeout/i.test(message)
  ) {
    return 'no-answer'
  }
  return 'other'
}

const CAUSE_BY_KIND: Record<FlashFailureKind, string> = {
  lost: 'The board disconnected in the middle of the write.',
  'no-answer': 'The board stopped answering in the middle of the write.',
  rejected: 'The board reported that part of the write did not verify.',
  other: 'The write to the board failed partway.',
}

const CABLE_AND_PORT =
  'Use another USB cable (a short data cable, not a charge-only one) or another USB port ' +
  'directly on the computer (not a hub), then try again.'

export function describeWriteFailure(
  error: unknown,
  ctx: { attempt: number; baudRate: number; part: string | null; address: number | null },
): FlashFailure {
  const kind = classifyWriteError(error)
  const where =
    ctx.part !== null && ctx.address !== null
      ? ` (while writing ${ctx.part} at 0x${ctx.address.toString(16)})`
      : ctx.part !== null
        ? ` (while writing ${ctx.part})`
        : ''
  const cause = CAUSE_BY_KIND[kind].replace(/\.$/, `${where}.`)
  const lowest = ctx.baudRate <= LOWEST_BAUD

  let tryFirst: string
  let retryBaud: number | null = null
  let chipSuspect = false
  if (ctx.attempt <= 1) {
    tryFirst = lowest
      ? CABLE_AND_PORT
      : `${CABLE_AND_PORT} If it fails again, retry at ${LOWEST_BAUD}.`
  } else if (!lowest) {
    tryFirst =
      `It failed again on this board. Retry at ${LOWEST_BAUD}: some USB-serial chips, ` +
      'CH340 especially, do not survive 921600.'
    retryBaud = LOWEST_BAUD
  } else {
    chipSuspect = true
    tryFirst =
      `It has failed ${ctx.attempt} times on this board, the last at ${LOWEST_BAUD}. If ` +
      "another cable and port failed too, the board's flash chip may be defective."
  }

  return {
    kind,
    part: ctx.part,
    address: ctx.address,
    attempt: ctx.attempt,
    baudRate: ctx.baudRate,
    chipSuspect,
    cause,
    tryFirst,
    retryBaud,
  }
}
