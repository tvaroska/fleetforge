// The fleet's refresh engine: `GET /v1/devices` + `GET /v1/events`.
//
// Three rules shape every line of this file. Each of them is a decision already made
// on the server side (`fleetforge/events.py`, `fleetforge/presence.py`,
// `api/routers/devices.py`), and each is easy to "optimise" away:
//
// 1. **The event is a hint; `GET /v1/devices` is the record.** A frame on the stream
//    means "something changed, go re-read". Nothing here ever patches a row from an
//    SSE payload, and nothing here ever recomputes `online` from `last_seen` — the
//    tolerance rule lives once, in `fleetforge.presence.is_online`. `DeviceEvent`
//    carries an `online` snapshot for other consumers; the browser reads it as a
//    trigger and nothing more.
//
// 2. **A sleepy board goes offline with NO event at all.** Its presence is
//    `now - last_seen < 2.5 x expected_wake_interval_s`, evaluated on read by the
//    server; nothing publishes when that timer expires. So the list is ALSO re-read on
//    a plain interval, not only on frames. A purely event-driven dashboard shows a dead
//    sleepy board as online forever and passes every unit test you would think to write.
//
// 3. **`EventSource` CLOSED is not `EventSource` CONNECTING.** Per spec, a non-200
//    response (401 when the cookie dies, 503 when `sse_max_clients` is exhausted) puts
//    the object in CLOSED permanently: `onerror` fires once and the browser never
//    reconnects. A network blip instead leaves it CONNECTING and the browser retries
//    itself. Treating the two alike either gives up on a transient blip or spins
//    against a 401.

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api, type DeviceSummary } from './api'

// jsdom has no `EventSource`, so the hook takes a factory rather than reaching for the
// global — the same seam/adapter idiom the Python side uses for the broker provisioner
// and the object store. Structural on purpose: a test double needs no DOM.
export interface EventSourceLike {
  readonly readyState: number
  close(): void
  onopen: ((ev: Event) => unknown) | null
  // Only `data` is read: the server sends no `event:` name (so `onmessage` fires for
  // every frame) and no `id:`, because there is no replay. The parameter is the real
  // `MessageEvent` so that a live `EventSource` satisfies this interface without a cast.
  onmessage: ((ev: MessageEvent<string>) => unknown) | null
  onerror: ((ev: Event) => unknown) | null
}

export type EventSourceFactory = (url: string) => EventSourceLike

// `readyState` per the HTML spec: 0 CONNECTING, 1 OPEN, 2 CLOSED. Spelled as a number
// because `EventSource.CLOSED` is a static on a global jsdom does not have — reading it
// at module scope throws in the tests.
const CLOSED = 2

// Relative, so the same-origin `ff_session` cookie rides along automatically. NOT
// `{ withCredentials: true }`: that is a cross-origin concept, and this app has one
// origin by construction. NO token in the query string — nginx logs `$request`.
export const EVENTS_URL = '/v1/events'

/**
 * How long a burst of frames is folded into a single re-read. Well inside the 2 s
 * `spec/prd.md` -> *Timing* allows between server receipt and the dashboard reflecting it.
 */
export const COALESCE_MS = 250

/**
 * The event-independent re-read (rule 2 above). At `spec/prd.md` -> *Capacity* = 25
 * devices this is one trivial indexed query every 10 s, nowhere near a load concern,
 * and it bounds how long a sleepy board that stopped waking can read as online.
 */
export const POLL_MS = 10_000

// Mirrors `api/eventstream.py`'s RECONNECT_INITIAL_DELAY / RECONNECT_MAX_DELAY, so an
// operator reading both sides of the stream does not have to learn two reconnect idioms.
const RECONNECT_INITIAL_MS = 1000
const RECONNECT_MAX_MS = 30_000

export type StreamState = 'connecting' | 'live' | 'reconnecting' | 'offline'

export type Fleet = {
  /** `null` until the first read lands — "loading", as distinct from "no boards". */
  devices: DeviceSummary[] | null
  /** A transport/5xx problem. The last good list stays on screen underneath it. */
  error: string | null
  stream: StreamState
  /** `Date.now()` of the last successful read; drives the "last seen" column's ticking. */
  lastSyncedAt: number | null
  /** Wall clock, refreshed once a second, so relative ages tick without refetching. */
  now: number
}

export type UseFleetOptions = {
  onSessionExpired: () => void
  createEventSource?: EventSourceFactory
}

const defaultFactory: EventSourceFactory = (url) => new EventSource(url)

export function useFleet({ onSessionExpired, createEventSource }: UseFleetOptions): Fleet {
  const [devices, setDevices] = useState<DeviceSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [stream, setStream] = useState<StreamState>('connecting')
  const [lastSyncedAt, setLastSyncedAt] = useState<number | null>(null)
  const [now, setNow] = useState(() => Date.now())

  // The factory and the expiry callback are read through refs so the stream effect can
  // depend on nothing and therefore run exactly once per mount: re-running it would
  // tear down and rebuild the EventSource on every parent render, and each rebuild
  // costs one of the API's `sse_max_clients` slots until the old one is reaped.
  const factoryRef = useRef<EventSourceFactory>(createEventSource ?? defaultFactory)
  factoryRef.current = createEventSource ?? defaultFactory
  const expiredRef = useRef(onSessionExpired)
  expiredRef.current = onSessionExpired

  // A relative-age tick, deliberately separate from the data refresh: "12 s ago" must
  // advance every second, and refetching once a second to make it do so would be silly.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const stateRef = useRef({
    /** Cleared by the effect cleanup; every async continuation checks it. */
    live: true,
    /** Monotonic request id. A response that is not the newest request is dropped. */
    seq: 0,
    inFlight: false,
    /** A frame arrived while a read was in flight — re-read once it lands. */
    again: false,
  })

  // The zombie-stream guard, and it is not theoretical: verified against `just up`, when
  // the api container is stopped the dev proxy holds the `/v1/events` socket open, so
  // `onerror` NEVER fires. The page then reports `Live` at a stream that is dead, and
  // stays dead after the api returns — the exact "is the fleet quiet, or is this page
  // broken?" confusion the keepalives exist to prevent.
  //
  // The read model is the detector. The api serves `/v1/devices` and `/v1/events` from
  // one process, so a failed read means the stream is gone whatever `readyState` claims;
  // the next successful read is when it is worth rebuilding. `reset` is installed by the
  // stream effect below and nulled by its cleanup.
  const streamRef = useRef<{ suspect: boolean; reset: (() => void) | null }>({
    suspect: false,
    reset: null,
  })

  const refresh = useCallback(async () => {
    const state = stateRef.current
    if (!state.live) return
    if (state.inFlight) {
      // Single in flight: a slow API must not let bursty heartbeats stack requests.
      state.again = true
      return
    }
    const seq = ++state.seq
    state.inFlight = true
    try {
      const result = await api.listDevices()
      // `GET /v1/devices` responses can land out of order under bursty heartbeats;
      // without this guard an older list overwrites a newer one and the table goes
      // backwards. Same reason the server stamps one `now` per response.
      if (!state.live || seq !== state.seq) return
      setDevices(result.devices)
      setError(null)
      setLastSyncedAt(Date.now())
      if (streamRef.current.suspect) {
        streamRef.current.suspect = false
        streamRef.current.reset?.()
      }
    } catch (err) {
      if (!state.live) return
      if (err instanceof ApiError && err.isUnauthorized) {
        // The cookie is dead. Stop the engine: `SessionGate` is about to swap this whole
        // subtree for the login form, and a poll that keeps firing 401s in the meantime
        // would call back into a screen that has already moved on.
        state.live = false
        expiredRef.current()
        return
      }
      // A dead API is not a dead session (`session.tsx` makes the same distinction).
      // Keep the last list on screen with a banner over it: a stale fleet the operator
      // can see is better than a blank page, and blanking it looks like a wiped fleet.
      setError(err instanceof Error ? err.message : 'could not read the fleet')
      // Whatever the EventSource believes, nothing is reaching this page.
      streamRef.current.suspect = true
      setStream('reconnecting')
    } finally {
      state.inFlight = false
      if (state.live && state.again) {
        state.again = false
        void refresh()
      }
    }
  }, [])

  useEffect(() => {
    const state = stateRef.current
    state.live = true
    state.seq = 0
    state.inFlight = false
    state.again = false

    let source: EventSourceLike | null = null
    let coalesce: ReturnType<typeof setTimeout> | null = null
    let reconnect: ReturnType<typeof setTimeout> | null = null
    let backoff = RECONNECT_INITIAL_MS

    /** Detach and close the current source, so a late callback from it is a no-op. */
    function discard() {
      if (source === null) return
      source.onopen = null
      source.onmessage = null
      source.onerror = null
      source.close()
      source = null
    }

    function scheduleRefresh() {
      if (coalesce !== null) return
      coalesce = setTimeout(() => {
        coalesce = null
        void refresh()
      }, COALESCE_MS)
    }

    function connect() {
      if (!state.live) return
      const es = factoryRef.current(EVENTS_URL)
      source = es

      es.onopen = () => {
        if (!state.live) return
        setStream('live')
        backoff = RECONNECT_INITIAL_MS
        // Every stream ends eventually — `sse_max_stream_s` caps it at 15 min, and a
        // Postgres listener reconnect calls `EventHub.close_all()` on purpose. Both are
        // normal, and both leave a gap; re-reading on open is the resync path, which is
        // exactly why the server ships no replay and no Last-Event-ID.
        void refresh()
      }

      es.onmessage = (event) => {
        if (!state.live) return
        // Parsed only to drop frames that are not ours; NOTHING is rendered from the
        // body, and the body is never logged (`fw_version` is device-controlled, same
        // rule the server follows). An unknown `type` is ignored, per the additive
        // evolution rule in `fleetforge.events.EventType`.
        try {
          const parsed: unknown = JSON.parse(event.data)
          if (parsed === null || typeof parsed !== 'object') return
          if (typeof (parsed as { type?: unknown }).type !== 'string') return
        } catch {
          return
        }
        scheduleRefresh()
      }

      es.onerror = () => {
        if (!state.live) return
        setStream('reconnecting')
        if (es.readyState !== CLOSED) {
          // CONNECTING: a network-level failure. The browser owns this retry and will
          // honour the server's `retry: 2000`; opening a second source here leaks a slot.
          return
        }
        // CLOSED: the server answered with an HTTP status (401 or 503) and per spec
        // EventSource will never retry on its own. Probe with an ordinary request to
        // tell "logged out" from "the API pushed back".
        if (source === es) discard()
        else es.close()
        void (async () => {
          try {
            await api.listDevices()
          } catch (err) {
            if (!state.live) return
            if (err instanceof ApiError && err.isUnauthorized) {
              setStream('offline')
              state.live = false
              expiredRef.current()
              return
            }
          }
          if (!state.live) return
          reconnect = setTimeout(connect, backoff)
          backoff = Math.min(backoff * 2, RECONNECT_MAX_MS)
        })()
      }
    }

    // Rebuild the stream from scratch, used by the zombie-stream guard above.
    streamRef.current.reset = () => {
      if (!state.live) return
      if (reconnect !== null) {
        clearTimeout(reconnect)
        reconnect = null
      }
      discard()
      backoff = RECONNECT_INITIAL_MS
      connect()
    }

    // Paint from the read model immediately; do not wait for the stream to open.
    void refresh()
    connect()

    // The interval re-read that rule 2 exists for. It runs whatever the stream is doing.
    const poll = setInterval(() => void refresh(), POLL_MS)

    return () => {
      state.live = false
      clearInterval(poll)
      if (coalesce !== null) clearTimeout(coalesce)
      if (reconnect !== null) clearTimeout(reconnect)
      streamRef.current.reset = null
      // `discard()` nulls the handlers before closing — a queued `onerror` from a source
      // we are closing would otherwise schedule a reconnect after unmount — and it CLOSES
      // the source, which is the point: React 19 StrictMode mounts effects twice in dev,
      // so a source left open costs one of the API's 20 `sse_max_clients` slots per mount,
      // and ~20 reloads later the dashboard silently stops updating with a 503 nobody sees.
      discard()
    }
  }, [refresh])

  return { devices, error, stream, lastSyncedAt, now }
}
