// The escalation artifact: everything the panel knows about one board, as one string.
//
// On 2026-09-11 the only way to get the log out of this page was to select text inside an
// unlabelled `<pre>`, and the operator could not find it — so the session's evidence was
// retyped into a chat window and then lost. This module is the fix: one function that
// turns `state.events` + `state.summary` + what the flasher page knows into something a
// person can paste at someone who has never seen the board.
//
// **DOM-free and dependency-free on purpose**, the same rule `ffcfg.ts` follows and for
// two reasons: `scripts/emit-bundle.ts` runs it under `vite-node` with no DOM (which is
// the only way to *read* the format as an operator would), and the redaction must be
// unit-testable without rendering anything. The two browser facts the header carries —
// the origin and the user agent — arrive as data.
//
// **The one architectural rule: redaction is a single choke point.** `redactSecrets` runs
// once, over the whole assembled string, as the last statement of `buildDiagnosticBundle`.
// Not per-section and not at the call sites — a section added later by someone who never
// read this file is redacted anyway, by construction. `diagnostics.test.ts` deletes
// exactly that one call to prove the tests are not vacuous.

import type { ConsoleEvent, ConsoleSummary } from './boardConsole'
import { MAX_CONSOLE_LINES, MILESTONE_LABELS } from './boardConsole'
import type { AgentBuildInfo } from './api'
import type { ChipInfo } from './flasher'

export const REDACTED = '[REDACTED]'

/**
 * Below this length a "secret" is scrubbed by the shape rules only.
 *
 * A global search-and-replace for a 2-character string shreds the log — `ff` alone appears
 * in every `ff-agent` tag on screen. Nothing under 8 characters is a valid WPA2 passphrase
 * anyway, and rules 2 and 3 in `redactSecrets` still apply to whatever this floor skips.
 */
export const MIN_SCRUB_LENGTH = 4

/**
 * The form as this page would encode it, with **lengths only** where a value is secret.
 *
 * Mirrors `ff_cfg_log()` in `agent/main/ff_cfg.c` field for field, deliberately: the
 * person reading a bundle and the person reading a UART log should recognise the same
 * block. `null` means the key is omitted from the blob, i.e. the firmware default applies.
 */
export type DiagnosticConfig = {
  apiBase: string
  mqttUri: string
  link: string
  ssid: string
  ntp: string | null
  hbS: string | null
  power: string
  wakeS: string | null
  /** `psk.length`. The value itself goes in `knownSecrets` and nowhere else. */
  pskLength: number
}

/** What the flasher page knows and the console does not. Every field independently
 *  nullable: the panel is also used with no flasher behind it, and `state.build` is null
 *  until a flash has actually run. */
export type DiagnosticContext = {
  /** `state.chip` from `useFlashBoard`. */
  chip: ChipInfo | null
  /** The build this page would flash, and the manifest version behind it. */
  build: AgentBuildInfo | null
  agentVersion: string | null
  /** `GET /v1/healthz` → `version`, or null when it did not answer. */
  serverVersion: string | null
  config: DiagnosticConfig | null
  /**
   * Values that must never appear in the output. **Scrub input only — never rendered.**
   * The builder deliberately cannot tell which is which, so it cannot print one by name.
   */
  knownSecrets: string[]
}

/** The password half of a `scheme://user:pass@host` userinfo, or `[]`.
 *
 *  Exported so a broker password typed into the flash form is scrubbed out of *every*
 *  section, not only out of the URI that carried it. */
export function uriSecrets(uri: string): string[] {
  const match = /^[a-z][a-z0-9+.-]*:\/\/[^\s/@:]+:([^\s/@]+)@/i.exec(uri.trim())
  return match === null ? [] : [match[1]]
}

/**
 * Remove every secret this page could plausibly be holding, whatever printed it.
 *
 * Exported for its own test. Three rules, applied in this order:
 *
 * 1. **Literal scrub** of everything in `knownSecrets`, with `split`/`join` rather than a
 *    `RegExp` — a Wi-Fi passphrase is arbitrary text and `new RegExp(secret)` would be a
 *    regex injection into our own redactor.
 * 2. **URI userinfo**, which works on URIs this page never saw (one the *firmware*
 *    printed, say). The username survives: it is diagnostic and it is not a credential.
 * 3. **Token shapes**, so an `ffe_`/`ffa_` token in a log line is removed even though it
 *    was never in `knownSecrets`. The `{6,}` floor is what keeps the panel's own prose
 *    ("re-flash ff_cfg with a fresh ffe_ token") intact — there is a space after the
 *    underscore there.
 */
export function redactSecrets(text: string, knownSecrets: string[]): string {
  let out = text
  for (const secret of knownSecrets) {
    if (secret.length < MIN_SCRUB_LENGTH) continue
    out = out.split(secret).join(REDACTED)
  }
  out = out.replace(/\b((?:https?|mqtts?|wss?):\/\/[^\s/@:]+):([^\s/@]+)@/g, `$1:${REDACTED}@`)
  out = out.replace(/(ff[ae]_)[A-Za-z0-9._~+/=-]{6,}/g, `$1${REDACTED}`)
  return out
}

/** `  label      value`, two-space indented under a bare heading line so the block
 *  survives a chat client's reflow. */
function field(label: string, value: string): string {
  return `  ${label.padEnd(13)}${value}`
}

/** The last event matching a predicate, or null. The board's own claims about itself are
 *  always the most recent ones it made. */
function lastEvent(
  events: ConsoleEvent[],
  match: (event: ConsoleEvent) => boolean,
): ConsoleEvent | null {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    if (match(events[i])) return events[i]
  }
  return null
}

function headerSection(
  events: ConsoleEvent[],
  context: DiagnosticContext | null,
  page: { origin: string; userAgent: string },
  now: number,
): string[] {
  // Both agent versions, and the pair is the point: they differ exactly when the board is
  // running a stale flash, which is a thing worth escalating with.
  const build = context?.build ?? null
  const agent =
    build === null
      ? (context?.agentVersion ?? 'unknown (nothing has been flashed from this tab)')
      : `${context?.agentVersion ?? build.agent_version} — ${build.target} (${build.chip_family}), ` +
        `idf ${build.idf_version}, commit ${build.source_commit}, layout ${build.partition_layout}`

  const boot = lastEvent(events, (event) => event.milestone === 'boot')
  const id = lastEvent(events, (event) => event.tag === 'ff-id')
  const reportedId = id === null ? null : (/([0-9a-f]{12})/i.exec(id.text)?.[1] ?? id.text)

  return [
    'fleetforge diagnostic bundle',
    // ISO-8601 UTC, never `toLocaleString()`: the recipient is not in the room, and a
    // locale-dependent string makes a test either brittle or vacuous.
    field('generated', new Date(now).toISOString()),
    // `origin`, never `href`. Nothing in this app may create a path that copies a URL —
    // `EnrollBoard.test.tsx` asserts no `ffe_` ever reaches `location.search`.
    field('page', page.origin || '(unknown)'),
    field('browser', page.userAgent || '(unknown)'),
    field('server', context?.serverVersion ?? 'unavailable (this page could not reach /v1/healthz)'),
    field('agent', agent),
    field('on board', boot === null ? 'nothing on this port announced itself as the agent' : boot.text),
    field(
      'device id',
      reportedId === null
        ? 'the board has not said (no ff-id line yet)'
        : `${reportedId} (reported by the board)`,
    ),
  ]
}

/**
 * The diagnosis, in plain English, above everything else in the bundle.
 *
 * The evidence line is deliberately NOT echoed here: it is already in the console log
 * below, verbatim, and a bundle that prints the same line twice makes "how many times did
 * the board brown out?" unanswerable by looking — which is exactly the question the
 * recipient of the 2026-09-11 log asked. So the fault points at its evidence by position
 * instead, and the log stays the single copy of every line the board printed.
 */
function faultSection(events: ConsoleEvent[], summary: ConsoleSummary): string[] {
  const lines = ['fault']
  if (summary.fault === null) {
    lines.push('  nothing in this log names a failure')
    return lines
  }
  lines.push(`  ${summary.fault.hint}`)
  const fault = summary.fault
  let index = -1
  for (let i = events.length - 1; i >= 0; i -= 1) {
    if (events[i].text === fault.text) {
      index = i
      break
    }
  }
  lines.push(
    field(
      'named by',
      index < 0 ? 'a line no longer in the log' : `line ${index + 1} of the console log below`,
    ),
  )
  // The remedy the panel offers for this fault, or `none` when no software fixes it
  // (a cable, a PSK). Named so a reader can tell "we offered nothing" from "we offered
  // something and it did not help".
  lines.push(field('offered', summary.fault.remedy ?? 'no action — this is not fixable from here'))
  return lines
}

function progressSection(summary: ConsoleSummary): string[] {
  return [
    'progress',
    field(
      'reached',
      summary.reached.length === 0
        ? 'nothing yet on this boot'
        : summary.reached.map((m) => MILESTONE_LABELS[m]).join(', '),
    ),
    field(
      'waiting for',
      summary.waitingFor === null ? 'nothing — the board is on the fleet' : MILESTONE_LABELS[summary.waitingFor],
    ),
    field(
      'overdue',
      summary.overdue === null
        ? 'no'
        : `${MILESTONE_LABELS[summary.overdue.milestone]} has not happened in ` +
          `${Math.round(summary.overdue.waitedMs / 1000)} s`,
    ),
    field('boots seen', String(summary.boots)),
    field(
      'reboot loop',
      summary.rebootLoop === null
        ? 'no'
        : `yes — ${summary.rebootLoop.boots} boots without reaching the fleet`,
    ),
  ]
}

function chipSection(chip: ChipInfo | null): string[] {
  if (chip === null) {
    return ['chip', '  not detected in this tab — the console was opened without flashing']
  }
  const predicted =
    chip.macAddress === null ? null : chip.macAddress.replace(/[^0-9a-fA-F]/g, '').toLowerCase()
  return [
    'chip',
    field('description', chip.description),
    field('family', chip.chipName),
    field('MAC', chip.macAddress ?? 'the ROM would not say'),
    field('flash', `${Math.round(chip.flashSizeBytes / (1024 * 1024))} MB`),
    field('features', chip.features.join(', ') || '—'),
    field(
      'device id',
      predicted === null || !/^[0-9a-f]{12}$/.test(predicted)
        ? 'unknown'
        : `${predicted} (predicted from the MAC)`,
    ),
  ]
}

function configSection(config: DiagnosticConfig | null): string[] {
  const heading = 'ff_cfg (as this page would write it)'
  if (config === null) {
    return [heading, '  not available — this console was opened without the flasher form']
  }
  return [
    heading,
    field('api_base', config.apiBase),
    field('mqtt_uri', config.mqttUri),
    field('link', config.link === 'wifi' ? `wifi, ssid ${config.ssid}` : config.link),
    field('ntp', config.ntp === null ? '(firmware default)' : config.ntp || '(disabled)'),
    field('hb_s', config.hbS ?? '(firmware default)'),
    field(
      'power',
      `${config.power} (wake_s ${config.wakeS ?? 'firmware default'})`,
    ),
    // The same sentence `ff_cfg_log()` prints, and for the same reason — except that here
    // the discretion is ours, not the firmware's. The token is not retained by this page
    // at all: `flash.ts` mints it, writes it and drops it.
    field(
      'secrets',
      `token minted per flash (never retained by this page), ` +
        `passphrase ${config.pskLength} chars (never printed)`,
    ),
  ]
}

function logSection(events: ConsoleEvent[]): string[] {
  const heading =
    `console log (${events.length} lines, oldest first; ` +
    `the panel keeps the most recent ${MAX_CONSOLE_LINES})`
  if (events.length === 0) return [heading, '  (empty — nothing has been read from this port)']
  // Verbatim `raw`, in order, with no added per-line timestamps: the ESP-IDF lines already
  // carry ms-since-boot and a prefix would break "paste it back into the same parser".
  // Panel notices are included — "the panel reset the board here" is context the reader
  // needs, and they already self-mark with a leading `— `.
  return [heading, ...events.map((event) => event.raw)]
}

export function buildDiagnosticBundle(input: {
  events: ConsoleEvent[]
  summary: ConsoleSummary
  context: DiagnosticContext | null
  page: { origin: string; userAgent: string }
  /** Same idiom as `summarizeConsole`: the clock is an argument so a test can pin it. */
  now?: number
}): string {
  const { events, summary, context, page, now = Date.now() } = input
  const sections = [
    headerSection(events, context, page, now),
    faultSection(events, summary),
    progressSection(summary),
    chipSection(context?.chip ?? null),
    configSection(context?.config ?? null),
    logSection(events),
  ]
  const text = sections.map((lines) => lines.join('\n')).join('\n\n') + '\n'
  // THE choke point. This must stay the only `return` in this function — see the header.
  return redactSecrets(text, context?.knownSecrets ?? [])
}
