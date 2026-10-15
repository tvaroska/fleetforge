// The non-rendering half of the device log tail (R4-fe-2): the one read of
// `GET /v1/devices/{id}/logs` and the pure helpers over it. `DeviceLog.tsx` is the
// rendering half, the `profiles.ts` / `PartitionProfiles.tsx` split.
//
// The rules that shape this file.
//
// 1. **The server's order is the order.** The API answers newest first, `(logged_at DESC,
//    id DESC)`, and only the last 7 days. Nothing here sorts.
// 2. **A gap sits BELOW the line that carries it.** `dropped` on a line is the number of
//    lines lost immediately before it, so in a newest-first list the loss is between that
//    line and the next older one.
// 3. **`text: null` is a gap marker**, a batch that lost lines and had none usable. It is a
//    gap row, never an empty line.
// 4. **`level` is an open vocabulary.** The ESP-IDF letters are labelled; anything else
//    renders as itself.
// 5. **The hint is the selected board's `last_seen`.** An `up/log` batch moves it (the
//    ingestor touches it before decoding), and `useFleet` re-reads the fleet when a frame
//    arrives. So the hook re-reads when that string changes: no new EventSource (each one
//    costs an API `sse_max_clients` slot), no poll of its own, and nothing is ever read
//    from an SSE body (`fleet.ts` rule 1).
// 6. **Lines belong to the board they were read for.** State is stored with its device id
//    and a different id reads as "loading", so board A's lines never show under board B.

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api, type DeviceLogLine } from './api'

/** Mirrors the server's `LOG_TAIL_DEFAULT`; always sent explicitly. "Load more" is a follow-up. */
export const LOG_TAIL_LIMIT = 200

/** A lookup with a fallback, never exhaustive: the server keeps any printable 1-8 char level. */
export const LOG_LEVEL_LABELS: Record<string, string> = {
  E: 'error',
  W: 'warning',
  I: 'info',
  D: 'debug',
  V: 'verbose',
}

export type LevelTone = 'bad' | 'warn' | 'muted'

/** The level as shown (the raw string), its label for a `title`, and its second axis. */
export function describeLevel(level: string): {
  level: string
  label: string | null
  tone: LevelTone | null
} {
  const label = LOG_LEVEL_LABELS[level] ?? null
  const tone: LevelTone | null =
    level === 'E' ? 'bad' : level === 'W' ? 'warn' : level === 'D' || level === 'V' ? 'muted' : null
  return { level, label, tone }
}

export type TailRow =
  | { kind: 'line'; line: DeviceLogLine }
  | { kind: 'gap'; key: string; dropped: number; at: string }

/**
 * The rows to draw, in the server's order. A line with `dropped > 0` is followed by a gap
 * row (the loss is below it); a marker (`text === null`) is a gap row only.
 * Adjacent gaps are never merged: each is a stored fact.
 */
export function tailRows(lines: readonly DeviceLogLine[]): TailRow[] {
  const rows: TailRow[] = []
  for (const line of lines) {
    if (line.text !== null) rows.push({ kind: 'line', line })
    if (line.dropped > 0) {
      rows.push({ kind: 'gap', key: `gap-${line.id}`, dropped: line.dropped, at: line.logged_at })
    }
  }
  return rows
}

export function formatDropped(n: number): string {
  return n === 1 ? '1 line missing here' : `${n} lines missing here`
}

export type DeviceLogState = {
  /** `null` while the first read for this board is in flight. A failed re-read keeps the last lines. */
  lines: DeviceLogLine[] | null
  /** A read failure, rendered in the section. It must not blank the page. */
  error: string | null
}

type Stored = { deviceId: string; lines: DeviceLogLine[] | null; error: string | null }

/**
 * One `GET /v1/devices/{id}/logs?limit=200` on mount, whenever `deviceId` changes and
 * whenever `hint` changes. `deviceId === null` reads nothing. A response that is not the
 * newest request is dropped.
 *
 * A 401 bounces to the login gate; a 404 means the board left the fleet; any other failure
 * becomes a string and the last lines of the SAME board stay on screen.
 */
export function useDeviceLog({
  deviceId,
  hint,
  onSessionExpired,
}: {
  deviceId: string | null
  hint: string | null
  onSessionExpired: () => void
}): DeviceLogState {
  const [stored, setStored] = useState<Stored | null>(null)

  const expiredRef = useRef(onSessionExpired)
  expiredRef.current = onSessionExpired
  const liveRef = useRef(true)
  const seqRef = useRef(0)

  const load = useCallback(async (id: string) => {
    const seq = ++seqRef.current
    try {
      const result = await api.deviceLogs(id, LOG_TAIL_LIMIT)
      if (!liveRef.current || seq !== seqRef.current) return
      // `?? []` for an API older than R4-be-1, or a mock that answers `{}`.
      setStored({ deviceId: id, lines: result.lines ?? [], error: null })
    } catch (err) {
      if (!liveRef.current || seq !== seqRef.current) return
      if (err instanceof ApiError && err.isUnauthorized) {
        expiredRef.current()
        return
      }
      const message =
        err instanceof ApiError && err.status === 404
          ? 'this board is no longer in the fleet'
          : err instanceof Error && err.message !== ''
            ? err.message
            : "could not read this board's log"
      setStored((prev) => ({
        deviceId: id,
        lines: prev !== null && prev.deviceId === id ? prev.lines : null,
        error: message,
      }))
    }
  }, [])

  useEffect(() => {
    liveRef.current = true
    return () => {
      liveRef.current = false
    }
  }, [])

  useEffect(() => {
    // `hint` is a dependency on purpose: it is the re-read trigger, not an input.
    if (deviceId !== null) void load(deviceId)
  }, [deviceId, hint, load])

  if (deviceId === null || stored === null || stored.deviceId !== deviceId) {
    return { lines: null, error: null }
  }
  return { lines: stored.lines, error: stored.error }
}
