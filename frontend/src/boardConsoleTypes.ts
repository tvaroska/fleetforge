// Shared types, constants, and pure milestone/restart formatters for the board UART console.

/** Cap console DOM growth when a board is left watched for hours. */
export const MAX_CONSOLE_LINES = 500

/** ESP-IDF's default UART console baud rate (distinct from the 921600 flash baud). */
export const CONSOLE_BAUD_RATE = 115200

export type ConsoleLevel = 'error' | 'warn' | 'info' | 'plain'

/**
 * A fix the panel can perform itself over the serial port: pulse EN, or take the port
 * back and write a fresh `ff_cfg` with a newly minted token.
 */
export type Remedy = 'reboot' | 'reflash'

/** Single-word category of a specific boot or onboarding failure (`R2b-fe-3`). */
export type Cause =
  | 'power'
  | 'wifi'
  | 'clock'
  | 'server'
  | 'broker'
  | 'token'
  | 'download-mode'
  | 'firmware'

/**
 * Button copy for a remedy, kept distinct from other page button labels so accessible-name
 * queries remain unambiguous.
 */
export const REMEDY_LABELS: Record<Remedy, string> = {
  reboot: 'Reboot and retry',
  reflash: 'Re-flash the board',
}

/** Ordered boot milestones; the first unreached milestone identifies a stall. */
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
  seq: number
  at: number
  raw: string
  level: ConsoleLevel
  tag: string | null
  text: string
  milestone: Milestone | null
  bootMarker: 'rom' | 'agent' | null
  hint: string | null
  hintKind: 'generic' | 'specific'
  remedy: Remedy | null
  cause: Cause | null
  source: 'board' | 'panel'
  commandedReset: boolean
}

export type ResetReason =
  | 'power-on'
  | 'brownout'
  | 'panic'
  | 'watchdog'
  | 'restart'
  | 'external'
  | 'deep-sleep'
  | 'download-mode'
  | 'unknown'

export const RESET_REASON_LABELS: Record<ResetReason, string> = {
  'power-on': 'power-on',
  brownout: 'brownout',
  panic: 'panic',
  watchdog: 'watchdog',
  restart: 'software restart',
  external: 'reset pin or USB reset',
  'deep-sleep': 'deep-sleep wake',
  'download-mode': 'download mode',
  unknown: 'reason not reported',
}

export type BootReset = {
  reason: ResetReason
  rom: string | null
}

export type Restart = BootReset & {
  at: number
  stage: Milestone | null
}

export type MilestoneStall = {
  milestone: Milestone
  waitedMs: number
  hint: string
  remedy: Remedy | null
}

export type ConsoleSummary = {
  reached: Milestone[]
  skipped: Milestone[]
  waitingFor: Milestone | null
  fault: { text: string; hint: string; remedy: Remedy | null; cause: Cause | null } | null
  boots: number
  rebootLoop: { boots: number } | null
  lastReset: (BootReset & { commanded: boolean }) | null
  restarts: Restart[]
  retracted: Milestone[]
  overdue: MilestoneStall | null
}

export function resetLabel(reset: BootReset): string {
  return reset.reason === 'unknown' && reset.rom !== null
    ? `other (${reset.rom})`
    : RESET_REASON_LABELS[reset.reason]
}

export const RESTART_STAGE: Record<Milestone, string> = {
  boot: 'during startup',
  link: 'during network startup',
  clock: 'while setting the clock',
  enroll: 'while enrolling',
  fleet: 'while connecting to the broker',
}

export function describeRestarts(restarts: Restart[]): string | null {
  if (restarts.length === 0) return null
  let out = `Rebooted ${restarts.length}\u00d7`
  const stages = new Set(restarts.map((r) => r.stage))
  if (stages.size === 1) {
    const stage = restarts[0].stage
    out += stage === null ? ' after reaching the fleet' : ` ${RESTART_STAGE[stage]}`
  }
  const groups = new Map<string, { n: number; last: number }>()
  restarts.forEach((r, i) => {
    const label = resetLabel(r)
    groups.set(label, { n: (groups.get(label)?.n ?? 0) + 1, last: i })
  })
  if (groups.size === 1) return `${out}: ${[...groups.keys()][0]}`
  const ranked = [...groups.entries()].sort((a, b) => b[1].n - a[1].n || b[1].last - a[1].last)
  return `${out}: ${ranked.map(([label, g]) => `${label} ${g.n}\u00d7`).join(', ')}`
}

export const MILESTONE_DEADLINE_MS: Record<Milestone, number> = {
  boot: 5_000,
  link: 45_000,
  clock: 30_000,
  enroll: 60_000,
  fleet: 30_000,
}

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

export const MILESTONE_REMEDY: Record<Milestone, Remedy | null> = {
  boot: 'reflash',
  link: null,
  clock: null,
  enroll: 'reflash',
  fleet: null,
}

export const MILESTONE_CAUSE: Record<Milestone, Cause> = {
  boot: 'firmware',
  link: 'wifi',
  clock: 'clock',
  enroll: 'server',
  fleet: 'broker',
}

export const NO_KNOWN_NETWORK_LINE = /^no known network in range \((\d+) known\)/
export const NONE_JOINED_LINE =
  /^none of the (\d+) known networks? in range could be joined \((\d+) known\)/

export type ConsoleAcquire = 'granted' | 'prompt'

export interface BoardConsole {
  lines(): AsyncIterable<string>
  reboot(): Promise<void>
  close(): Promise<void>
}

export type ConsoleFactory = (options: {
  baudRate: number
  acquire: ConsoleAcquire
  onLog: (line: string) => void
}) => Promise<BoardConsole>
