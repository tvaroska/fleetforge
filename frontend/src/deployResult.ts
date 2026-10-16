// The judgement behind the update result card (R2b-fe-10): what a FINISHED deploy
// transaction says, in the operator's terms. Pure: no React, no fetch. The render half is
// `DeployResultCard.tsx`, the same split as `onboardingResult.ts` / `ResultCard.tsx`.
//
// Spec: `spec/flows.md` Flow 2 step 5 RESULT — firmware before and after, GOOD, ROLLED BACK
// or FAILED BEFORE REBOOT, with the reason, UI and API versions, and when. A failure shows
// one cause and one next action. Everything comes from `DeploySummary` (the newest
// transaction on `GET /v1/devices`) and `device.fw_version`; there is no new endpoint.
//
// Rules this file keeps, each one inherited from an earlier decision:
//
// 1. `good` only while the board's announce still equals the confirmed artifact; otherwise
//    the drift wording, never `good`, never "running" (R2-fe-1). The verdict itself is
//    `deploy.ts::deployOutcome`, reused unchanged. "back on X" only when the announce is X.
// 2. A card exists only when the SERVER says the transaction is terminal (`is_terminal`).
// 3. The state vocabulary is open: an unknown terminal state still gets a card, labelled
//    `DEPLOY_STATE_LABELS[state] ?? state`, and every lookup has a fallback.
// 4. Nothing here is announced to assistive tech: the card re-renders every second, so it
//    carries no `role` and no `aria-live` (the component's concern, stated here too).
// 5. Device-controlled text (`detail`) is only ever placed in a string a component renders
//    as text, and is never interpreted. It is also used as a lookup key, so the lookup is
//    `Object.hasOwn`, never a bare index (`constructor` is a legal thing for a board to say).
// 6. No `null`, `undefined`, `NaN` or `%` in any string, and no progress number: `pct` is a
//    transition log (`DeployCell.tsx`).
// 7. No arrow characters: R2-fe-1's tests forbid one beside a verdict, so before and after
//    are two rows.
// 8. `sendAgain` (R2b-fe-11) is set only for a failure before the reboot, with a known
//    version, whose entry says the same build can survive (`resend`). Never after a
//    rollback. The button is offered exactly when the next action says to send it again
//    (a test pins `resend === /send it again/i.test(next)` for every entry). It opens the
//    pre-check and never posts (`DeployCell.tsx`).
//
// 9. The self-test outcome (R5-fe-1) is read ONLY from `deploy.selftest`, which the server
//    parses (R5-be-1). This file never parses the self-test grammar out of `detail`; an
//    older API (no `selftest`) gets no `Self-test` row, whatever the detail says.
// 10. `cancelled` (R5) is an operator's choice, not a failure: no tone, no next action, no
//    Send again. A command rollback (`deploy.ts::isCommandRollback`) is not blamed on the
//    image either.
//
// Not here, on purpose:
// * `Sent by` is server-authored text (R2b-be-4), rendered as text; absent when not recorded.
// * The failed boot's crash reason and last milestone after a rollback: the agent does not
//   report them. Only the board's own `detail` is shown, verbatim.

import { type DeploySelfTest, type DeploySummary, type DeviceSummary } from './api'
import {
  DEPLOY_BAD_STATES,
  DEPLOY_STATE_LABELS,
  deployOutcome,
  driftText,
  isCommandRollback,
} from './deploy'
import { MILESTONE_OF } from './deployTimeline'
import { formatAgo, formatWhen } from './format'
import { type ResultRow } from './onboardingResult'
import { type VersionLine } from './statusStrip'

export type DeployResultOutcome =
  | 'good'
  | 'drift'
  | 'rolled-back'
  | 'failed'
  | 'cancelled'
  | 'other'

export type DeployResult = {
  outcome: DeployResultOutcome
  /** The verdict word. `good` / `rolled back` / `failed before reboot` / a state label. */
  word: string
  tone: 'ok' | 'bad' | null
  /** `deployOutcome`'s note, verbatim (`running 1.5.0`); null when it has none. */
  note: string | null
  /** The drift sentence, for `drift` only. */
  drift: string | null
  reason: string | null
  next: string | null
  ago: string
  agoTitle: string
  rows: ResultRow[]
  versionsDiffer: boolean
  /** The version "Send again" re-checks, or null when not offered (R2b-fe-11). */
  sendAgain: string | null
}

/**
 * What the board's own failure words mean, and the one thing to do about each. Keyed by
 * the detail up to the first colon, so the simulator's `download failed: <Exception>`
 * finds `download failed`. The board's exact words are always shown as well.
 */
type FailureReason = { cause: string; next: string; resend: boolean }

export const FAILURE_REASONS: Record<string, FailureReason> = {
  'download stalled': {
    cause: 'The download stopped: no data arrived for about a minute, so the board gave up.',
    next: 'Check the board’s Wi-Fi signal and that it can reach the server, then send it again.',
    resend: true,
  },
  'download failed': {
    cause: 'The board could not download the image: the connection to the server failed.',
    next: 'Check the board’s network, then send it again.',
    resend: true,
  },
  'truncated download': {
    cause: 'The image arrived incomplete.',
    next: 'Send it again.',
    resend: true,
  },
  'size mismatch': {
    cause: 'The image arrived incomplete.',
    next: 'Send it again.',
    resend: true,
  },
  'sha256 mismatch': {
    cause: 'The downloaded image did not match its checksum, so the board refused to install it.',
    next: 'Send it again; if it happens again, upload the build again.',
    resend: true,
  },
  'cannot open the artifact': {
    cause: 'The board could not start the download from the server.',
    next: 'Check that the board can reach the server, then send it again.',
    resend: true,
  },
  'artifact larger than the ota slot': {
    cause: 'The image is larger than the board’s update slot.',
    next: 'Build a smaller image; the pre-check shows the slot size.',
    resend: false,
  },
  'the running image is not confirmed yet': {
    cause: 'The board is still confirming its current image and takes no new update until it has.',
    next: 'Wait for that to finish, then send it again.',
    resend: true,
  },
  'an update is already staged and waits for a reboot': {
    cause: 'An earlier update is already written and waits for the board to reboot.',
    next: 'Let the board reboot (or power-cycle it), then send it again.',
    resend: true,
  },
  'image validation failed': {
    cause: 'The board found the file is not a valid app image for it.',
    next: 'Check that you picked the app .bin built for this chip, not a merged or bootloader file.',
    resend: false,
  },
}

const UNKNOWN_FAILURE: FailureReason = {
  cause: 'The board stopped the update; its own words are below.',
  next: 'Send it again; if it fails the same way, watch the board’s serial console while it updates.',
  resend: true,
}
const SILENT_FAILURE: FailureReason = {
  cause: 'The update failed and no reason was reported.',
  next: 'Check that the board is online, then send it again.',
  resend: true,
}

// Milestones from the reboot on: a step on one of these means the board got past the point
// where a failure leaves the old image untouched.
const PAST_REBOOT = new Set(['rebooting', 'confirming', 'confirmed'])

/** True only when steps are known and none reached the reboot. Unknown is not "before". */
function failedBeforeReboot(deploy: DeploySummary): boolean {
  const steps = deploy.steps
  if (steps === undefined || steps.length === 0) return false
  return !steps.some((step) => {
    const milestone = MILESTONE_OF[step.state]
    return milestone !== undefined && PAST_REBOOT.has(milestone)
  })
}

// The `Self-test` row's words per server outcome. A lookup with a fallback: an outcome
// this file has not heard of renders as itself.
const SELFTEST_WORDS: Record<string, string> = {
  passed: 'passed',
  failed: 'failed',
  timed_out: 'timed out',
}

const nonEmpty = (text: string | null | undefined): string | null =>
  typeof text === 'string' && text.trim() !== '' ? text : null

/** The `Self-test` row value, from the server's parse only (rule 9). */
export function selfTestRow(selftest: DeploySelfTest): string {
  const outcome = String(selftest.outcome)
  let value = Object.hasOwn(SELFTEST_WORDS, outcome) ? SELFTEST_WORDS[outcome] : outcome
  const reason = nonEmpty(selftest.reason)
  if (outcome === 'failed' && reason !== null) value += `: ${reason}`
  const returned = nonEmpty(selftest.returned_to)
  if (returned !== null) value += ` (returned to ${returned})`
  return value
}

function failureLookup(detail: string | null): FailureReason {
  if (detail === null || detail.trim() === '') return SILENT_FAILURE
  const key = detail.split(':')[0].trim()
  return Object.hasOwn(FAILURE_REASONS, key) ? FAILURE_REASONS[key] : UNKNOWN_FAILURE
}

/**
 * The update result card's content, or `null` while the transaction is in flight (or there
 * is none). `device.fw_version` is the board's last announce; `versions` is
 * `describeVersions(ui, health)` or null for no `UI / API` row.
 */
export function deployResult(
  deploy: DeploySummary | null,
  device: Pick<DeviceSummary, 'fw_version'>,
  versions: VersionLine | null,
  now: number,
): DeployResult | null {
  if (deploy === null || !deploy.is_terminal) return null

  const fw = device.fw_version
  const from = deploy.from_version
  const v = deploy.artifact_version
  const target = v ?? 'the new image'
  const detail = deploy.detail !== null && deploy.detail !== '' ? deploy.detail : null
  const backOnFrom = from !== null && fw === from

  const verdict = deployOutcome(deploy, fw)
  const selftest = deploy.selftest ?? null

  let outcome: DeployResultOutcome
  let word: string
  let tone: 'ok' | 'bad' | null
  let note: string | null = null
  let drift: string | null = null
  let reason: string | null = null
  let next: string | null = null
  let after: string | null
  let sendAgain: string | null = null

  if (verdict?.kind === 'verdict' && deploy.state === 'confirmed') {
    outcome = 'good'
    word = verdict.word
    tone = verdict.tone
    note = verdict.note
    reason =
      selftest?.outcome === 'passed'
        ? `The board rebooted into ${target}, its self-test passed, and it confirmed it, so it keeps this image.`
        : `The board rebooted into ${target}, reconnected to the server and confirmed it, so it keeps this image.`
    after = v ?? 'the new image'
  } else if (verdict?.kind === 'drift') {
    outcome = 'drift'
    word = DEPLOY_STATE_LABELS.confirmed
    tone = null
    drift = driftText(verdict.reported)
    reason =
      'This update was confirmed, but the board has reported another version since, so it is not called good.'
    after = `${v ?? 'the new image'} when it was confirmed`
  } else if (verdict?.kind === 'verdict') {
    // `rolled_back`.
    outcome = 'rolled-back'
    word = verdict.word
    tone = verdict.tone
    note = verdict.note
    const back = backOnFrom ? from : 'the previous image'
    const window =
      typeof deploy.confirm_timeout_s === 'number' && Number.isFinite(deploy.confirm_timeout_s)
        ? `within ${deploy.confirm_timeout_s} s of its reboot`
        : 'in time'
    if (isCommandRollback(deploy)) {
      // An operator's rollback command: nothing is wrong with an image, so no next action.
      reason = `A rollback command returned the board to ${back}.`
    } else if (selftest?.outcome === 'failed') {
      const why = nonEmpty(selftest.reason)
      reason =
        `${target} started, but its own self-test failed${why !== null ? `: “${why}”` : ''}, ` +
        `so the board went back to ${back} at once.`
      next = `Do not send ${target} again as it is: fix what its self-test checks, then upload a fixed version.`
    } else if (selftest?.outcome === 'timed_out') {
      reason = `${target} started, but its self-test did not report ${window}, so the board went back to ${back}.`
      next =
        'Check on a board you can watch over USB that the self-test finishes in time, then upload a fixed version.'
    } else {
      reason =
        `${target} did not confirm, so the board went back to ${back} on its own. ` +
        `That happens when the new image does not reconnect ${window}, or resets before it confirms ` +
        '(a crash, a boot loop or a power cut); the board does not say which.'
      next =
        `Do not send ${target} again as it is: check that it boots and reconnects on a board you ` +
        'can watch over USB, then upload a fixed version.'
    }
    after = backOnFrom ? `${from} again` : 'the previous image'
  } else if (deploy.state === 'failed') {
    outcome = 'failed'
    tone = 'bad'
    const before = failedBeforeReboot(deploy)
    word = before ? 'failed before reboot' : DEPLOY_STATE_LABELS.failed
    const found = failureLookup(deploy.detail)
    reason = found.cause
    next = found.next
    if (before && v !== null && found.resend) sendAgain = v
    if (before) {
      after = backOnFrom ? `${from}, unchanged: ${target} was not installed` : `${target} was not installed`
    } else {
      after = fw !== null ? `the board reports ${fw}` : 'not reported'
    }
  } else if (deploy.is_terminal && deploy.state === 'cancelled') {
    outcome = 'cancelled'
    word = 'cancelled'
    tone = null
    reason = `The update was cancelled before the board switched to it, so ${target} was not installed.`
    after = backOnFrom ? `${from}, unchanged` : `${target} was not installed`
  } else {
    outcome = 'other'
    word = DEPLOY_STATE_LABELS[deploy.state] ?? deploy.state
    tone = DEPLOY_BAD_STATES.has(deploy.state) ? 'bad' : null
    after = fw !== null ? `the board reports ${fw}` : 'not reported'
  }

  const rows: ResultRow[] = [
    { label: 'Before', value: from ?? 'not recorded', tone: null },
    { label: 'After', value: after, tone: null },
  ]
  if (detail !== null) rows.push({ label: 'Board says', value: detail, tone: null })
  if (selftest !== null) rows.push({ label: 'Self-test', value: selfTestRow(selftest), tone: null })
  const sentAt = deploy.steps?.[0]?.at
  if (sentAt !== undefined && Number.isFinite(Date.parse(sentAt))) {
    rows.push({ label: 'Sent', value: formatWhen(sentAt), tone: null })
  }
  const sender = deploy.sent_by
  if (sender && typeof sender.subject === 'string' && sender.subject !== '') {
    const credential =
      typeof sender.credential === 'string' && sender.credential !== '' ? sender.credential : null
    rows.push({
      label: 'Sent by',
      value: credential !== null ? `${sender.subject} · ${credential}` : sender.subject,
      tone: null,
    })
  }
  rows.push({
    label: 'Finished',
    value: `${formatWhen(deploy.at)} (${formatAgo(deploy.at, now)})`,
    tone: null,
  })
  if (versions !== null) {
    rows.push({ label: 'UI / API', value: `UI ${versions.ui} · API ${versions.api}`, tone: null })
  }

  return {
    outcome,
    word,
    tone,
    note,
    drift,
    reason,
    next,
    ago: formatAgo(deploy.at, now),
    agoTitle: formatWhen(deploy.at),
    rows,
    versionsDiffer: versions?.differ ?? false,
    sendAgain,
  }
}
