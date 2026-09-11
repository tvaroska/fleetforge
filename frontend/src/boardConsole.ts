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
 * A fix the panel can perform itself. There are only two, because the panel's only
 * channel to the board is the serial port: pulse EN, or take the port back and write
 * a new ff_cfg. "Retry enrol" and "mint a fresh token" are not separate actions —
 * the agent has no command surface, and a token that is not written changes nothing.
 */
export type Remedy = 'reboot' | 'reflash'

/**
 * Button copy for a remedy. Exported because the panel and the tests must agree on it.
 *
 * Short on purpose: `button` is set in Press Start 2P (see index.css rule 2), which is
 * ~2.5x wide. Both strings are also deliberately distinct from every existing button
 * name — "Reboot the board", "Flash this board", "Watch a board" — because Testing
 * Library matches accessible names by substring and a collision turns every existing
 * `getByRole('button', …)` into "found multiple elements". In particular
 * `'Re-flash this board'` is forbidden: it contains `flash this board`.
 */
export const REMEDY_LABELS: Record<Remedy, string> = {
  reboot: 'Reboot and retry',
  reflash: 'Re-flash the board',
}

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
  /** Wall clock when the line arrived. Milestone deadlines are the only consumer. */
  at: number
  /** The line as it came off the wire, ANSI stripped. Always shown. */
  raw: string
  level: ConsoleLevel
  /** `ff-wifi`, `ff-enroll`, … or null for boot-ROM chatter that is not an IDF log line. */
  tag: string | null
  /** The message without the `I (1234) ff-wifi: ` preamble. */
  text: string
  /** Reaching this proves a step of the boot happened. */
  milestone: Milestone | null
  /**
   * This line is the start of a boot: `'rom'` for the boot-ROM reset banner, `'agent'` for
   * the agent's own first line. Both mark a boundary past which nothing an earlier boot
   * proved is still true — see `summarizeConsole`. One boot prints both, in that order.
   */
  bootMarker: 'rom' | 'agent' | null
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
  /**
   * A fix the panel can perform for this line, or null when the board will fix itself or
   * only a human with a cable can. Rides on the event for the same reason `commandedReset`
   * does: `summarizeConsole` is pure over `events` alone, so anything the summary must
   * know has to travel in-band rather than as a second argument.
   */
  remedy: Remedy | null
  /** Who printed this line: the board over UART, or this panel narrating what it did. */
  source: 'board' | 'panel'
  /**
   * This panel deliberately reset the board here. The next boot marker is therefore
   * expected, and must not be counted as evidence of a reset loop.
   */
  commandedReset: boolean
}

export type ConsoleSummary = {
  /**
   * What **this boot** has proved, not what the session has ever seen. A board that resets
   * retracts everything the previous boot showed: on 2026-09-11 a single early boot reached
   * `link up` and the panel then displayed ✓ **Network up** for the rest of a session in
   * which the board reset dozens of times. A stale ✓ is worse than silence — it sends the
   * diagnosis in the wrong direction, and it did.
   */
  reached: Milestone[]
  /** The next thing that has not happened yet, or null once the board is on the fleet. */
  waitingFor: Milestone | null
  /**
   * The most recent explained failure that no later milestone has invalidated. A board
   * that recovers on its own clears its own fault, which is why this is folded over the
   * whole event list rather than latched on the first error.
   *
   * Deliberately NOT cleared by a reboot, unlike `reached`. A fault is a description of
   * something that happened; the reset it caused does not make it untrue, and a panic's
   * cause is printed immediately *before* the reset that hides it.
   */
  fault: { text: string; hint: string; remedy: Remedy | null } | null
  /** Boots seen since this panel started watching. */
  boots: number
  /**
   * Set when a boot began while the previous boot had not reached the fleet — which is what
   * a reset loop looks like from outside, and is proof on its own even when no line explains
   * why. Cleared by a boot that does reach the fleet, so a deliberate reboot of a working
   * board never raises it.
   */
  rebootLoop: { boots: number } | null
  /** The milestone being waited on has blown its deadline. Nothing here spins forever. */
  overdue: MilestoneStall | null
}

export type MilestoneStall = {
  milestone: Milestone
  /** How long this milestone has been waited on, in ms. */
  waitedMs: number
  /** What should have happened, what usually prevents it, and what to try. */
  hint: string
  /** The fix the panel can perform for this stall, or null. See `MILESTONE_REMEDY`. */
  remedy: Remedy | null
}

/**
 * How long each milestone may take before the panel says something, measured from the point
 * the one before it was reached (or from the start of the boot, for `boot` itself).
 *
 * Grounded in the agent's own constants so a deadline cannot fire before the board has even
 * given up: `agent_main.c` waits `NET_TIMEOUT_MS` 30 s for a link and `SNTP_TIMEOUT_MS` 15 s
 * for the clock, and `ff_enroll.c` allows `ENROLL_TIMEOUT_MS` 30 s per attempt. Each deadline
 * here is that budget plus room for one retry.
 */
export const MILESTONE_DEADLINE_MS: Record<Milestone, number> = {
  boot: 5_000,
  link: 45_000,
  clock: 30_000,
  enroll: 60_000,
  fleet: 30_000,
}

/** Plain English for a milestone that never arrived. The operator is not an engineer. */
export const MILESTONE_STALL: Record<Milestone, string> = {
  boot:
    'Nothing on this port looks like the fleetforge agent. Either the flash did not take, ' +
    'or the board is sitting in its ROM bootloader instead of running the app. Flash the ' +
    'board again, then press "Reboot the board".',
  link:
    'The agent is running but has not joined the network. The usual causes are a mistyped ' +
    'network name or password, or a 5 GHz-only network — this board\'s radio cannot see ' +
    '5 GHz at all. Re-flash it with the right network, or move it closer to the router.',
  clock:
    'The network is up but the board could not get the time, and without a clock it cannot ' +
    'check the server\'s certificate — so enrolment will fail no matter how many times it ' +
    'retries. Guest and corporate networks often block NTP (UDP port 123).',
  enroll:
    'The board has a network and a clock but the server has not accepted it. Check that ' +
    'this network can reach the fleetforge server, and if the board was flashed a while ago, ' +
    'flash it again to mint a fresh enrolment token.',
  fleet:
    'The board enrolled but never reached the message broker. Outbound port 8883 is usually ' +
    'the culprit on a locked-down network.',
}

/**
 * Which stalls the panel can act on itself. `null` where the fix is the network or the
 * cable — see `Remedy`, and S0-fe-6's rule: no button that cannot work.
 *
 * `boot` and `enroll` are the two whose prose above already issues the instruction
 * ("Flash the board again", "flash it again to mint a fresh enrolment token"), which is
 * precisely the three manual steps this task replaces with one press.
 */
export const MILESTONE_REMEDY: Record<Milestone, Remedy | null> = {
  boot: 'reflash',
  link: null,
  clock: null,
  enroll: 'reflash',
  fleet: null,
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

type Hint = { text: string; kind: 'generic' | 'specific'; remedy: Remedy | null }
/**
 * A named diagnosis, optionally with a fix the panel can perform.
 *
 * The remedy defaults to `null` — S0-fe-6's rule is that a remedy is offered only when
 * the board will NOT fix itself *and* the panel's action changes the outcome. The agent
 * retries the link forever and backs off 60 s → 15 min on a 503, so a button that only
 * restarts a retry already in progress is a button that cannot work.
 */
const specific = (text: string, remedy: Remedy | null = null): Hint => ({
  text,
  kind: 'specific',
  remedy,
})

/** The boot-ROM reset banner: `rst:0xf (RTCWDT_BROWN_OUT_RESET),boot:0x13 (SPI_FAST…)`. */
const RESET_BANNER = /^rst:0x[0-9a-f]+\s*\(([A-Z0-9_]+)\)/i

/**
 * Lines that carry no ESP-IDF preamble, and the most important ones on a bench.
 *
 * These are the reason this table exists. `LOG_LINE` needs `E (1234) tag: msg`; a brownout
 * prints `E BOD: Brownout detector was triggered`, the ROM prints `rst:0x…`, and a panic
 * prints `Guru Meditation Error:` — none of which have a timestamp or a tag. Before
 * 2026-09-11 every one of them classified as `level: 'plain', tag: null` and `hintFor`,
 * keyed entirely on the tag, could not see them. The most common first-board failure mode
 * in existence produced zero diagnostic output by construction.
 *
 * Matched against the whole cleaned line rather than the split-off message, so a build that
 * *does* route one of these through the normal logger (`E (403) BOD: …`) is still caught.
 * The level is raised too: `summarizeConsole` only accepts a fault from a warn or an error.
 */
const BARE_RULES: { match: RegExp; level: ConsoleLevel; hint: Hint }[] = [
  {
    // `esp_brownout` fires when the 3.3 V rail sags below the detector's trip point — on a
    // DevKit that is nearly always the USB cable, not the board.
    match: /Brownout detector was triggered/i,
    level: 'error',
    hint: specific(
      'The board is browning out: the USB port cannot hold 3.3 V while the Wi-Fi radio ' +
        'draws current, so the board resets before it can join. Try a shorter, thicker USB ' +
        'cable, straight into the machine, not a hub — and not a keyboard or monitor port.',
    ),
  },
  {
    match: /Guru Meditation Error|assert failed:/,
    level: 'error',
    hint: specific(
      'The firmware crashed. Flash the board again; if it crashes in the same place a second ' +
        'time, this is a bug in the firmware rather than anything the cable can fix.',
      // The hint already says "flash the board again" — S0-fe-6 makes that a button.
      'reflash',
    ),
  },
  {
    // Follows the panic line, so it must not displace it: generic never overwrites specific.
    match: /^Backtrace:/,
    level: 'error',
    // Generic hints never carry a remedy: a generic hint can be displaced by a specific
    // one, and an action chosen from a line that merely FOLLOWS the cause is an action
    // chosen from the wrong evidence.
    hint: {
      text: 'The board printed a crash backtrace. The line above names what it crashed on.',
      kind: 'generic',
      remedy: null,
    },
  },
  {
    // `SerialConsole.reboot()` drives RTS with DTR low; inverted wiring lands here instead.
    match: /waiting for download/,
    level: 'error',
    hint: specific(
      'The board came up in its flashing bootloader instead of running the agent. Unplug it, ' +
        'plug it back in, and press "Reboot the board".',
      // An EN pulse with DTR low is exactly the fix the prose already asks for.
      'reboot',
    ),
  },
  {
    match: /invalid header: 0x|flash read err/,
    level: 'error',
    hint: specific(
      'There is no usable firmware in the slot the board tried to boot. Flash the board again.',
      'reflash',
    ),
  },
]

/** A reset banner is normal at the top of a boot; only some reasons are a diagnosis. */
function resetBannerRule(reason: string): { level: ConsoleLevel; hint: Hint | null } {
  if (/BROWN_OUT/.test(reason)) {
    return {
      level: 'error',
      hint: specific(
        'This board reset because its power browned out. Try a shorter, thicker USB cable, ' +
          'straight into the machine, not a hub.',
      ),
    }
  }
  if (/WDT/.test(reason)) {
    return {
      level: 'warn',
      hint: specific(
        'The board stopped responding and a watchdog reset it. If it keeps happening, flash ' +
          'the board again.',
        'reflash',
      ),
    }
  }
  if (/SW_(CPU_)?RESET/.test(reason)) {
    return {
      level: 'warn',
      // Generic: a panic prints its cause immediately before the reset it causes. And a
      // generic hint never carries a remedy — see `Backtrace:` above.
      hint: {
        text: 'The firmware restarted itself. Anything printed just above this says why.',
        kind: 'generic',
        remedy: null,
      },
    }
  }
  // POWERON_RESET, DEEPSLEEP_RESET, EXT_CPU_RESET: the ordinary start of a boot.
  return { level: 'plain', hint: null }
}

/**
 * One line in, one classified event out. Pure, and the only place that knows what the
 * agent's log lines look like — every string matched here exists in `agent/main/*.c`, in
 * ESP-IDF's own early-boot output, or in the boot ROM.
 */
export function classifyConsoleLine(raw: string, seq: number, at = Date.now()): ConsoleEvent {
  const clean = raw.replace(ANSI, '').replace(/\r/g, '')
  const match = LOG_LINE.exec(clean)
  const tag = match === null ? null : match[3]
  const text = match === null ? clean : match[4]

  const banner = RESET_BANNER.exec(clean)
  const bare =
    banner !== null
      ? resetBannerRule(banner[1])
      : (BARE_RULES.find((rule) => rule.match.test(clean)) ?? null)

  const milestone = milestoneFor(tag, text)
  const hint = hintFor(tag, text) ?? bare?.hint ?? null
  return {
    seq,
    at,
    raw: clean,
    // A rule that names a fault outranks the preamble's own letter, and is the only way a
    // tagless line can become anything but 'plain'.
    level: bare?.hint != null ? bare.level : match === null ? 'plain' : (LEVELS[match[1]] ?? 'info'),
    tag,
    text,
    milestone,
    bootMarker: banner !== null ? 'rom' : milestone === 'boot' ? 'agent' : null,
    hint: hint?.text ?? null,
    hintKind: hint?.kind ?? 'specific',
    remedy: hint?.remedy ?? null,
    source: 'board',
    commandedReset: false,
  }
}

/** A line this panel printed about itself. Never a milestone, never a fault, never a
 *  remedy — the panel does not diagnose its own narration. */
export function panelNotice(
  raw: string,
  seq: number,
  at = Date.now(),
  options: { commandedReset?: boolean } = {},
): ConsoleEvent {
  return {
    seq,
    at,
    raw,
    level: 'plain',
    tag: null,
    text: raw,
    milestone: null,
    bootMarker: null,
    hint: null,
    hintKind: 'specific',
    remedy: null,
    source: 'panel',
    commandedReset: options.commandedReset ?? false,
  }
}

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
    return {
      text: 'The agent gave up on purpose. It will not retry until the board is re-flashed.',
      // GENERIC, and this is a correction to S0-fe-6's original table. `park()`
      // (`agent_main.c:67`) logs its reason strictly AFTER the failure it reports, so on
      // the flagship case — `enroll 409` then `halted:` — a specific classification would
      // replace "this token is single-use, flash the board again to mint a fresh one"
      // with the engineer-facing "re-flash ff_cfg with a fresh ffe_ token (POST
      // /v1/enrollment-tokens)". Same shape as `Backtrace:` and `SW_CPU_RESET`: real, but
      // derivative of the line above it.
      kind: 'generic',
      // It DOES carry the remedy, unlike the other generic lines. The ban on those exists
      // because their fix belongs to the specific line above (a crash's re-flash, a
      // retry's nothing); here the action is the same one whatever evidence you read it
      // from, and a board that parks with nothing named above it — `no usable ff_cfg
      // partition`, `no eFuse MAC` — is fixed by exactly this button and nothing else.
      remedy: 'reflash',
    }
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
        'reflash',
      )
    }
    if (/^enroll 409\b/.test(text)) {
      return specific(
        'This token was already spent, revoked or has expired. Tokens are single-use: ' +
          'flash the board again to mint a fresh one.',
        // S0-fe-6's flagship case: the three manual steps this sentence describes are
        // exactly what `useFlashBoard.reflash` does in one click.
        'reflash',
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
        'reflash',
      )
    }
  }

  if (tag === 'ff-mqtt' && text.startsWith('broker refused the connection')) {
    return specific(
      'The stored credential is no longer valid — the device was probably deleted or ' +
        'reset on the server. Re-flash the board to enrol it again.',
      // Only an erase clears it: `ff_store_load()` short-circuits enrolment while a
      // credential is in NVS, so a fresh token written beside it is dead on arrival.
      // The recovery path always erases — see `FlashBoard.tsx`.
      'reflash',
    )
  }

  // `agent_main.c:167`, once every 5 s, forever. Generic on purpose: on a board with a bad
  // SSID this is the last line printed, and it must not bury the reason code above it.
  if (tag === 'ff-agent' && text.startsWith('no network yet')) {
    return {
      text: 'The link has not come up yet. The agent retries every 5 s and never gives up.',
      kind: 'generic',
      // Generic hints never carry a remedy, and this one names its own reason why: the
      // agent is already retrying, so no button of ours changes the outcome.
      remedy: null,
    }
  }

  return null
}

/** Fold the events into the checklist and the current fault. Pure; the UI renders this. */
export function summarizeConsole(events: ConsoleEvent[], now = Date.now()): ConsoleSummary {
  let reached = new Set<Milestone>()
  let fault: { text: string; hint: string; remedy: Remedy | null } | null = null
  let faultKind: 'generic' | 'specific' = 'generic'
  let boots = 0
  let rebootLoop: { boots: number } | null = null
  /** A ROM banner has started a boot whose agent banner has not arrived yet. */
  let romPending = false
  /** The panel asked for the next boot. It is not evidence of a loop. */
  let commanded = false
  /** When the current wait began: the last boot boundary or milestone, whichever is later. */
  let since = events.length > 0 ? events[0].at : now

  for (const event of events) {
    if (event.commandedReset) commanded = true
    if (event.bootMarker !== null) {
      // One boot prints the ROM banner and then the agent's own first line. Counting both
      // would report every board as looping.
      const continuing = event.bootMarker === 'agent' && romPending
      romPending = event.bootMarker === 'rom'
      if (!continuing) {
        if (boots > 0 && !reached.has('fleet') && !commanded) rebootLoop = { boots: boots + 1 }
        commanded = false
        boots += 1
        // The new boot has proved nothing yet. This is the stale-✓ fix.
        reached = new Set()
        since = event.at
      }
    }

    if (event.milestone !== null) {
      reached.add(event.milestone)
      since = event.at
      // A board that got all the way up is not looping, whatever it did on the way.
      if (event.milestone === 'fleet') rebootLoop = null
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
    fault = { text: event.text, hint: event.hint, remedy: event.remedy }
    faultKind = event.hintKind
  }

  const ordered = MILESTONES.filter((m) => reached.has(m))
  const waitingFor = MILESTONES.find((m) => !reached.has(m)) ?? null

  // Nothing may spin forever: a milestone that has outlived its budget says so itself, even
  // when the board has gone completely quiet and no line will ever arrive to explain it.
  const waitedMs = now - since
  const overdue: MilestoneStall | null =
    events.length > 0 && waitingFor !== null && waitedMs >= MILESTONE_DEADLINE_MS[waitingFor]
      ? {
          milestone: waitingFor,
          waitedMs,
          hint: MILESTONE_STALL[waitingFor],
          remedy: MILESTONE_REMEDY[waitingFor],
        }
      : null

  return { reached: ordered, waitingFor, fault, boots, rebootLoop, overdue }
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
  const [watching, setWatching] = useState(false)
  // A deadline passes with no new line arriving — that IS the stalled case — so the summary
  // has to be recomputed against the wall clock rather than only when an event lands. Only
  // while a port is open: an idle panel must not re-render once a second forever.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!watching) return
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [watching])
  const summary = useMemo(() => summarizeConsole(events, now), [events, now])
  const [opening, setOpening] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const sessionRef = useRef<BoardConsole | null>(null)
  /** Bumped on every `watch()`/`release()`; a stale generator's lines are dropped. */
  const runRef = useRef(0)
  const seqRef = useRef(0)
  /** When the panel last commanded a reset; used to explain a native-USB disconnect. */
  const commandedResetAtRef = useRef<number | null>(null)

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

  const appendNotice = useCallback(
    (raw: string, options: { commandedReset?: boolean } = {}) => {
      const event = panelNotice(raw, seqRef.current++, Date.now(), options)
      setEvents((current) => {
        const next = current.length >= MAX_CONSOLE_LINES ? current.slice(1) : current.slice()
        next.push(event)
        return next
      })
    },
    [],
  )

  const requestReboot = useCallback(
    (session: BoardConsole, why: string) => {
      appendNotice(`— ${why}`, { commandedReset: true })
      commandedResetAtRef.current = Date.now()
      // NOT awaited: the read loop must be attached before the board starts talking again
      // (Chromium's Serial read buffer defaults to 255 bytes ≈ 22 ms at 115200), and the
      // notice above is appended synchronously so its position in the stream is
      // deterministic — step 3 keys the loop suppression off that position.
      void session.reboot().catch((err: unknown) => {
        // A failed pulse must not read as a board fault: `setSignals` is unsupported on
        // some adapters, and the panel is still perfectly usable without it.
        appendNotice(`— could not reset the board: ${explainError(err)}. Press "Reboot the board".`)
      })
    },
    [appendNotice, explainError],
  )

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
          onLog: appendNotice,
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
      // S0-fe-5: whatever this board printed while the flasher held the port went to nobody.
      // Pulse EN so the log the operator reads starts at the first line of a boot.
      requestReboot(session, 'resetting the board so the log starts at its first line')
      try {
        for await (const line of session.lines()) {
          if (run !== runRef.current) break
          append(line)
        }
        if (run === runRef.current) {
          const recentReset = commandedResetAtRef.current !== null && Date.now() - commandedResetAtRef.current < 5000
          setError(
            recentReset
              ? 'The board dropped off the USB bus when it was reset — some boards re-enumerate. Press "Watch a board" to pick it up again.'
              : 'The board disconnected. Plug it back in and watch it again.',
          )
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
    [append, appendNotice, explainError, requestReboot, stop],
  )

  const release = useCallback(async () => {
    await stop()
    setError(null)
    setOpening(false)
  }, [stop])

  const reboot = useCallback(async () => {
    const session = sessionRef.current
    if (session === null) return
    requestReboot(session, 'reset requested — the board is restarting')
  }, [requestReboot])

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
