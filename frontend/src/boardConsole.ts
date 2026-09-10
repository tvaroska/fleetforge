// The second half of "flash a board and know what happened".
//
// `flash.ts` stops at `hard_reset`, and its Rule 4 — the port is always released — stays
// exactly as it is: esptool-js's `Transport` owns the port at the FLASH baud, which is not
// the console baud, and keeping a flasher alive past its `finally` would leak a port on
// every error path. So the console is a SECOND, independent session against the same
// physical port, opened at 115200 after the flasher has let go.
//
// Split the same way as `flash.ts` / `esptoolFlasher.ts`:
//   * this file is pure plus React, and knows nothing about `navigator.serial`;
//   * `serialConsole.ts` is the only file that opens a port for the console.
// jsdom has neither Web Serial nor a board, so the transport arrives as an argument.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

/** Same cap and same reason as `MAX_LOG_LINES` in `flash.ts`: a board left watched for an
 *  hour must not grow the DOM without bound. The interesting lines are the recent ones. */
export const MAX_CONSOLE_LINES = 500

/** ESP-IDF's default, and the agent does not override it — `agent/sdkconfig.defaults`
 *  carries no `CONFIG_ESP_CONSOLE_UART_BAUDRATE`. Flashing runs at 921600; reading the
 *  console at 921600 produces plausible-looking garbage, which is worse than nothing. */
export const CONSOLE_BAUD_RATE = 115200

export type ConsoleLevel = 'error' | 'warn' | 'info' | 'plain'

/**
 * The boot the operator is waiting on, in order. Each one is a thing that can be true or
 * not true about a board, and the first one that never becomes true IS the diagnosis.
 */
export const MILESTONES = ['boot', 'link', 'clock', 'enroll', 'fleet'] as const
export type Milestone = (typeof MILESTONES)[number]

export const MILESTONE_LABELS: Record<Milestone, string> = {
  boot: 'Agent running',
  link: 'Network up',
  clock: 'Clock set',
  enroll: 'Enrolled',
  fleet: 'On the fleet',
}

export type ConsoleEvent = {
  /** Monotonic within a session; React keys, and nothing else. */
  seq: number
  /** The line as it came off the wire, ANSI stripped. Always shown. */
  raw: string
  level: ConsoleLevel
  /** `ff-wifi`, `ff-enroll`, … or null for boot-ROM chatter that is not an IDF log line. */
  tag: string | null
  /** The message without the `I (1234) ff-wifi: ` preamble. */
  text: string
  /** Reaching this proves a step of the boot happened. */
  milestone: Milestone | null
  /** Plain English for a line that explains a stall. This is the "name the cause" bit. */
  hint: string | null
  /**
   * How specific `hint` is.
   *
   * `'generic'` is the "still waiting, this is normal for now" note that `agent_main.c`
   * reprints every 5 s. It must never displace a `'specific'` diagnosis: the retry
   * heartbeat is always the LAST line on a stranded board, so recency alone would bury
   * "no AP with that SSID" under "the agent retries every 5 s" — which is true, useless,
   * and exactly the silence this panel exists to break.
   */
  hintKind: 'generic' | 'specific'
}

export type ConsoleSummary = {
  reached: Milestone[]
  /** The next thing that has not happened yet, or null once the board is on the fleet. */
  waitingFor: Milestone | null
  /**
   * The most recent explained failure that no later milestone has invalidated. A board
   * that recovers on its own clears its own fault, which is why this is folded over the
   * whole event list rather than latched on the first error.
   */
  fault: { text: string; hint: string } | null
}

/**
 * `esp_wifi`'s `wifi_err_reason_t`, for the handful that actually reach a bench.
 *
 * These are the numbers behind "the board just doesn't connect". The agent logs the code
 * and nothing else (`ff_net_wifi.c:51`), and nobody has the table memorised.
 */
const WIFI_REASONS: Record<number, string> = {
  2: 'the Wi-Fi password (PSK) is wrong, or the AP aged this board out',
  4: 'the AP dropped an idle association',
  15: 'the Wi-Fi password (PSK) is wrong — the 4-way handshake timed out',
  200: 'the AP stopped answering — weak signal, or it moved channel',
  201:
    'no access point with that SSID is in range. Check the spelling, and check the ' +
    'network has a 2.4 GHz band: the ESP32 radio cannot see 5 GHz at all',
  202: 'the AP rejected the credentials — check the password',
  203: 'the AP refused the association',
  204: 'the Wi-Fi password (PSK) is wrong — the handshake timed out',
  205: 'the association failed; the AP may be at its client limit',
}

/** IDF colours its log lines when CONFIG_LOG_COLORS is on: `ESC[0;32mI (12) …`. */
const ANSI = /\u001b\[[0-9;]*[A-Za-z]/g
/** `I (1234) ff-wifi: associated; waiting for DHCP` — the ESP-IDF log format. */
const LOG_LINE = /^([IWED])\s+\((\d+)\)\s+([A-Za-z0-9_.-]+):\s?(.*)$/

const LEVELS: Record<string, ConsoleLevel> = { E: 'error', W: 'warn', I: 'info', D: 'info' }

/**
 * One line in, one classified event out. Pure, and the only place that knows what the
 * agent's log lines look like — every string matched here exists in `agent/main/*.c`.
 */
export function classifyConsoleLine(raw: string, seq: number): ConsoleEvent {
  const clean = raw.replace(ANSI, '').replace(/\r/g, '')
  const match = LOG_LINE.exec(clean)
  const level = match === null ? 'plain' : (LEVELS[match[1]] ?? 'info')
  const tag = match === null ? null : match[3]
  const text = match === null ? clean : match[4]

  const hint = hintFor(tag, text)
  return {
    seq,
    raw: clean,
    level,
    tag,
    text,
    milestone: milestoneFor(tag, text),
    hint: hint?.text ?? null,
    hintKind: hint?.kind ?? 'specific',
  }
}

type Hint = { text: string; kind: 'generic' | 'specific' }
const specific = (text: string): Hint => ({ text, kind: 'specific' })

function milestoneFor(tag: string | null, text: string): Milestone | null {
  if (tag === 'ff-agent' && text.startsWith('fleetforge agent ')) return 'boot'
  // `ff_net.c:29` — "wifi link up, ip 192.168.1.40 gw …". DHCP done, not merely associated.
  if (tag === 'ff-net' && / link up/.test(text)) return 'link'
  // `ff_time.c:82` — "sntp: <before> -> <after> (via pool.ntp.org)". The arrow only
  // appears when the clock actually moved; the timeout branch logs a warning instead.
  if (tag === 'ff-time' && /^sntp: .* -> /.test(text)) return 'clock'
  if (tag === 'ff-enroll' && /^enroll 200\b/.test(text)) return 'enroll'
  if (tag === 'ff-mqtt' && text.startsWith('mqtt connected as ')) return 'fleet'
  return null
}

function hintFor(tag: string | null, text: string): Hint | null {
  if (tag === 'ff-agent' && text.startsWith('halted:')) {
    return specific(
      'The agent gave up on purpose. It will not retry until the board is re-flashed.',
    )
  }

  if (tag === 'ff-wifi') {
    const reason = /^disconnected \(reason (-?\d+)\)/.exec(text)
    if (reason !== null) {
      const code = Number(reason[1])
      return specific(
        WIFI_REASONS[code] ?? `the AP dropped this board (esp_wifi reason ${code})`,
      )
    }
    if (text.includes('carries no ssid')) {
      return specific(
        'This board was flashed with link=wifi but no SSID. Re-flash it with one.',
      )
    }
  }

  if (tag === 'ff-net' && text.startsWith('no IP address after')) {
    return specific(
      'Associated, but DHCP never answered. The AP may be isolating this client.',
    )
  }

  if (tag === 'ff-time') {
    if (text.startsWith('sntp: no answer')) {
      return specific(
        'The clock is still unset, so TLS cannot check a certificate: enrolment over ' +
          'https:// and mqtts:// will fail. The network may be blocking UDP/123.',
      )
    }
    if (text.includes('no ntp server configured')) {
      return specific(
        'No NTP server in ff_cfg. Any https:// or mqtts:// URI will fail to verify.',
      )
    }
    if (text.includes('the clock is still before')) {
      return specific('SNTP answered with a nonsense time. TLS will fail.')
    }
  }

  if (tag === 'ff-enroll') {
    if (/^enroll 401\b/.test(text)) {
      return specific(
        'The server does not know this ffe_ token. Flash the board again to mint a new one.',
      )
    }
    if (/^enroll 409\b/.test(text)) {
      return specific(
        'This token was already spent, revoked or has expired. Tokens are single-use: ' +
          'flash the board again to mint a fresh one.',
      )
    }
    if (/^enroll 429\b/.test(text)) {
      return specific(
        'Rate limited. The limiter counts failures, so something before this is wrong too.',
      )
    }
    if (/^enroll 503\b/.test(text)) {
      return specific(
        'The broker would not provision this device. This is a server-side fault, not the board.',
      )
    }
    if (text.startsWith('cannot reach ')) {
      return specific(
        'The board has a network but could not reach the API. If the clock milestone is ' +
          'still open this is a TLS failure caused by the unset clock, not a routing problem.',
      )
    }
    if (text.includes('carries no enrollment')) {
      return specific(
        'This board holds no credential and no ffe_ token. It can never join; re-flash it.',
      )
    }
  }

  if (tag === 'ff-mqtt' && text.startsWith('broker refused the connection')) {
    return specific(
      'The stored credential is no longer valid — the device was probably deleted or ' +
        'reset on the server. Re-flash the board to enrol it again.',
    )
  }

  // `agent_main.c:167`, once every 5 s, forever. Generic on purpose: on a board with a bad
  // SSID this is the last line printed, and it must not bury the reason code above it.
  if (tag === 'ff-agent' && text.startsWith('no network yet')) {
    return {
      text: 'The link has not come up yet. The agent retries every 5 s and never gives up.',
      kind: 'generic',
    }
  }

  return null
}

/** Fold the events into the checklist and the current fault. Pure; the UI renders this. */
export function summarizeConsole(events: ConsoleEvent[]): ConsoleSummary {
  const reached = new Set<Milestone>()
  let fault: { text: string; hint: string } | null = null
  let faultKind: 'generic' | 'specific' = 'generic'

  for (const event of events) {
    if (event.milestone !== null) {
      reached.add(event.milestone)
      // Progress clears the previous complaint: a board that reconnects after three bad
      // handshakes is fine, and leaving "the PSK is wrong" on screen would be a lie.
      fault = null
      faultKind = 'generic'
      continue
    }
    if (event.hint === null) continue
    if (event.level !== 'warn' && event.level !== 'error') continue
    // A generic note fills an empty slot but never overwrites a named cause. See
    // `ConsoleEvent.hintKind` — the retry heartbeat is always the newest line.
    if (event.hintKind === 'generic' && faultKind === 'specific') continue
    fault = { text: event.text, hint: event.hint }
    faultKind = event.hintKind
  }

  const ordered = MILESTONES.filter((m) => reached.has(m))
  const waitingFor = MILESTONES.find((m) => !reached.has(m)) ?? null
  return { reached: ordered, waitingFor, fault }
}

/** How the console gets hold of a port. See `serialConsole.ts` for what each one costs. */
export type ConsoleAcquire =
  /** Reuse a permission the operator already granted. No user gesture needed — the flasher
   *  never calls `port.forget()`, so the port it just used is still listed. */
  | 'granted'
  /** Show the chooser. MUST be called synchronously from a click, like `requestPort()`. */
  | 'prompt'

export interface BoardConsole {
  /**
   * Decoded lines, until the port closes or `close()` is called.
   *
   * There is deliberately NO inactivity timeout. `agent_main.c:166` retries the link every
   * 5 s forever, and the whole point of this panel is to be watching when the interesting
   * line finally arrives — ten minutes in, if that is how long the operator waits.
   */
  lines(): AsyncIterable<string>
  /** Pulse EN so the board boots again, to catch a log from its first line. */
  reboot(): Promise<void>
  /** Idempotent. Releases the OS device so `screen`/`esptool.py` can have it. */
  close(): Promise<void>
}

export type ConsoleFactory = (options: {
  baudRate: number
  acquire: ConsoleAcquire
  onLog: (line: string) => void
}) => Promise<BoardConsole>

export const defaultConsoleFactory: ConsoleFactory = async (options) =>
  (await import('./serialConsole')).createSerialConsole(options)

export type BoardConsoleState = {
  /** A port is open and being read. */
  watching: boolean
  /** Acquiring the port — between the click and the first byte. */
  opening: boolean
  events: ConsoleEvent[]
  summary: ConsoleSummary
  /** Why the console could not be opened, or why it stopped. Not a board fault. */
  error: string | null
  watch: (acquire: ConsoleAcquire) => Promise<void>
  release: () => Promise<void>
  reboot: () => Promise<void>
  clear: () => void
}

/**
 * Exactly one open port at a time.
 *
 * Web Serial allows one reader per port, and the OS allows one owner per tty. A second
 * `watch()` that did not first release would fail with "the port is already open" and,
 * worse, would leave the first session holding the device against `screen`.
 */
export function useBoardConsole({
  createConsole,
  explainError,
}: {
  createConsole?: ConsoleFactory
  explainError: (error: unknown) => string
}): BoardConsoleState {
  const [events, setEvents] = useState<ConsoleEvent[]>([])
  const summary = useMemo(() => summarizeConsole(events), [events])
  const [watching, setWatching] = useState(false)
  const [opening, setOpening] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const sessionRef = useRef<BoardConsole | null>(null)
  /** Bumped on every `watch()`/`release()`; a stale generator's lines are dropped. */
  const runRef = useRef(0)
  const seqRef = useRef(0)

  const factoryRef = useRef(createConsole ?? defaultConsoleFactory)
  factoryRef.current = createConsole ?? defaultConsoleFactory

  const append = useCallback((raw: string) => {
    const event = classifyConsoleLine(raw, seqRef.current++)
    setEvents((current) => {
      const next = current.length >= MAX_CONSOLE_LINES ? current.slice(1) : current.slice()
      next.push(event)
      return next
    })
  }, [])

  const stop = useCallback(async () => {
    runRef.current += 1
    const session = sessionRef.current
    sessionRef.current = null
    setWatching(false)
    if (session !== null) await session.close()
  }, [])

  const watch = useCallback(
    async (acquire: ConsoleAcquire) => {
      await stop()
      const run = runRef.current
      setError(null)
      setOpening(true)
      let session: BoardConsole
      try {
        session = await factoryRef.current({
          baudRate: CONSOLE_BAUD_RATE,
          acquire,
          onLog: append,
        })
      } catch (err) {
        if (run === runRef.current) {
          setError(explainError(err))
          setOpening(false)
        }
        return
      }
      if (run !== runRef.current) {
        // Released while the chooser was up. Do not start reading a port nobody wants.
        await session.close()
        return
      }
      sessionRef.current = session
      setOpening(false)
      setWatching(true)
      try {
        for await (const line of session.lines()) {
          if (run !== runRef.current) break
          append(line)
        }
        if (run === runRef.current) {
          setError('The board disconnected. Plug it back in and watch it again.')
        }
      } catch (err) {
        if (run === runRef.current) setError(explainError(err))
      } finally {
        if (run === runRef.current) {
          sessionRef.current = null
          setWatching(false)
          await session.close()
        }
      }
    },
    [append, explainError, stop],
  )

  const release = useCallback(async () => {
    await stop()
    setError(null)
    setOpening(false)
  }, [stop])

  const reboot = useCallback(async () => {
    const session = sessionRef.current
    if (session === null) return
    try {
      await session.reboot()
    } catch (err) {
      setError(explainError(err))
    }
  }, [explainError])

  const clear = useCallback(() => setEvents([]), [])

  // Leaving the page must hand the device back. A React unmount that kept the port would
  // strand it until the tab closed, which is precisely the "port is already open" the
  // operator then blames on `screen`.
  useEffect(
    () => () => {
      runRef.current += 1
      const session = sessionRef.current
      sessionRef.current = null
      void session?.close()
    },
    [],
  )

  return { watching, opening, events, summary, error, watch, release, reboot, clear }
}
