// The API's own build, as the strip reads it (R2b-fe-1).
//
// Polled, not read once. A tab left open across a server deploy is exactly the stale-
// bundle case the strip exists to reveal: read once on mount, it would show the API
// version as of page load and never notice that the API moved on underneath it.
// `/v1/healthz` is unauthenticated and does no I/O, so a minute is cheap.
//
// A failure here says nothing about the operator's session, so this hook never touches
// session state and never calls `onSessionExpired` (same rule as `FlashBoard.tsx`).

import { useEffect, useState } from 'react'
import { api, type Health } from './api'

export const HEALTH_POLL_MS = 60_000

export type HealthState =
  | { phase: 'loading' }
  | { phase: 'ok'; health: Health }
  | { phase: 'unreachable' }

export function useHealth(pollMs: number = HEALTH_POLL_MS): HealthState {
  const [state, setState] = useState<HealthState>({ phase: 'loading' })

  useEffect(() => {
    let live = true
    const read = () => {
      api
        .health()
        .then((health) => {
          if (live) setState({ phase: 'ok', health })
        })
        .catch(() => {
          if (live) setState({ phase: 'unreachable' })
        })
    }
    read()
    // Cleared on unmount: StrictMode mounts twice in dev.
    const timer = setInterval(read, pollMs)
    return () => {
      live = false
      clearInterval(timer)
    }
  }, [pollMs])

  return state
}
