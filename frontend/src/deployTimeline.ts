// The update timeline (R2b-fe-9): the pure half. Decides everything the timeline shows —
// the milestones, the elapsed seconds, the deadline sentence and the stall sentence — from
// `deploy.steps` (the current transaction's recorded transitions, oldest first), the
// device's presence and the shared `now`. `DeployTimeline.tsx` only renders the answer.
//
// The rules this file keeps (each one a decision recorded in DECISIONS.md):
//
// 1. **No percentage, no progress bar.** `pct` is a transition log, not a feed; nothing
//    here reads it.
// 2. **`awaiting_safe_window` gets no deadline and no stall, ever.** It may last forever
//    (the device owns the reboot). Elapsed time is a fact and is shown; "stuck" is not.
// 3. **The client never authors an outcome.** `terminal` is the server's `is_terminal`,
//    never re-derived from state names. A stall sentence states the BOARD's own rule
//    and when it has passed; it never changes the state label, the verdict or styling.
// 4. **Open vocabulary.** Every state lookup is a `Record` with a fallback. An unknown
//    state renders as itself and gets no deadline and no stall.
// 5. **Never print `null`, `undefined`, `NaN` or `%`.** Unknown versions get words,
//    unparseable times get no offset, and every elapsed time is clamped at 0 (the
//    browser clock is compared against server receipt times).

import { type DeploySummary, type DeviceSummary } from './api'
import { DEPLOY_STATE_LABELS } from './deploy'
import { formatDuration } from './format'

// `agent/main/ff_ota.c` OTA_STALL_MS: the board aborts a download that has received no
// data for 60 s (R2-fw-5).
export const BOARD_STALL_MIN_S = 60
// Measured: the abort lands about 80 s after the last byte (R2-fw-5, QEMU). Past this,
// a board still at `downloading` is either still receiving or has not reported yet.
export const BOARD_STALL_MAX_S = 80
// Display threshold only, not a policy: an online board takes a command within seconds.
export const ACK_WAIT_S = 30
// Display slack only: the previous image has to boot and reconnect before it can report
// `rolled_back`, so the window is called "passed" this long after it closes.
export const ROLLBACK_REPORT_SLACK_S = 60

/** The spec's six (`spec/flows.md` Flow 2 step 4 WATCH), in order. */
export const MILESTONES = [
  'sent',
  'downloading',
  'staged',
  'rebooting',
  'confirming',
  'confirmed',
] as const
export type Milestone = (typeof MILESTONES)[number]

export const MILESTONE_LABELS: Record<Milestone, string> = {
  sent: 'sent',
  downloading: 'downloading',
  staged: 'staged',
  rebooting: 'rebooting',
  confirming: 'confirming',
  confirmed: 'confirmed',
}

/**
 * Which milestone a raw state belongs to. A lookup: a state missing here (`failed`,
 * `rolling_back`, `rolled_back`, `idle`, anything newer than this file) is on no
 * milestone and is shown as itself.
 */
export const MILESTONE_OF: Record<string, Milestone | undefined> = {
  requested: 'sent',
  staging: 'downloading',
  downloading: 'downloading',
  verifying: 'downloading',
  staged: 'staged',
  awaiting_safe_window: 'staged',
  applying: 'staged',
  rebooting: 'rebooting',
  confirming: 'confirming',
  confirmed: 'confirmed',
}

export type TimelineItem = {
  key: string
  label: string
  status: 'done' | 'implied' | 'current' | 'pending' | 'failed'
  /** Already formatted (`+12 s`); null for implied and pending items, or an unparseable time. */
  offset: string | null
}

export type DeployTimeline = {
  items: TimelineItem[]
  /** The server's `is_terminal`, verbatim. */
  terminal: boolean
  /** `elapsed 1 min 3 s` in flight, `took 2 min 14 s` once terminal. */
  elapsed: string
  /** `downloading the image for 42 s` in flight; null once terminal. */
  current: string | null
  /** A muted sentence stating the board's own rule for the current state. */
  deadline: string | null
  /** A warn sentence, shown only once that rule's time has passed. */
  stall: string | null
}

const stateLabel = (state: string) => DEPLOY_STATE_LABELS[state] ?? state

const QUEUED =
  'Queued: the board is offline, and the broker holds the command until it next connects.'
const DOWNLOAD_DEADLINE =
  'The board says nothing more until the image is written. If data stops arriving, it ' +
  'gives up on its own 60 to 80 s after the last byte and reports “download stalled”.'

/** The deadline and stall sentences for an in-flight transaction (DECISIONS, R2b-fe-9 D4). */
function deadlineAndStall(
  deploy: DeploySummary,
  device: Pick<DeviceSummary, 'online' | 'fw_version'>,
  stepAt: (state: string) => number,
  sinceLastMs: number,
  now: number,
): { deadline: string | null; stall: string | null } {
  const v = deploy.artifact_version ?? 'the new image'
  const from = deploy.from_version ?? 'the previous version'

  switch (deploy.state) {
    case 'requested':
      // A sleepy or offline board may take hours: queued is a fact, never a stall.
      if (!device.online) return { deadline: QUEUED, stall: null }
      if (sinceLastMs < ACK_WAIT_S * 1000) return { deadline: null, stall: null }
      return {
        deadline: null,
        stall:
          `No answer from the board after ${formatDuration(sinceLastMs)}. An online board ` +
          'normally takes a command within seconds; the broker holds it until the board does.',
      }

    case 'downloading': {
      if (sinceLastMs <= BOARD_STALL_MAX_S * 1000) return { deadline: DOWNLOAD_DEADLINE, stall: null }
      let stall =
        `No word from the board for ${formatDuration(sinceLastMs)}. It may still be ` +
        `downloading: it gives up only when no data has arrived for ${BOARD_STALL_MIN_S} to ` +
        `${BOARD_STALL_MAX_S} s, and then reports “download stalled”.`
      if (!device.online) {
        stall += ' The board is offline now; it reports what happened when it reconnects.'
      }
      return { deadline: DOWNLOAD_DEADLINE, stall }
    }

    case 'rebooting':
    case 'confirming': {
      const T = deploy.confirm_timeout_s
      const rebooting = stepAt('rebooting')
      const anchor = Number.isFinite(rebooting) ? rebooting : stepAt('applying')
      // No anchor or no window from the server → say nothing rather than invent one.
      if (typeof T !== 'number' || !Number.isFinite(T) || !Number.isFinite(anchor)) {
        return { deadline: null, stall: null }
      }
      const closes = anchor + T * 1000
      if (now < closes + ROLLBACK_REPORT_SLACK_S * 1000) {
        const remaining = Math.max(0, closes - now)
        const left = remaining < 1000 ? '(due now)' : `(${formatDuration(remaining)} left)`
        return {
          deadline: `If ${v} never connects, the board rolls back on its own about ${T} s after the reboot ${left}.`,
          stall: null,
        }
      }
      let stall =
        `The ${T} s rollback window has passed with no result. A board that rolled back ` +
        'reports “rolled back” once the previous image reconnects.'
      const art = deploy.artifact_version
      const prev = deploy.from_version
      if (!device.online) stall += ' The board is offline now.'
      else if (art !== null && device.fw_version === art) {
        stall += ` It now reports running ${art}, but sent no result for this update.`
      } else if (prev !== null && device.fw_version === prev) {
        stall += ` It now reports running ${prev} again.`
      }
      return { deadline: null, stall }
    }

    case 'rolling_back':
      return {
        deadline: `The board is going back to ${from}; it reports “rolled back” once that image reconnects.`,
        stall: null,
      }

    // `awaiting_safe_window` (binding rule 2), every other known state, and every state
    // this file has never heard of: no deadline, no stall.
    default:
      return { deadline: null, stall: null }
  }
}

export function deployTimeline(
  deploy: DeploySummary,
  device: Pick<DeviceSummary, 'online' | 'fw_version'>,
  now: number,
): DeployTimeline {
  const steps =
    deploy.steps !== undefined && deploy.steps.length > 0
      ? deploy.steps
      : [{ state: deploy.state, at: deploy.at }]
  const times = steps.map((step) => Date.parse(step.at))
  const firstAt = times[0]
  const lastAt = times[times.length - 1]

  const offset = (at: number): string | null =>
    Number.isFinite(at) && Number.isFinite(firstAt) ? `+${formatDuration(at - firstAt)}` : null
  const stepAt = (state: string): number => {
    const i = steps.findIndex((step) => step.state === state)
    return i < 0 ? Number.NaN : times[i]
  }

  // A milestone's time is the FIRST step mapped to it.
  const reachedAt = new Map<Milestone, number>()
  steps.forEach((step, i) => {
    const milestone = MILESTONE_OF[step.state]
    if (milestone !== undefined && !reachedAt.has(milestone)) reachedAt.set(milestone, times[i])
  })
  let furthest = -1
  MILESTONES.forEach((milestone, i) => {
    if (reachedAt.has(milestone)) furthest = i
  })

  // A later milestone implies every earlier one: `implied`, never `pending`.
  const reached = (i: number): TimelineItem => {
    const milestone = MILESTONES[i]
    const at = reachedAt.get(milestone)
    return at === undefined
      ? { key: milestone, label: MILESTONE_LABELS[milestone], status: 'implied', offset: null }
      : { key: milestone, label: MILESTONE_LABELS[milestone], status: 'done', offset: offset(at) }
  }
  const range = (from: number, to: number) =>
    Array.from({ length: Math.max(0, to - from) }, (_, k) => from + k)

  const terminal = deploy.is_terminal
  const items: TimelineItem[] = []
  const currentMilestone = MILESTONE_OF[deploy.state]

  if (terminal) {
    for (const i of range(0, furthest + 1)) items.push(reached(i))
    if (currentMilestone !== 'confirmed') {
      // A failure, a rollback or a terminal state this file does not know: one end item.
      items.push({
        key: `end:${deploy.state}`,
        label: stateLabel(deploy.state),
        status: 'failed',
        offset: offset(lastAt),
      })
    }
  } else if (currentMilestone !== undefined) {
    const idx = MILESTONES.indexOf(currentMilestone)
    for (const i of range(0, idx)) items.push(reached(i))
    const at = reachedAt.get(currentMilestone)
    items.push({
      key: currentMilestone,
      label: MILESTONE_LABELS[currentMilestone],
      status: 'current',
      offset: at === undefined ? null : offset(at),
    })
    for (const i of range(idx + 1, MILESTONES.length)) {
      items.push({ key: MILESTONES[i], label: MILESTONE_LABELS[MILESTONES[i]], status: 'pending', offset: null })
    }
  } else {
    // `rolling_back` or a state on no milestone: where it sits among the six is unknown,
    // so the reached ones stay done and nothing is promised after it.
    for (const i of range(0, furthest + 1)) items.push(reached(i))
    items.push({
      key: `now:${deploy.state}`,
      label: stateLabel(deploy.state),
      status: 'current',
      offset: offset(lastAt),
    })
  }

  const sinceLastMs = Math.max(0, now - lastAt)
  const { deadline, stall } = terminal
    ? { deadline: null, stall: null }
    : deadlineAndStall(deploy, device, stepAt, Number.isFinite(sinceLastMs) ? sinceLastMs : 0, now)

  return {
    items,
    terminal,
    elapsed: terminal
      ? `took ${formatDuration(lastAt - firstAt)}`
      : `elapsed ${formatDuration(now - firstAt)}`,
    current: terminal ? null : `${stateLabel(deploy.state)} for ${formatDuration(now - lastAt)}`,
    deadline,
    stall,
  }
}
