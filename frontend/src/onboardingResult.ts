// The onboarding result card's judgement (R2b-fe-3). Pure: no React, no fetch.
// `ResultCard.tsx` renders what this returns and decides nothing.
//
// `spec/flows.md` Flow 1 step 6: "One card, success or failure: device id, firmware,
// partition layout, link, clock source, enrolled, on the fleet, UI and API versions. A
// failure shows ONE cause, ONE in-place recovery action, and ONE click to copy a redacted
// diagnostic bundle."
//
// Where each value comes from. The console is preferred, because it is what is running
// right now; the device row can be stale for a re-enrolled board until its next announce.
// Success comes from the MERGED summary reaching `fleet`: the console's own milestone, or
// (R2b-fe-5) the server's view of the board, judged in `serverWatch.ts` and merged in by the
// panel before it calls this. A board whose console lost the port (a native-USB reset) can
// therefore succeed with zero console lines. The device row otherwise only ENRICHES rows
// here (firmware fallback, layout, "server: online").
//
// R2b-fe-13 (Flow 3 step 5): when the board's last scan cycle ended without an address, the
// failure names it in plain words ("none of its 2 known networks is in range"). That comes
// from the cycle line read into `ConsoleFacts`, not from `summary.fault`, so it cannot
// flicker between cycles.

import {
  MILESTONE_CAUSE,
  MILESTONE_LABELS,
  NONE_JOINED_LINE,
  NO_KNOWN_NETWORK_LINE,
  describeRestarts,
  type Cause,
  type ConsoleEvent,
  type ConsoleSummary,
  type Milestone,
  type Remedy,
} from './boardConsole'
import { type DeviceSummary } from './api'
import { knowsNetworks } from './network'
import { type Tone, type VersionLine } from './statusStrip'
import { type FleetBaseline } from './serverWatch'

/** `ff_identity.c:64` — "device_id <12 hex>". Shared with `serverWatch.ts::lastDeviceId`. */
export const DEVICE_ID_LINE = /^device_id ([0-9a-fA-F]{12})\b/
/** `ff_mqtt.c` — "mqtt connected as <id> (<uri>)". Shared with `serverWatch.ts::lastDeviceId`. */
export const MQTT_CONNECTED_LINE = /^mqtt connected as ([0-9a-fA-F]{12})\b/

/** `FF_TIME_SANE_YEAR`, `agent/main/ff_time.c:24` — the same bound `boardConsole.ts` uses. */
const SANE_YEAR = 2024

/** What the board said about itself, THIS boot only. */
export type ConsoleFacts = {
  deviceId: string | null
  agentVersion: string | null
  ssid: string | null
  /** N of "N known networks" (agent >= 0.4.6; 1 for the single-network form). */
  knownNetworks: number | null
  /**
   * The last scan cycle ended with no address: `known` networks, `inRange` of them seen.
   * Read from the cycle line, not from `summary.fault`, so it cannot flicker.
   */
  unjoined: { known: number; inRange: number } | null
  link: { type: 'wifi' | 'ethernet'; ip: string | null } | null
  /**
   * `set`: the clock milestone line was seen ("sntp: a -> b (via server)").
   * Otherwise the `sntp: no answer` line, with the year the clock still held.
   */
  ntp: { server: string; set: true } | { server: string; set: false; keptYear: number | null } | null
}

const EMPTY_FACTS: ConsoleFacts = {
  deviceId: null,
  agentVersion: null,
  ssid: null,
  knownNetworks: null,
  unjoined: null,
  link: null,
  ntp: null,
}

/**
 * Folds the board's own lines into facts, resetting at every boot marker: a reset retracts
 * everything an earlier boot said, for the same reason `summarizeConsole` clears `reached`.
 * The agent banner IS a boot marker and carries the version, so its own fact is applied
 * after the reset. Matches on `tag` + `text` the way `milestoneFor` does; every format is
 * an `ESP_LOGx` string in `agent/main/*.c`.
 */
export function consoleFacts(events: ConsoleEvent[]): ConsoleFacts {
  let facts: ConsoleFacts = { ...EMPTY_FACTS }
  for (const event of events) {
    if (event.source !== 'board') continue
    if (event.bootMarker !== null) facts = { ...EMPTY_FACTS }
    const { tag, text } = event
    if (tag === 'ff-agent') {
      // `agent_main.c:129` — "fleetforge agent <ver> (idf …), built …".
      const version = /^fleetforge agent (\S+)/.exec(text)?.[1]
      if (version !== undefined) facts.agentVersion = version
    } else if (tag === 'ff-id') {
      // `ff_identity.c:64` — "device_id <12 hex>".
      const id = DEVICE_ID_LINE.exec(text)?.[1]
      if (id !== undefined) facts.deviceId = id.toLowerCase()
    } else if (tag === 'ff-wifi') {
      // `ff_net_wifi.c:228` — "wifi sta starting, ssid <ssid>". Not a secret.
      const ssid = /^wifi sta starting, ssid (.+)$/.exec(text)?.[1]
      if (ssid !== undefined) {
        facts.ssid = ssid
        facts.knownNetworks = 1
      }
      // `ff_net_wifi.c:553` — "wifi sta starting, N known networks; scanning".
      const starting = /^wifi sta starting, (\d+) known networks/.exec(text)?.[1]
      if (starting !== undefined) facts.knownNetworks = Number(starting)
      // `trying "x" (known network K of N…)` and `joined "x" (known network K of N)`.
      const ofN = /\(known network \d+ of (\d+)/.exec(text)?.[1]
      if (ofN !== undefined) facts.knownNetworks = Number(ofN)
      const none = NO_KNOWN_NETWORK_LINE.exec(text)
      if (none !== null) {
        facts.knownNetworks = Number(none[1])
        facts.unjoined = { known: Number(none[1]), inRange: 0 }
      }
      const noneJoined = NONE_JOINED_LINE.exec(text)
      if (noneJoined !== null) {
        facts.knownNetworks = Number(noneJoined[2])
        facts.unjoined = { known: Number(noneJoined[2]), inRange: Number(noneJoined[1]) }
      }
      // `ff_net_wifi.c:450` (agent ≥ 0.4.6, several known networks) — `joined "<ssid>"
      // (known network K of N)`. The only line that names the network it is actually on.
      const joined = /^joined "(.+)" \(known network \d+ of \d+\)$/.exec(text)?.[1]
      if (joined !== undefined) {
        facts.ssid = joined
        facts.unjoined = null
      }
    } else if (tag === 'ff-net' && / link up/.test(text)) {
      // `ff_net.c:29/33` — "<what> link up, ip A gw B mask C" or "(address unavailable)".
      const ip = /link up, ip (\d+\.\d+\.\d+\.\d+)/.exec(text)?.[1] ?? null
      facts.link = { type: text.startsWith('eth') ? 'ethernet' : 'wifi', ip }
      facts.unjoined = null
    } else if (tag === 'ff-time') {
      // `ff_time.c:82` — "sntp: <before> -> <after> (via <server>)".
      const via = /^sntp: .* -> .*\(via ([^)]+)\)/.exec(text)?.[1]
      if (via !== undefined) {
        facts.ntp = { server: via, set: true }
      } else {
        // `ff_time.c:75` — "sntp: no answer from <server> within N ms; clock is still <iso>".
        const miss = /^sntp: no answer from (\S+) within/.exec(text)
        if (miss !== null) {
          const year = /clock is still (\d{4})-/.exec(text)?.[1]
          facts.ntp = { server: miss[1], set: false, keptYear: year === undefined ? null : Number(year) }
        }
      }
    } else if (tag === 'ff-mqtt' && facts.deviceId === null) {
      // "mqtt connected as <id> (<uri>)" names the id too, for a log that missed `ff-id`.
      const id = MQTT_CONNECTED_LINE.exec(text)?.[1]
      if (id !== undefined) facts.deviceId = id.toLowerCase()
    }
  }
  return facts
}

export type ResultContext = {
  /** Dashboard's one `useFleet`; null while loading or when there is no fleet. */
  devices: DeviceSummary[] | null
  /** `describeVersions(ui, health)`; null means no versions row (standalone panel). */
  versions: VersionLine | null
  /** What this tab just wrote, or null when nothing was flashed here. */
  flashed: {
    deviceId: string | null
    agentVersion: string | null
    layout: string | null
    link: string
    ssid: string | null
  } | null
  /**
   * R2b-fe-5. The fleet as it was when this tab started the flash (`FlashBoard` owns it).
   * Present means this tab flashed the board and minted a fresh token, so the server must
   * show a NEW enrolment before Enrolled is marked from it. Absent or null: no flash here.
   */
  flashBaseline?: FleetBaseline | null
  /** R2b-fe-5. The fleet's 1 Hz clock (`Fleet.now`); drives the server-wait deadline. */
  now?: number
}

export type ResultRow = { label: string; value: string; tone: Tone }

export type OnboardingResult =
  | { outcome: 'success'; rows: ResultRow[]; versionsDiffer: boolean }
  | {
      outcome: 'failure'
      cause: Cause | null
      headline: string
      next: string
      remedy: Remedy | null
      rows: ResultRow[]
      versionsDiffer: boolean
    }

/** The one cause, as a headline. Short: the watch paragraphs above keep the long story. */
export const CAUSE_LABELS: Record<Cause, string> = {
  power: "Power: the board's supply is collapsing (brownout)",
  wifi: 'Wi-Fi: the board could not join the network',
  clock: 'Clock: the board could not get the time',
  server: 'Server: the board could not enrol',
  broker: 'Broker: enrolled, but the message broker is unreachable',
  token: 'Enrolment refused: the token or credential is not valid',
  'download-mode': 'Stuck in download mode',
  firmware: 'Firmware: the agent crashed or is not on the board',
}

/** The one next action as text, shown only when no button renders for it. */
export const CAUSE_NEXT: Record<Cause, string> = {
  power:
    'Use a short, thick USB cable straight into the computer (no hub). If nothing changes, ' +
    "suspect the board's own supply.",
  wifi: 'Check the network name and passphrase in step 2 (2.4 GHz only), then re-flash.',
  clock:
    'Use a network that lets the board reach a time server (UDP port 123), for example a ' +
    'phone hotspot.',
  server: 'Check that this network can reach the fleetforge server, then reboot the board.',
  broker: 'Allow outbound port 8883 on this network, or try another network.',
  token: 'Re-flash the board: every flash mints a fresh single-use token.',
  'download-mode': 'Unplug the board, plug it back in, and press Watch a board.',
  firmware: 'Flash the board again.',
}

const NO_CAUSE_NEXT = 'Copy the diagnostic bundle and send it to whoever is helping you.'

function clockRow(facts: ConsoleFacts): ResultRow {
  const ntp = facts.ntp
  if (ntp === null) return { label: 'Clock source', value: '—', tone: null }
  if (ntp.set) return { label: 'Clock source', value: `NTP (${ntp.server})`, tone: null }
  if (ntp.keptYear !== null && ntp.keptYear >= SANE_YEAR) {
    // The 2026-10-04 bench: the RTC kept a sane time across the flasher's reset.
    return {
      label: 'Clock source',
      value: `Kept across the reset (no answer from ${ntp.server})`,
      tone: null,
    }
  }
  return { label: 'Clock source', value: `Not set (no answer from ${ntp.server})`, tone: 'bad' }
}

/** The failure copy when the last scan cycle ended without an address (R2b-fe-13). */
export function unjoinedCopy(u: { known: number; inRange: number }): {
  headline: string
  next: string
} {
  if (u.inRange === 0) {
    if (u.known === 1) {
      return {
        headline: 'Wi-Fi: its one network is not in range',
        next: 'Check the network name in step 2 (2.4 GHz only), or move the board closer. It keeps trying on its own.',
      }
    }
    return {
      headline: `Wi-Fi: none of its ${u.known} known networks is in range`,
      next: 'It keeps trying on its own. Move it within range of one of its networks, or re-flash it with the network it is near (2.4 GHz only).',
    }
  }
  return {
    headline: `Wi-Fi: ${u.inRange} of its ${u.known} known networks ${u.inRange === 1 ? 'is' : 'are'} in range, but none would take the board`,
    next: 'Check the passphrases in step 2, then re-flash. It keeps trying on its own.',
  }
}

function linkValue(
  facts: ConsoleFacts,
  flashed: ResultContext['flashed'],
  row: DeviceSummary | undefined,
): string {
  const link = facts.link
  if (link === null) return '—'
  const ip = link.ip === null ? 'address unavailable' : `ip ${link.ip}`
  if (link.type === 'ethernet') return `Ethernet · ${ip}`
  const ssid = facts.ssid ?? flashed?.ssid ?? null
  // The count shows from 2 networks up: with one, the operator just typed it (the fleet
  // row shows it from 1, where it answers "can this board move?").
  const n = facts.knownNetworks ?? row?.known_networks ?? null
  const knows = n !== null && n >= 2 ? knowsNetworks(n) : null
  const parts = [ssid === null ? 'Wi-Fi' : `Wi-Fi ${ssid}`]
  if (knows !== null) parts.push(knows)
  parts.push(ip)
  return parts.join(' · ')
}

export function describeOnboardingResult(input: {
  events: ConsoleEvent[]
  summary: ConsoleSummary
  context: ResultContext | null
  /** R2b-fe-5. Milestones in `summary` that only the server saw (`mergeServerView`). */
  fromServer?: Milestone[]
}): OnboardingResult | null {
  const { events, summary } = input
  const fromServer = input.fromServer ?? []
  // With no console line at all, only the server's "on the fleet" earns a card: a console
  // that lost the port is never a failure on its own.
  if (events.length === 0 && !summary.reached.includes('fleet')) return null

  const success = summary.reached.includes('fleet')
  const failure =
    !success &&
    (summary.fault?.remedy != null || summary.overdue !== null || summary.rebootLoop !== null)
  // While the board is still progressing the checklist is the view: a transient
  // `disconnected (reason 2)` that recovers must never flash a failure card.
  if (!success && !failure) return null

  const facts = consoleFacts(events)
  const devices = input.context?.devices ?? null
  const versions = input.context?.versions ?? null
  const flashed = input.context?.flashed ?? null

  const deviceId = facts.deviceId ?? flashed?.deviceId ?? null
  const row =
    deviceId === null || devices === null
      ? undefined
      : devices.find((d) => d.device_id.toLowerCase() === deviceId.toLowerCase())

  const firmware =
    facts.agentVersion ??
    row?.fw_version ??
    (flashed?.agentVersion != null ? `${flashed.agentVersion} (written)` : null)
  const enrolled = summary.reached.includes('enroll') || summary.skipped.includes('enroll')

  let fleetValue = success ? 'yes' : 'no'
  if (fromServer.includes('fleet')) {
    fleetValue = 'yes · server: online (the console did not see it)'
  } else if (devices !== null) {
    fleetValue +=
      row === undefined
        ? ' · server: not seen yet'
        : row.online
          ? ' · server: online'
          : ' · server: offline'
  }

  const rows: ResultRow[] = [
    { label: 'Device id', value: deviceId ?? '—', tone: null },
    { label: 'Firmware', value: firmware ?? '—', tone: null },
    { label: 'Partition layout', value: row?.partition_layout ?? flashed?.layout ?? '—', tone: null },
    { label: 'Link', value: linkValue(facts, flashed, row), tone: null },
    clockRow(facts),
    {
      label: 'Enrolled',
      value: fromServer.includes('enroll') ? 'yes (from the server)' : enrolled ? 'yes' : 'no',
      tone: enrolled ? 'ok' : 'bad',
    },
    { label: 'On the fleet', value: fleetValue, tone: success ? 'ok' : 'bad' },
  ]
  // R2b-fe-4: a board that restarted while watched says so, success or not. Bad while it is
  // still looping; a warning once it has come back.
  const restartSentence = describeRestarts(summary.restarts)
  if (restartSentence !== null) {
    rows.push({
      label: 'Restarts',
      value: restartSentence,
      tone: summary.rebootLoop !== null ? 'bad' : 'warn',
    })
  }
  if (versions !== null) {
    rows.push({ label: 'UI / API', value: `UI ${versions.ui} · API ${versions.api}`, tone: null })
  }
  const versionsDiffer = versions?.differ ?? false

  if (success) return { outcome: 'success', rows, versionsDiffer }

  const { fault, rebootLoop, overdue, waitingFor } = summary
  const cause: Cause | null =
    fault?.cause ??
    (rebootLoop !== null && fault === null ? 'power' : null) ??
    (waitingFor !== null ? MILESTONE_CAUSE[waitingFor] : null)
  const headline =
    cause !== null
      ? CAUSE_LABELS[cause]
      : waitingFor !== null
        ? `Stopped before ${MILESTONE_LABELS[waitingFor]}`
        : 'Stopped short of the fleet'
  const unjoined = cause === 'wifi' && facts.unjoined !== null ? unjoinedCopy(facts.unjoined) : null
  return {
    outcome: 'failure',
    cause,
    headline: unjoined?.headline ?? headline,
    next: unjoined?.next ?? (cause !== null ? CAUSE_NEXT[cause] : NO_CAUSE_NEXT),
    // The same precedence the panel always had: the fault's action first, then the stall's.
    remedy: fault?.remedy ?? overdue?.remedy ?? null,
    rows,
    versionsDiffer,
  }
}
