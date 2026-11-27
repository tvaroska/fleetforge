// Pure ESP-IDF / boot-ROM UART line classifier and boot-state reducer for `boardConsole.ts`.

import {
  MILESTONES,
  MILESTONE_DEADLINE_MS,
  MILESTONE_REMEDY,
  MILESTONE_STALL,
  NONE_JOINED_LINE,
  NO_KNOWN_NETWORK_LINE,
  type BootReset,
  type Cause,
  type ConsoleEvent,
  type ConsoleLevel,
  type ConsoleSummary,
  type Milestone,
  type MilestoneStall,
  type Remedy,
  type ResetReason,
  type Restart,
} from './boardConsoleTypes'

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

const ANSI = /\u001b\[[0-9;]*[A-Za-z]/g
const LOG_LINE = /^([IWED])\s+\((\d+)\)\s+([A-Za-z0-9_.-]+):\s?(.*)$/
const LEVELS: Record<string, ConsoleLevel> = { E: 'error', W: 'warn', I: 'info', D: 'info' }

type Hint = {
  text: string
  kind: 'generic' | 'specific'
  remedy: Remedy | null
  cause: Cause | null
}

const specific = (cause: Cause, text: string, remedy: Remedy | null = null): Hint => ({
  text,
  kind: 'specific',
  remedy,
  cause,
})

const FF_TIME_SANE_YEAR = 2024
const RESET_BANNER = /^rst:0x[0-9a-f]+\s*\(([A-Z0-9_]+)\)/i
const BOOT_MODE = /boot:0x[0-9a-f]+\s*\(([^)]*)/i
const BROWNOUT_LINE = /Brownout detector was triggered/i
const PANIC_LINE = /Guru Meditation Error|assert failed:|abort\(\) was called/
const PREVIOUS_BOOT_BROWNOUT = 'the previous boot ended in a BROWNOUT'
const PHY_FULL_CALIBRATION = /falling back to full calibration/i

const BROWNOUT_DURING_CALIBRATION = specific(
  'power',
  'The 3.3 V rail collapsed during radio calibration — before this board ever tried to ' +
    'join the network, so the network settings are not the problem. This board has no ' +
    'stored RF calibration, so every boot runs the full calibration, which draws more ' +
    'current than anything else in startup, and it dies in the same place each time. It ' +
    'cannot get out of that on its own: the calibration is only saved once a boot survives ' +
    'it. Give it a steadier 3.3 V — a different USB port or cable, or a bulk capacitor ' +
    'across 3V3 and GND — and once it survives a single boot it caches the calibration and ' +
    'stops doing this.',
)

const BARE_RULES: { match: RegExp; level: ConsoleLevel; hint: Hint }[] = [
  {
    match: BROWNOUT_LINE,
    level: 'error',
    hint: specific(
      'power',
      'The board reset because its 3.3 V rail sagged below about 2.43 V, which is where ' +
        'the brownout detector trips. That is a real collapse, not a marginal reading. The ' +
        'usual cause is the USB supply — a thin or long cable, a hub, or a keyboard or ' +
        'monitor port — but a board whose own regulator or wiring cannot meet the radio’s ' +
        'current peaks does this on a good cable too. The line just above says which stage ' +
        'it died in.',
    ),
  },
  {
    match: /Guru Meditation Error|assert failed:/,
    level: 'error',
    hint: specific(
      'firmware',
      'The firmware crashed. Flash the board again; if it crashes in the same place a second ' +
        'time, this is a bug in the firmware rather than anything the cable can fix.',
      'reflash',
    ),
  },
  {
    match: /^Backtrace:/,
    level: 'error',
    hint: {
      text: 'The board printed a crash backtrace. The line above names what it crashed on.',
      kind: 'generic',
      remedy: null,
      cause: null,
    },
  },
  {
    match: /waiting for download/,
    level: 'error',
    hint: specific(
      'download-mode',
      'The board came up in its flashing bootloader instead of running the agent. Unplug it, ' +
        'plug it back in, and press "Reboot the board".',
      'reboot',
    ),
  },
  {
    match: /invalid header: 0x|flash read err/,
    level: 'error',
    hint: specific(
      'firmware',
      'There is no usable firmware in the slot the board tried to boot. Flash the board again.',
      'reflash',
    ),
  },
]

function resetBannerRule(reason: string): { level: ConsoleLevel; hint: Hint | null } {
  if (/BROWN_OUT/.test(reason)) {
    return {
      level: 'error',
      hint: specific(
        'power',
        'This board reset because its 3.3 V rail collapsed. Most often that is the USB ' +
          'supply — a thin or long cable, a hub, or a keyboard or monitor port — but a ' +
          'board that cannot meet the radio’s current peaks does it on a good cable too.',
      ),
    }
  }
  if (/WDT/.test(reason)) {
    return {
      level: 'warn',
      hint: specific(
        'firmware',
        'The board stopped responding and a watchdog reset it. If it keeps happening, flash ' +
          'the board again.',
        'reflash',
      ),
    }
  }
  if (/SW_(CPU_)?RESET/.test(reason)) {
    return {
      level: 'warn',
      hint: {
        text: 'The firmware restarted itself. Anything printed just above this says why.',
        kind: 'generic',
        remedy: null,
        cause: null,
      },
    }
  }
  return { level: 'plain', hint: null }
}

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
    level: bare?.hint != null ? bare.level : match === null ? 'plain' : (LEVELS[match[1]] ?? 'info'),
    tag,
    text,
    milestone,
    bootMarker: banner !== null ? 'rom' : milestone === 'boot' ? 'agent' : null,
    hint: hint?.text ?? null,
    hintKind: hint?.kind ?? 'specific',
    remedy: hint?.remedy ?? null,
    cause: hint?.cause ?? null,
    source: 'board',
    commandedReset: false,
  }
}

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
    cause: null,
    source: 'panel',
    commandedReset: options.commandedReset ?? false,
  }
}

function milestoneFor(tag: string | null, text: string): Milestone | null {
  if (tag === 'ff-agent' && text.startsWith('fleetforge agent ')) return 'boot'
  if (tag === 'ff-net' && / link up/.test(text)) return 'link'
  if (tag === 'ff-time' && /^sntp: .* -> /.test(text)) return 'clock'
  if (tag === 'ff-enroll' && /^enroll 200\b/.test(text)) return 'enroll'
  if (tag === 'ff-mqtt' && text.startsWith('mqtt connected as ')) return 'fleet'
  return null
}

function hintFor(tag: string | null, text: string): Hint | null {
  if (tag === 'ff-agent' && text.includes(PREVIOUS_BOOT_BROWNOUT)) {
    return specific(
      'power',
      'This boot is fine, but the one before it died when the 3.3 V rail collapsed — the ' +
        'agent read that off the reset reason rather than guessing. The supply is marginal ' +
        'for this board: it will do this again on a cold boot, an OTA, or anything else ' +
        'that makes the radio work harder. Worth fixing before the board is somewhere you ' +
        'cannot reach it.',
    )
  }

  if (tag === 'ff-agent' && text.startsWith('halted:')) {
    return {
      text: 'The agent gave up on purpose. It will not retry until the board is re-flashed.',
      kind: 'generic',
      remedy: 'reflash',
      cause: null,
    }
  }

  if (tag === 'ff-wifi') {
    const none = NO_KNOWN_NETWORK_LINE.exec(text)
    if (none !== null) {
      const n = Number(none[1])
      return specific(
        'wifi',
        n === 1
          ? 'Its one network is not in range: no access point with that SSID answered. ' +
              'Check the spelling, and check the network has a 2.4 GHz band — the ESP32 ' +
              'radio cannot see 5 GHz at all. The board keeps trying on its own.'
          : `None of its ${n} known networks is in range. The board keeps scanning on its ` +
              'own and joins the first one it sees. Move it closer to one of them, or ' +
              're-flash it with the network it is near (2.4 GHz only).',
      )
    }
    const unjoined = NONE_JOINED_LINE.exec(text)
    if (unjoined !== null) {
      const v = Number(unjoined[1])
      const n = Number(unjoined[2])
      return specific(
        'wifi',
        `${v} of its ${n} known networks ${v === 1 ? 'is' : 'are'} in range, but none gave ` +
          'the board an address — usually a wrong passphrase, or an access point that ' +
          'filters or isolates clients. The board keeps trying.',
      )
    }
    const reason = /^disconnected \(reason (-?\d+)\)/.exec(text)
    if (reason !== null) {
      const code = Number(reason[1])
      if (code === 201 && /; (trying ".*" next|that was the last known network)$/.test(text)) {
        return { text: WIFI_REASONS[201], kind: 'generic', remedy: null, cause: null }
      }
      return specific(
        'wifi',
        WIFI_REASONS[code] ?? `the AP dropped this board (esp_wifi reason ${code})`,
      )
    }
    if (text.includes('carries no ssid')) {
      return specific(
        'wifi',
        'This board was flashed with link=wifi but no SSID. Re-flash it with one.',
      )
    }
  }

  if (tag === 'ff-net' && text.startsWith('no IP address after')) {
    return specific(
      'wifi',
      'Associated, but DHCP never answered. The AP may be isolating this client.',
    )
  }

  if (tag === 'ff-time') {
    if (text.startsWith('sntp: no answer')) {
      const year = /clock is still (\d{4})-/.exec(text)?.[1]
      if (year !== undefined && Number(year) >= FF_TIME_SANE_YEAR) {
        return {
          text:
            "No NTP answer, but the board's clock already holds a plausible time (it " +
            'survived the reset), so TLS can still work. If enrolment fails next, the ' +
            'network may be blocking UDP/123.',
          kind: 'generic',
          remedy: null,
          cause: null,
        }
      }
      return specific(
        'clock',
        'The clock is still unset, so TLS cannot check a certificate: enrolment over ' +
          'https:// and mqtts:// will fail until the clock is set. The network may be ' +
          'blocking UDP/123.',
      )
    }
    if (text.includes('no ntp server configured')) {
      return specific(
        'clock',
        'No NTP server in ff_cfg. Any https:// or mqtts:// URI will fail to verify.',
      )
    }
    if (text.includes('the clock is still before')) {
      return specific('clock', 'SNTP answered with a nonsense time. TLS will fail.')
    }
  }

  if (tag === 'ff-enroll') {
    if (/^enroll 401\b/.test(text)) {
      return specific(
        'token',
        'The server does not know this ffe_ token. Flash the board again to mint a new one.',
        'reflash',
      )
    }
    if (/^enroll 409\b/.test(text)) {
      return specific(
        'token',
        'This token was already spent, revoked or has expired. Tokens are single-use: ' +
          'flash the board again to mint a fresh one.',
        'reflash',
      )
    }
    if (/^enroll 429\b/.test(text)) {
      return specific(
        'server',
        'Rate limited. The limiter counts failures, so something before this is wrong too.',
      )
    }
    if (/^enroll 503\b/.test(text)) {
      return specific(
        'server',
        'The broker would not provision this device. This is a server-side fault, not the board.',
      )
    }
    if (text.startsWith('cannot reach ')) {
      return specific(
        'server',
        'The board has a network but could not reach the API. If the clock milestone is ' +
          'still open this is a TLS failure caused by the unset clock, not a routing problem.',
      )
    }
    if (text.includes('carries no enrollment')) {
      return specific(
        'token',
        'This board holds no credential and no ffe_ token. It can never join; re-flash it.',
        'reflash',
      )
    }
  }

  if (tag === 'ff-mqtt' && text.startsWith('broker refused the connection')) {
    return specific(
      'token',
      'The stored credential is no longer valid — the device was probably deleted or ' +
        'reset on the server. Re-flash the board to enrol it again.',
      'reflash',
    )
  }

  if (tag === 'ff-agent' && text.startsWith('no network yet')) {
    return {
      text: 'The link has not come up yet. The agent retries every 5 s and never gives up.',
      kind: 'generic',
      remedy: null,
      cause: null,
    }
  }

  return null
}

export function resetReasonFromBanner(rst: string, boot: string | null): ResetReason {
  if (boot !== null && /DOWNLOAD/i.test(boot)) return 'download-mode'
  if (/BROWN_?OUT/i.test(rst)) return 'brownout'
  if (/WDT/i.test(rst)) return 'watchdog'
  if (/DEEPSLEEP|DSLEEP/i.test(rst)) return 'deep-sleep'
  if (/POWERON/i.test(rst)) return 'power-on'
  if (/SW_/i.test(rst)) return 'restart'
  if (/EXT_|USB|JTAG/i.test(rst)) return 'external'
  return 'unknown'
}

export function summarizeConsole(events: ConsoleEvent[], now = Date.now()): ConsoleSummary {
  let reached = new Set<Milestone>()
  let fault: ConsoleSummary['fault'] = null
  let faultKind: 'generic' | 'specific' = 'generic'
  let boots = 0
  let rebootLoop: { boots: number } | null = null
  let romPending = false
  let commanded = false
  let calibrating = false
  let clue: 'brownout' | 'panic' | null = null
  let lastReset: ConsoleSummary['lastReset'] = null
  let inRestart = false
  const restarts: Restart[] = []
  let lost = new Set<Milestone>()
  let since = events.length > 0 ? events[0].at : now

  for (const event of events) {
    if (event.commandedReset) commanded = true
    if (event.bootMarker !== null) {
      const continuing = event.bootMarker === 'agent' && romPending
      romPending = event.bootMarker === 'rom'
      if (!continuing) {
        if (boots > 0 && !reached.has('fleet') && !commanded) rebootLoop = { boots: boots + 1 }
        let reset: BootReset
        if (event.bootMarker === 'rom') {
          const rst = RESET_BANNER.exec(event.raw)
          const bootMode = BOOT_MODE.exec(event.raw)
          const fromBanner = resetReasonFromBanner(rst?.[1] ?? '', bootMode?.[1] ?? null)
          reset = {
            reason: fromBanner === 'download-mode' ? fromBanner : (clue ?? fromBanner),
            rom: rst?.[1] ?? null,
          }
        } else {
          reset = { reason: clue ?? 'unknown', rom: null }
        }
        if (boots > 0 && !commanded) {
          const furthestEnding = MILESTONES.reduce((acc, m, i) => (reached.has(m) ? i : acc), -1)
          restarts.push({
            ...reset,
            at: event.at,
            stage: reached.has('fleet') ? null : (MILESTONES[furthestEnding + 1] ?? null),
          })
          MILESTONES.forEach((m, i) => {
            if (i <= furthestEnding) lost.add(m)
          })
          inRestart = true
        } else {
          inRestart = false
        }
        if (commanded) lost = new Set()
        lastReset = { ...reset, commanded }
        clue = null
        commanded = false
        boots += 1
        reached = new Set()
        calibrating = false
        since = event.at
      }
    }

    if (PHY_FULL_CALIBRATION.test(event.raw)) calibrating = true

    if (event.source === 'board') {
      if (BROWNOUT_LINE.test(event.raw)) clue = 'brownout'
      else if (PANIC_LINE.test(event.raw) && clue === null) clue = 'panic'
      if (event.tag === 'ff-agent' && event.text.includes(PREVIOUS_BOOT_BROWNOUT)) {
        if (lastReset !== null) lastReset = { ...lastReset, reason: 'brownout' }
        if (inRestart) restarts[restarts.length - 1].reason = 'brownout'
      }
    }

    if (event.milestone !== null) {
      reached.add(event.milestone)
      since = event.at
      if (event.milestone === 'fleet') rebootLoop = null
      fault = null
      faultKind = 'generic'
      continue
    }
    if (event.hint === null) continue
    if (event.level !== 'warn' && event.level !== 'error') continue
    if (event.hintKind === 'generic' && faultKind === 'specific') continue
    const staged = calibrating && BROWNOUT_LINE.test(event.raw)
    fault = {
      text: event.text,
      hint: staged ? BROWNOUT_DURING_CALIBRATION.text : event.hint,
      remedy: staged ? BROWNOUT_DURING_CALIBRATION.remedy : event.remedy,
      cause: staged ? BROWNOUT_DURING_CALIBRATION.cause : event.cause,
    }
    faultKind = event.hintKind
  }

  const ordered = MILESTONES.filter((m) => reached.has(m))
  const furthest = MILESTONES.reduce((acc, m, i) => (reached.has(m) ? i : acc), -1)
  const skipped = MILESTONES.filter((m, i) => i < furthest && !reached.has(m))
  const waitingFor = MILESTONES[furthest + 1] ?? null

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

  const retracted = MILESTONES.filter(
    (m) => lost.has(m) && !reached.has(m) && !skipped.includes(m),
  )

  return {
    reached: ordered,
    skipped,
    waitingFor,
    fault,
    boots,
    rebootLoop,
    lastReset,
    restarts,
    retracted,
    overdue,
  }
}
