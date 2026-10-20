// The non-rendering half of the Deploy cell (R1-fe-1): the state vocabulary, and the
// one read of `GET /v1/artifact`. `DeployCell.tsx` is the rendering half, the same split
// `fleet.ts` / `FleetView.tsx` uses.
//
// Two rules shape this file.
//
// 1. **The state vocabulary is open.** `deploy_events.state` is TEXT with no CHECK and
//    the server's `DeployState` is explicitly advisory, so a board running an agent
//    newer than this dashboard can report a state nobody here has heard of. The labels
//    below are therefore a lookup WITH A FALLBACK — deliberately not a `switch`, the
//    same idiom and the same reason as `FleetView.tsx`'s `STAGE_LABELS`.
//
// 2. **Nothing here expires a deploy.** `awaiting_safe_window` may last forever: a
//    vehicle in motion, a drone in the air. The device owns the reboot
//    (`design/architecture.md` principle 5), the server has no sweeper, and this client
//    must not invent one — no timeout, no "stuck?" badge after N minutes, no spinner.
//    Its label says the wait is legitimate and unbounded, which is the whole point.
//    The update timeline's stall text (R2b-fe-9, `deployTimeline.ts`) is not a client
//    policy either: it states the BOARD's own deadlines, never ends a deploy, and never
//    applies to `awaiting_safe_window`.

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api, type ArtifactSummary, type DeploySummary } from './api'

/**
 * Human wording for the states in `spec/device-protocol.md`, plus the server-authored
 * `requested`. A lookup with an `?? raw` fallback, never exhaustive.
 *
 * The wording carries most of this feature's value, so it is phrased for an operator
 * watching a board rather than for someone reading the protocol. `awaiting_safe_window`
 * gets a whole sentence because it is the one state that LOOKS like a hang and is not:
 * it names who is waiting and says the wait may be indefinite.
 */
export const DEPLOY_STATE_LABELS: Record<string, string> = {
  requested: 'sent to the board',
  staging: 'starting',
  downloading: 'downloading the image',
  verifying: 'checking the image',
  staged: 'image written, waiting to reboot',
  awaiting_safe_window:
    'waiting for a safe moment — the board decides when, and may wait indefinitely',
  applying: 'switching to the new image',
  rebooting: 'rebooting',
  confirming: 'confirming the new image',
  confirmed: 'confirmed by the board',
  rolling_back: 'rolling back',
  rolled_back: 'rolled back to the previous version',
  // R5: an operator's Cancel ended the transaction before the board switched images.
  // Not in `DEPLOY_BAD_STATES`: it is an operator action, not a failure.
  cancelled: 'cancelled — the board keeps its current image',
  failed: 'failed',
  idle: 'idle',
}

/**
 * The only two states rendered as a problem. `confirmed` renders `ok`; everything else,
 * including `awaiting_safe_window` and every state this file has not heard of, is plain
 * text.
 *
 * `rolled_back` is here because the operator's board is not running the version they
 * asked for — but note that for the fleet-safety KPI a rollback is a SAVE, not a loss
 * (`db/models.py::DeployEvent`). Colour is never the only signal: the label is already
 * the word (S0-fe-2 / WCAG 1.4.1).
 *
 * This is about STYLING and is not a terminal-state list. `is_terminal` comes from the
 * server on every `DeploySummary`; do not re-derive it from this set.
 */
export const DEPLOY_BAD_STATES = new Set(['failed', 'rolled_back'])

/**
 * The verdict words, keyed by the state that earns them. A lookup, like every vocabulary
 * here, and consulted only when the SERVER says the transaction is terminal (`is_terminal`
 * is never re-derived from state names).
 */
export const DEPLOY_OUTCOMES: Record<string, { word: string; tone: 'ok' | 'bad' }> = {
  confirmed: { word: 'good', tone: 'ok' },
  rolled_back: { word: 'rolled back', tone: 'bad' },
}

// The two exact strings the agent (firmware and simulator) writes on
// `awaiting_safe_window` (R5-fw-5). Device text, matched by equality only.
export const APPLY_WAIT_DETAIL = 'apply waits for a safe window'
export const ROLLBACK_WAIT_DETAIL = 'rollback waits for a safe window'

/** The label of a rollback that waits for the board's safe moment (R5-fe-1). */
export const ROLLBACK_WAIT_LABEL =
  'rolling back at the board’s next safe moment — it has decided, and may wait indefinitely'

// Raw states from the reboot on. Kept here on raw names rather than through
// `deployTimeline.ts::MILESTONE_OF`, which imports this file: no circular import.
const AFTER_REBOOT_STATES = new Set(['rebooting', 'confirming'])

/**
 * What an `awaiting_safe_window` deploy is waiting to do (R5-fe-1), or null for any other
 * state. The board's own detail decides when it is one of the two exact sentences;
 * otherwise (no detail, e.g. an `apply: auto` stage held by the simulator, or a detail this
 * file has not heard of) the recorded steps do: a wait after a reboot is a rollback wait.
 * Never expires either wait (rule 2 in the header).
 */
export function safeWindowWait(
  deploy: Pick<DeploySummary, 'state' | 'detail' | 'steps'>,
): 'apply' | 'rollback' | null {
  if (deploy.state !== 'awaiting_safe_window') return null
  if (deploy.detail === ROLLBACK_WAIT_DETAIL) return 'rollback'
  if (deploy.detail === APPLY_WAIT_DETAIL) return 'apply'
  const steps = deploy.steps ?? []
  return steps.some((step) => AFTER_REBOOT_STATES.has(step.state)) ? 'rollback' : 'apply'
}

/**
 * The state's label, with the one case a state name alone cannot tell: a rollback that
 * waits for a safe window. An apply wait keeps the `awaiting_safe_window` label.
 */
export function deployStateLabel(
  deploy: Pick<DeploySummary, 'state' | 'detail' | 'steps'>,
): string {
  if (safeWindowWait(deploy) === 'rollback') return ROLLBACK_WAIT_LABEL
  return DEPLOY_STATE_LABELS[deploy.state] ?? deploy.state
}

/**
 * True for a `rolled_back` caused by an operator's rollback command (R5-be-2): firmware and
 * simulator write the same head, `rolled back by command; returned to <slot>`. One rule,
 * exact equality on the head; the detail is never otherwise interpreted.
 */
export function isCommandRollback(deploy: Pick<DeploySummary, 'state' | 'detail'>): boolean {
  if (deploy.state !== 'rolled_back' || deploy.detail === null) return false
  return deploy.detail.split(';')[0].trim() === 'rolled back by command'
}

export type DeployOutcome =
  | { kind: 'verdict'; word: string; tone: 'ok' | 'bad'; note: string }
  // Confirmed, but the board's own announce disagrees: history, not a present-tense verdict.
  | { kind: 'drift'; reported: string | null }

/**
 * What a finished transaction should say, or `null` to render the in-flight line as before.
 *
 * The drift rule is the CUJ-1 hard-fail trap in `spec/cujs.md` ("a milestone was shown as
 * reached while it was no longer true"): `good` is claimed only while `fw_version` still
 * equals the confirmed artifact. Likewise "back on X" is claimed only when the announce
 * agrees. Neither case lets the client author an outcome the server did not record.
 */
export function deployOutcome(
  deploy: DeploySummary,
  fwVersion: string | null,
): DeployOutcome | null {
  if (!deploy.is_terminal) return null
  const entry = DEPLOY_OUTCOMES[deploy.state]
  if (entry === undefined) return null
  const v = deploy.artifact_version
  if (deploy.state === 'confirmed') {
    if (v !== null && fwVersion !== v) return { kind: 'drift', reported: fwVersion }
    return {
      kind: 'verdict',
      ...entry,
      note: v !== null ? `running ${v}` : 'running the new version',
    }
  }
  if (deploy.state === 'rolled_back') {
    const from = deploy.from_version
    let note = isCommandRollback(deploy)
      ? 'by an operator command'
      : v !== null
        ? `${v} did not confirm`
        : 'the new image did not confirm'
    if (from !== null && fwVersion === from) note += `; back on ${from}`
    return { kind: 'verdict', ...entry, note }
  }
  return null
}

/**
 * The one sentence for a confirmed deploy whose board has since announced something else
 * (R2b-fe-10 moved it here so `LiveState` and the update result card say the same words).
 */
export function driftText(reported: string | null): string {
  return reported === null
    ? 'the board has not reported a version since'
    : `the board has since reported ${reported}, so this is not what it runs now`
}

export type Artifacts = {
  /** Deployable versions per chip target, each group already newest-first. */
  byTarget: Map<string, ArtifactSummary[]>
  /** `false` until the first read lands — "loading", as distinct from "none uploaded". */
  loaded: boolean
  /** A read failure, rendered inside the cell. It must not blank the fleet table. */
  error: string | null
  /** Re-read. Exported for a future upload UI; nothing in R1 polls this. */
  reload: () => void
}

/**
 * One `GET /v1/artifact` on mount, grouped by `target`.
 *
 * **No poll and no SSE subscription.** Artifacts change only when a human uploads one,
 * and there is no event type for it — `fleetforge.events.EventType` has none, and adding
 * a poll here would spend a request every N seconds on a list that changes by hand.
 *
 * A 401 bounces to the login gate (`EnrollBoard`'s `handle` pattern); any other failure
 * becomes a string the cell renders, because a broken artifact list must not take the
 * fleet table down with it.
 */
export function useArtifacts({
  onSessionExpired,
  project,
}: {
  onSessionExpired: () => void
  project?: string
}): Artifacts {
  const [byTarget, setByTarget] = useState<Map<string, ArtifactSummary[]>>(() => new Map())
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Read through a ref so `load` depends on nothing and the mount effect runs once —
  // the same reason `fleet.ts` holds its callbacks this way.
  const expiredRef = useRef(onSessionExpired)
  expiredRef.current = onSessionExpired
  const liveRef = useRef(true)

  const load = useCallback(async () => {
    try {
      const result = await api.listArtifacts(project)
      if (!liveRef.current) return
      const grouped = new Map<string, ArtifactSummary[]>()
      // `?? []` for the same reason `fleet.ts` guards `arrivals`: a dashboard served
      // against an API older than this endpoint renders an empty picker, not a crash.
      for (const artifact of result.artifacts ?? []) {
        // The server orders by `(target, created_at DESC, version)`, so pushing in
        // arrival order keeps each group newest-first without re-sorting here.
        const group = grouped.get(artifact.target)
        if (group === undefined) grouped.set(artifact.target, [artifact])
        else group.push(artifact)
      }
      setByTarget(grouped)
      setError(null)
      setLoaded(true)
    } catch (err) {
      if (!liveRef.current) return
      if (err instanceof ApiError && err.isUnauthorized) {
        expiredRef.current()
        return
      }
      setError(err instanceof Error ? err.message : 'could not read the artifact list')
      // Loaded, in the sense the cell cares about: the read finished and there is
      // nothing to offer, so the cell explains itself instead of saying "Loading…".
      setLoaded(true)
    }
  }, [project])

  useEffect(() => {
    liveRef.current = true
    void load()
    return () => {
      liveRef.current = false
    }
  }, [load])

  return { byTarget, loaded, error, reload: () => void load() }
}
