// The non-rendering half of the partition-profiles section (R3-fe-1): the one read of
// `GET /v1/partition-profiles` and the pure helpers over it. `PartitionProfiles.tsx` is the
// rendering half, the `deploy.ts` / `DeployCell.tsx` split.
//
// Two rules shape this file.
//
// 1. **The server is the gate.** `effectiveLayout` mirrors `LayoutCatalog.resolve`
//    (R3-be-2 D7) for DISPLAY and for the upload form's default only. Whether a deploy is
//    allowed is the pre-check's answer, never this file's.
// 2. **There is no event type for profiles.** They change on an operator action or on a
//    board's announce, so the hook re-reads on mount, after an adopt (`reload`), and when
//    `hint` changes (`useFleet().announceSeq`). No second EventSource and no poll.

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api, type DeviceSummary, type PartitionProfileSummary } from './api'

export type Profiles = {
  /** `null` until the first read lands, and again never: a failed re-read keeps the last list. */
  profiles: PartitionProfileSummary[] | null
  /** A read failure, rendered under the section heading. It must not blank the page. */
  error: string | null
  /** Re-read now, after the operator named a map. */
  reload: () => void
}

/**
 * One `GET /v1/partition-profiles` on mount, again whenever `hint` changes to a positive
 * value, and on `reload`. A response that is not the newest request is dropped, so a slow
 * read cannot overwrite the list an adopt just refreshed.
 *
 * A 401 bounces to the login gate; any other failure becomes a string, because a broken
 * profile list must not take the fleet table or the upload form down with it.
 */
export function useProfiles({
  onSessionExpired,
  hint,
}: {
  onSessionExpired: () => void
  hint: number
}): Profiles {
  const [profiles, setProfiles] = useState<PartitionProfileSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const expiredRef = useRef(onSessionExpired)
  expiredRef.current = onSessionExpired
  const liveRef = useRef(true)
  const seqRef = useRef(0)

  const load = useCallback(async () => {
    const seq = ++seqRef.current
    try {
      const result = await api.listPartitionProfiles()
      if (!liveRef.current || seq !== seqRef.current) return
      // `?? []` for a dashboard served against an API older than R3-be-2.
      setProfiles(result.profiles ?? [])
      setError(null)
    } catch (err) {
      if (!liveRef.current || seq !== seqRef.current) return
      if (err instanceof ApiError && err.isUnauthorized) {
        expiredRef.current()
        return
      }
      setError(err instanceof Error ? err.message : 'could not read the partition profiles')
    }
  }, [])

  useEffect(() => {
    liveRef.current = true
    void load()
    return () => {
      liveRef.current = false
    }
  }, [load])

  useEffect(() => {
    if (hint > 0) void load()
  }, [hint, load])

  return { profiles, error, reload: () => void load() }
}

/** The name of every deployable profile, sorted and de-duplicated: what an upload may name. */
export function adoptedNames(profiles: readonly PartitionProfileSummary[]): string[] {
  const names = profiles.flatMap((p) => (p.deployable && p.layout_id !== null ? [p.layout_id] : []))
  return [...new Set(names)].sort()
}

/** The detected maps still waiting for a name, in the server's order (oldest first). */
export function pendingProfiles(
  profiles: readonly PartitionProfileSummary[],
): PartitionProfileSummary[] {
  return profiles.filter((p) => !p.deployable)
}

/**
 * The adopted profile a board is on, or `null`: the announced id if it is an adopted name;
 * else the adopted profile whose fingerprint is the board's; else none. The server's order
 * (R3-be-2 D7), so an `unknown` board on an adopted map resolves to that map's name.
 */
export function effectiveLayout(
  device: Pick<DeviceSummary, 'partition_layout' | 'partition_table_sha256'>,
  profiles: readonly PartitionProfileSummary[],
): string | null {
  const adopted = adoptedNames(profiles)
  if (device.partition_layout != null && adopted.includes(device.partition_layout)) {
    return device.partition_layout
  }
  const sha = device.partition_table_sha256
  if (sha != null) {
    const match = profiles.find(
      (p) => p.deployable && p.layout_id !== null && p.partition_table_sha256 === sha,
    )
    if (match !== undefined) return match.layout_id
  }
  return null
}

/** A lookup with a fallback, never exhaustive: see `PartitionProfileOrigin` in `api.ts`. */
export const ORIGIN_LABELS: Record<string, string> = {
  builtin: 'built in',
  user: 'defined by you',
  detected: 'detected',
}

/** The first 12 hex characters of a fingerprint; the full value goes in a `title`. */
export function shortSha(sha: string): string {
  return `${sha.slice(0, 12)}…`
}
