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

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api, type ArtifactSummary } from './api'

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
  confirmed: 'done — running the new version',
  rolling_back: 'rolling back',
  rolled_back: 'rolled back to the previous version',
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
export function useArtifacts({ onSessionExpired }: { onSessionExpired: () => void }): Artifacts {
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
      const result = await api.listArtifacts()
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
  }, [])

  useEffect(() => {
    liveRef.current = true
    void load()
    return () => {
      liveRef.current = false
    }
  }, [load])

  return { byTarget, loaded, error, reload: () => void load() }
}
