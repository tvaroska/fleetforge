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
//
// Not here, on purpose:
// * `Sent by` is server-authored text (R2b-be-4), rendered as text; absent when not recorded.
// * A "Send again" button: `R2b-fe-11`. The next action may say "send it again" in words.
// * The failed boot's crash reason and last milestone after a rollback: the agent does not
//   report them. Only the board's own `detail` is shown, verbatim.

import { type DeploySummary, type DeviceSummary } from './api'
import { DEPLOY_BAD_STATES, DEPLOY_STATE_LABELS, deployOutcome, driftText } from './deploy'
import { MILESTONE_OF } from './deployTimeline'
import { formatAgo, formatWhen } from './format'
import { type ResultRow } from './onboardingResult'
import { type VersionLine } from './statusStrip'

export type DeployResultOutcome = 'good' | 'drift' | 'rolled-back' | 'failed' | 'other'

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
}

/**
 * What the board's own failure words mean, and the one thing to do about each. Keyed by
 * the detail up to the first colon, so the simulator's `download failed: <Exception>`
 * finds `download failed`. The board's exact words are always shown as well.
 */
export const FAILURE_REASONS: Record<string, { cause: string; next: string }> = {
  'download stalled': {
    cause: 'The download stopped: no data arrived for about a minute, so the board gave up.',
    next: 'Check the board’s Wi-Fi signal and that it can reach the server, then send it again.',
  },
  'download failed': {
    cause: 'The board could not download the image: the connection to the server failed.',
    next: 'Check the board’s network, then send it again.',
  },
  'truncated download': {
    cause: 'The image arrived incomplete.',
    next: 'Send it again.',
  },
  'size mismatch': {
    cause: 'The image arrived incomplete.',
    next: 'Send it again.',
  },
  'sha256 mismatch': {
    cause: 'The downloaded image did not match its checksum, so the board refused to install it.',
    next: 'Send it again; if it happens again, upload the build again.',
  },
  'cannot open the artifact': {
    cause: 'The board could not start the download from the server.',
    next: 'Check that the board can reach the server, then send it again.',
  },
  'artifact larger than the ota slot': {
    cause: 'The image is larger than the board’s update slot.',
    next: 'Build a smaller image; the pre-check shows the slot size.',
  },
  'the running image is not confirmed yet': {
    cause: 'The board is still confirming its current image and takes no new update until it has.',
    next: 'Wait for that to finish, then send it again.',
  },
  'an update is already staged and waits for a reboot': {
    cause: 'An earlier update is already written and waits for the board to reboot.',
    next: 'Let the board reboot (or power-cycle it), then send it again.',
  },
  'image validation failed': {
    cause: 'The board found the file is not a valid app image for it.',
    next: 'Check that you picked the app .bin built for this chip, not a merged or bootloader file.',
  },
}

const UNKNOWN_FAILURE = {
  cause: 'The board stopped the update; its own words are below.',
  next: 'Send it again; if it fails the same way, watch the board’s serial console while it updates.',
}
const SILENT_FAILURE = {
  cause: 'The update failed and no reason was reported.',
  next: 'Check that the board is online, then send it again.',
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

function failureLookup(detail: string | null): { cause: string; next: string } {
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

  let outcome: DeployResultOutcome
  let word: string
  let tone: 'ok' | 'bad' | null
  let note: string | null = null
  let drift: string | null = null
  let reason: string | null = null
  let next: string | null = null
  let after: string | null

  if (verdict?.kind === 'verdict' && deploy.state === 'confirmed') {
    outcome = 'good'
    word = verdict.word
    tone = verdict.tone
    note = verdict.note
    reason = `The board rebooted into ${target}, reconnected to the server and confirmed it, so it keeps this image.`
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
    reason =
      `${target} did not confirm, so the board went back to ${back} on its own. ` +
      `That happens when the new image does not reconnect ${window}, or resets before it confirms ` +
      '(a crash, a boot loop or a power cut); the board does not say which.'
    next =
      `Do not send ${target} again as it is: check that it boots and reconnects on a board you ` +
      'can watch over USB, then upload a fixed version.'
    after = backOnFrom ? `${from} again` : 'the previous image'
  } else if (deploy.state === 'failed') {
    outcome = 'failed'
    tone = 'bad'
    const before = failedBeforeReboot(deploy)
    word = before ? 'failed before reboot' : DEPLOY_STATE_LABELS.failed
    const found = failureLookup(deploy.detail)
    reason = found.cause
    next = found.next
    if (before) {
      after = backOnFrom ? `${from}, unchanged: ${target} was not installed` : `${target} was not installed`
    } else {
      after = fw !== null ? `the board reports ${fw}` : 'not reported'
    }
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
  }
}
