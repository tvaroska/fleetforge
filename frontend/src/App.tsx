import { useEffect, useState } from 'react'

// R0-infra-1 ships the smallest page that PROVES the one-origin topology: a
// same-origin fetch of /v1/healthz through nginx (prod) or the Vite proxy (dev).
// R0-fe-1 replaces this with the token page and the device list.

type Health = { status: string; version: string }

type Probe =
  | { state: 'loading' }
  | { state: 'ok'; health: Health }
  | { state: 'error'; detail: string }

export default function App() {
  const [probe, setProbe] = useState<Probe>({ state: 'loading' })

  useEffect(() => {
    const controller = new AbortController()
    // Relative URL on purpose: the API shares this page's origin, so there is no
    // base URL to configure and no CORS preflight to satisfy.
    fetch('/v1/healthz', { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`)
        }
        return (await response.json()) as Health
      })
      .then((health) => setProbe({ state: 'ok', health }))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setProbe({ state: 'error', detail: error instanceof Error ? error.message : 'unreachable' })
      })
    return () => controller.abort()
  }, [])

  return (
    <main>
      <h1>
        Fleetforge — API:{' '}
        {probe.state === 'ok' ? (
          <span className="ok">ok</span>
        ) : probe.state === 'error' ? (
          <span className="bad">unreachable</span>
        ) : (
          <span>checking…</span>
        )}
      </h1>
      <dl>
        <dt>API version</dt>
        <dd>{probe.state === 'ok' ? probe.health.version : '—'}</dd>
        <dt>Origin</dt>
        <dd>{window.location.origin}</dd>
        <dt>Web Serial</dt>
        {/* R0-fe-3 needs this to be true; http://localhost is a secure context. */}
        <dd>{'serial' in navigator ? 'available' : 'unavailable'}</dd>
        {probe.state === 'error' && (
          <>
            <dt>Detail</dt>
            <dd className="bad">{probe.detail}</dd>
          </>
        )}
      </dl>
    </main>
  )
}
