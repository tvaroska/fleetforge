import { useEffect, useState } from 'react'
import { api, type Health } from './api'
import { EnrollBoard } from './EnrollBoard'
import { SessionGate } from './session'

// R0-infra-1's health probe survives as a footer rather than a page: it is the
// cheapest proof of the one-origin topology, and R0-fe-3 needs the Web Serial
// capability line to explain itself on a non-Chromium browser.
function Diagnostics() {
  const [health, setHealth] = useState<Health | null>(null)

  useEffect(() => {
    let live = true
    api
      .health()
      .then((value) => {
        if (live) setHealth(value)
      })
      .catch(() => {
        if (live) setHealth(null)
      })
    return () => {
      live = false
    }
  }, [])

  return (
    <footer className="muted">
      API {health === null ? 'unreachable' : `ok · ${health.version}`} · Web Serial{' '}
      {'serial' in navigator ? 'available' : 'unavailable (use Chrome or Edge to flash)'}
    </footer>
  )
}

export default function App() {
  return (
    <main>
      <SessionGate>
        {({ me, expire, signOut }) => (
          <>
            <header>
              <h1>Fleetforge</h1>
              <p className="muted">
                {me.subject}{' '}
                <button type="button" onClick={() => void signOut()}>
                  Sign out
                </button>
              </p>
            </header>
            <EnrollBoard onSessionExpired={expire} />
          </>
        )}
      </SessionGate>
      <Diagnostics />
    </main>
  )
}
