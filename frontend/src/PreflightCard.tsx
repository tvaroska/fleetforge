// The pre-flight card (R2b-fe-2). Renders `preflight.ts` and decides nothing.
//
// No `aria-live`: it re-renders on every fleet poll. All device-controlled text goes
// through React's escaping. It never mints, fetches or reads a token.

import { formatAgo } from './format'
import { type Preflight } from './preflight'

const CONSEQUENCE = 'flashing issues a new token and re-enrols it; its current baseline ends.'

export function PreflightCard({ preflight, now }: { preflight: Preflight; now: number }) {
  return (
    <section
      data-testid="preflight-card"
      data-kind={preflight.kind}
      aria-labelledby="preflight-heading"
    >
      <h3 id="preflight-heading">Before you flash</h3>
      <Body preflight={preflight} now={now} />
    </section>
  )
}

function Body({ preflight, now }: { preflight: Preflight; now: number }) {
  switch (preflight.kind) {
    case 'unknown-id':
      return (
        <p>
          This board&apos;s device id cannot be predicted (no MAC was read). If it is already
          enrolled, {CONSEQUENCE}
        </p>
      )
    case 'checking':
      return (
        <>
          <p>
            {preflight.error === null
              ? 'Checking the fleet for this board…'
              : `Could not check the fleet (${preflight.error}).`}
          </p>
          <p>If this board is already enrolled, {CONSEQUENCE}</p>
        </>
      )
    case 'new':
      return (
        <>
          <p>
            <strong>New board</strong> — <code>{preflight.deviceId}</code> is not on the fleet.
            Flashing enrols it with a fresh single-use token.
          </p>
          {preflight.arrival !== null && (
            <p className="muted">
              It has reported boot progress before (last: {preflight.arrival.stage}) but never
              joined the fleet
              {preflight.arrival.stalled ? ' — stalled' : ''}.
            </p>
          )}
          {preflight.install !== null && (
            <p>
              This flash installs agent {preflight.install.agentVersion} ({preflight.install.layout}
              ).
            </p>
          )}
        </>
      )
    case 'known':
      return (
        <>
          <p>
            Already on the fleet:{' '}
            <code>{preflight.name === null ? preflight.deviceId : preflight.name}</code>
            {preflight.name !== null && (
              <code className="muted"> ({preflight.deviceId})</code>
            )} · {preflight.platform} · fw {preflight.firmware ?? '—'} ·{' '}
            <span className={preflight.online ? 'ok' : 'muted'}>
              {preflight.online ? 'online' : 'offline'}
            </span>{' '}
            (last seen {formatAgo(preflight.lastSeen, now)})
          </p>
          {preflight.updating !== null && (
            <p className="warn">
              An update is in progress on it ({preflight.updating}); flashing interrupts it.
            </p>
          )}
          {preflight.install !== null && (
            <p>
              This flash replaces fw {preflight.firmware ?? '—'} with agent{' '}
              {preflight.install.agentVersion} ({preflight.install.layout}).
            </p>
          )}
          {preflight.layoutChange !== null && (
            <p className="warn">
              Partition layout changes: {preflight.layoutChange.from} → {preflight.layoutChange.to}.
            </p>
          )}
          <p className="warn">
            Re-flashing issues a new token and re-enrols it; its current baseline ends.
          </p>
          <p className="muted">Keeping its identity across a re-flash is not available yet.</p>
        </>
      )
  }
}
