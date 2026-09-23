import { useEffect, useState } from 'react'
import { api, type Health } from './api'
import { UNKNOWN, buildInfo, describeBuild } from './buildInfo'
import { EnrollBoard } from './EnrollBoard'
import { FlashBoard } from './FlashBoard'
import { FleetView } from './FleetView'
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

  // Both halves, always, and each labelled. A single "version" on a page cannot say
  // whether a stale cached bundle is talking to a fresh API, which is the state that
  // makes a deploy look like it did nothing.
  const ui = describeBuild(buildInfo.version, buildInfo.commit)
  const server =
    health === null
      ? 'unreachable'
      : describeBuild(health.version, health.commit ?? UNKNOWN)

  return (
    <footer className="muted">
      <span title={`built ${buildInfo.builtAt}`}>UI {ui}</span> ·{' '}
      <span title={health?.built_at ? `built ${health.built_at}` : undefined}>
        API {server}
      </span>{' '}
      · Web Serial{' '}
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
            {/* The fleet first: R0's done-when is "watch it come online", and after the
                first board this is what the page is opened for. */}
            <FleetView onSessionExpired={expire} />
            {/* Flashing mints its own token, so it sits above the manual token screen:
                the common path is "plug a board in", not "copy a string somewhere". */}
            <FlashBoard onSessionExpired={expire} />
            <EnrollBoard onSessionExpired={expire} />
          </>
        )}
      </SessionGate>
      <Diagnostics />
    </main>
  )
}
