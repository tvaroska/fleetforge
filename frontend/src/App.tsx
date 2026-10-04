import { buildInfo } from './buildInfo'
import { Dashboard } from './Dashboard'
import { useHealth } from './health'
import { SessionGate } from './session'
import { StatusStrip } from './StatusStrip'

// The footer is only the Web Serial capability line now: R0-fe-3 needs it to explain
// itself on a non-Chromium browser. The UI/API versions moved to the status strip in
// R2b-fe-1, where a stale bundle is flagged rather than merely printed.
function Diagnostics() {
  return (
    <footer className="muted">
      Web Serial{' '}
      {'serial' in navigator ? 'available' : 'unavailable (use Chrome or Edge to flash)'}
    </footer>
  )
}

export default function App() {
  // One poll for the page, shared by the signed-out strip and the Dashboard's.
  const health = useHealth()
  return (
    <main>
      <SessionGate banner={<StatusStrip ui={buildInfo} health={health} />}>
        {(session) => <Dashboard {...session} health={health} />}
      </SessionGate>
      <Diagnostics />
    </main>
  )
}
