// The server's half of "watch the board" (R2b-fe-5). Pure: no React, no fetch.
//
// `spec/flows.md` Flow 1 step 5: the dashboard "watches Web Serial and the server enrollment
// stream in parallel (so a native-USB ESP32-S3/C3 port reset never causes a false failure)".
// A native-USB board re-enumerates when it is reset, so the console can lose the port (the
// 8 s `getPorts()` window in `serialConsole.ts` runs out, or the stream ends right after the
// EN pulse) while the board itself boots, enrols and joins the fleet perfectly well. The
// server saw all of that. This file decides what the server's view proves.
//
// The one source is `Dashboard`'s single `useFleet` (`fleet.ts`): `GET /v1/devices` re-read
// on every `/v1/events` frame plus a 10 s poll. Never a second `useFleet` or EventSource
// (one `sse_max_clients` slot each), and never a row patched from an SSE payload.
//
// Rules:
//
// 1. **Judged against a baseline, never the browser clock.** Every timestamp on a
//    `DeviceSummary` is a server receipt time on one clock (models.py rule 3). Comparing them
//    to `Date.now()` would make the answer depend on the operator's clock skew. Instead the
//    panel compares the row to a snapshot of the fleet taken at an anchor moment (the start
//    of the flash, or the first watch when nothing was flashed here).
// 2. **The re-flash trap.** Re-flashing a known, online board: its stale row still says
//    `online: true` with the OLD `enrolled_at` until `/v1/enroll` runs (the re-enrol resets
//    presence and `broker_provisioned_at`, and leaves `last_seen` untouched). Without the
//    baseline comparison the card would say "On the fleet" before the new image has booted.
//    So after a flash, Enrolled needs a NEW `enrolled_at` and a credential in dynsec, and On
//    the fleet needs `online` plus `last_seen > enrolled_at` — the board spoke to the broker
//    after THIS enrolment (the same idea as `progress.py::has_already_arrived`).
// 3. **`online` is read as-is** (`fleet.ts` rule 1). `last_seen > enrolled_at` only proves
//    "spoke after this enrolment"; it never stands in for presence.
// 4. **Only Enrolled and On the fleet.** Arrival stages are not used for Network up / Clock
//    set: arrival filtering makes them a poor per-session signal.
// 5. **Merged, not forked.** `boardConsole.ts`'s classifier and `summarizeConsole` are
//    unchanged; `mergeServerView` adds the server's milestones to the console's summary and
//    re-applies the furthest-reached rule.

import { type DeviceSummary } from './api'
import {
  MILESTONES,
  MILESTONE_DEADLINE_MS,
  type ConsoleEvent,
  type ConsoleSummary,
  type Milestone,
} from './boardConsole'
import { DEVICE_ID_LINE, MQTT_CONNECTED_LINE } from './onboardingResult'

/** The fleet's rows at the anchor moment, keyed by lowercased device id. */
export type FleetBaseline = {
  rows: Record<string, { enrolled_at: string; last_seen: string | null }>
}

export function takeBaseline(devices: DeviceSummary[]): FleetBaseline {
  const rows: FleetBaseline['rows'] = {}
  for (const device of devices) {
    rows[device.device_id.toLowerCase()] = {
      enrolled_at: device.enrolled_at,
      last_seen: device.last_seen,
    }
  }
  return { rows }
}

export type ServerView = { deviceId: string; enrolled: boolean; onFleet: boolean }

/** Server times compared as instants, never as strings. */
function at(iso: string): number {
  return Date.parse(iso)
}

/**
 * What the server's device list proves about this board, or null when there is nothing to
 * judge (no id, no fleet, or no baseline yet).
 *
 * `expectEnroll` is true when this tab flashed the board: a fresh token was minted, so only
 * a NEW enrolment counts. Otherwise the board keeps a credential from an earlier enrolment
 * and nothing re-enrols it; On the fleet then needs `last_seen` to move past the baseline.
 */
export function describeServerView(input: {
  deviceId: string | null
  devices: DeviceSummary[] | null
  baseline: FleetBaseline | null
  expectEnroll: boolean
}): ServerView | null {
  const { deviceId, devices, baseline, expectEnroll } = input
  if (deviceId === null || devices === null || baseline === null) return null
  const id = deviceId.toLowerCase()
  const row = devices.find((d) => d.device_id.toLowerCase() === id)
  const prior = Object.hasOwn(baseline.rows, id) ? baseline.rows[id] : undefined
  if (row === undefined) return { deviceId: id, enrolled: false, onFleet: false }

  const enrolled =
    row.broker_provisioned_at !== null &&
    (!expectEnroll || prior === undefined || at(row.enrolled_at) !== at(prior.enrolled_at))
  const onFleet =
    enrolled &&
    row.online &&
    row.last_seen !== null &&
    at(row.last_seen) > at(row.enrolled_at) &&
    (expectEnroll ||
      prior === undefined ||
      prior.last_seen === null ||
      at(row.last_seen) !== at(prior.last_seen))
  return { deviceId: id, enrolled, onFleet }
}

/**
 * The last device id the board printed in ANY boot. Not `consoleFacts`, which resets at each
 * boot marker: a port dropped right after a reset banner would otherwise lose the id.
 */
export function lastDeviceId(events: ConsoleEvent[]): string | null {
  let id: string | null = null
  for (const event of events) {
    if (event.source !== 'board') continue
    const match =
      event.tag === 'ff-id'
        ? DEVICE_ID_LINE.exec(event.text)
        : event.tag === 'ff-mqtt'
          ? MQTT_CONNECTED_LINE.exec(event.text)
          : null
    if (match !== null) id = match[1].toLowerCase()
  }
  return id
}

/**
 * How long the panel waits on the server alone once the console has stopped, before saying
 * so (`spec/standards.md`: no unbounded wait). The console's own budget for the two
 * milestones the server can mark.
 */
export const SERVER_WAIT_MS = MILESTONE_DEADLINE_MS.enroll + MILESTONE_DEADLINE_MS.fleet

/**
 * The console's summary with the server's milestones added. `fromServer` lists the ones the
 * console did not reach this boot. `consoleStopped`: the console's deadline clock stopped
 * with the port, so its frozen `overdue` is stale once the server is watching
 * (`SERVER_WAIT_MS` replaces it).
 */
export function mergeServerView(
  summary: ConsoleSummary,
  server: ServerView | null,
  consoleStopped: boolean,
): { summary: ConsoleSummary; fromServer: Milestone[] } {
  if (server === null) return { summary, fromServer: [] }

  const reached = new Set<Milestone>(summary.reached)
  const fromServer: Milestone[] = []
  if (server.enrolled && !reached.has('enroll')) {
    reached.add('enroll')
    fromServer.push('enroll')
  }
  if (server.onFleet && !reached.has('fleet')) {
    reached.add('fleet')
    fromServer.push('fleet')
  }

  // The same furthest-reached rule `summarizeConsole` uses: a later milestone proves the
  // earlier ones were passed.
  const ordered = MILESTONES.filter((m) => reached.has(m))
  const furthest = MILESTONES.reduce((acc, m, i) => (reached.has(m) ? i : acc), -1)
  const skipped = MILESTONES.filter((m, i) => i < furthest && !reached.has(m))
  const waitingFor = MILESTONES[furthest + 1] ?? null
  const retracted = summary.retracted.filter((m) => !reached.has(m) && !skipped.includes(m))

  const overdue =
    summary.overdue === null ||
    server.onFleet ||
    consoleStopped ||
    MILESTONES.indexOf(summary.overdue.milestone) <= furthest
      ? null
      : summary.overdue

  return {
    summary: {
      ...summary,
      reached: ordered,
      skipped,
      waitingFor,
      retracted,
      overdue,
      // Being on the fleet invalidates every earlier fault, as the console's own `fleet` does.
      fault: server.onFleet ? null : summary.fault,
    },
    fromServer,
  }
}
