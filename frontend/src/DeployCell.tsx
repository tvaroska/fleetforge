// One fleet row's Deploy cell (R1-fe-1): pick a version, send it, watch what the board
// says back.
//
// Its own file because it is the only part of the fleet table that ACTS. `FleetView.tsx`
// says in its header that it "renders and nothing else"; keeping the POST and the picker
// state here is what keeps that true.
//
// The live state is read from `device.deploy` — the server's read model, arriving on the
// same `GET /v1/devices` the whole table is built from — and NOT from anything this
// component remembers. That is why a reload shows the same thing, and why a second
// browser tab catches up on its own. The only thing held locally is the outcome of THIS
// operator's last POST, which is a fact about this browser and exists nowhere else.
//
// Two things this cell deliberately does not do:
//
// * **No progress bar.** `pct` is a transition log, not a feed: our agent publishes
//   `downloading` once and the writer keeps the first `pct` per state, so a bar would sit
//   at one number through a two-minute download and read as a hang — the precise failure
//   this task exists to avoid. Text only, and no `role="progressbar"` anywhere.
// * **No client-side timeout.** Nothing here expires `awaiting_safe_window`; see
//   `deploy.ts`.

import { useState } from 'react'
import { ApiError, api, type ArtifactSummary, type DeployAccepted, type DeviceSummary } from './api'
import { DEPLOY_BAD_STATES, DEPLOY_STATE_LABELS } from './deploy'
import { formatAgo, formatWhen } from './format'

/** What the 202 body means, in the operator's terms. Only the two surprising cases. */
function acceptedMessage(accepted: DeployAccepted): string {
  if (accepted.reused) {
    return 'already in flight — the board deduplicates and will not download twice'
  }
  if (!accepted.device_online) {
    return 'queued — the board is offline; the broker holds the command until it next connects'
  }
  return 'sent'
}

function LiveState({ device, now }: { device: DeviceSummary; now: number }) {
  const deploy = device.deploy
  if (deploy === null) return null

  // `?? state` is the whole rule: a state this dashboard has never heard of renders as
  // itself rather than as a blank cell (`deploy.ts`).
  const label = DEPLOY_STATE_LABELS[deploy.state] ?? deploy.state
  const bad = DEPLOY_BAD_STATES.has(deploy.state)
  const className = bad ? 'bad' : deploy.state === 'confirmed' ? 'ok' : undefined

  return (
    <p>
      <span className={className} data-testid="deploy-state">
        {label}
      </span>
      {deploy.artifact_version !== null && <> → {deploy.artifact_version}</>}
      {/* Text, never a bar — see the header. */}
      {deploy.pct !== null && <> {deploy.pct}%</>}{' '}
      <span className="muted" title={formatWhen(deploy.at)}>
        ({formatAgo(deploy.at, now)})
      </span>
      {deploy.detail !== null && deploy.detail !== '' && (
        <>
          <br />
          {/* Device-controlled text, sanitised at write time and escaped by React.
              Nothing here interprets it. */}
          <span className="muted">{deploy.detail}</span>
        </>
      )}
    </p>
  )
}

export type DeployCellProps = {
  device: DeviceSummary
  /** This board's chip only, newest first — `FleetView` filters by `platform_type`. */
  artifacts: ArtifactSummary[]
  artifactsLoaded: boolean
  /** The fleet's own `refresh`, so the committed `requested` row paints immediately. */
  onDeployed: () => void
  onSessionExpired: () => void
  /** The table's shared tick, so every relative age on the page agrees. */
  now: number
}

export function DeployCell({
  device,
  artifacts,
  artifactsLoaded,
  onDeployed,
  onSessionExpired,
  now,
}: DeployCellProps) {
  const [chosen, setChosen] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [accepted, setAccepted] = useState<DeployAccepted | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Default to the newest, which is index 0 — the server orders each target's group
  // `created_at DESC`. Held as "no choice yet" rather than seeded into state, so a
  // newly uploaded artifact becomes the default without an effect to resynchronise.
  const version = chosen ?? artifacts[0]?.version ?? null

  async function deploy() {
    if (version === null) return
    setBusy(true)
    setError(null)
    try {
      setAccepted(await api.deployDevice(device.device_id, version))
      // Re-read now rather than waiting up to `POLL_MS` for the next poll: the
      // `requested` row is already committed, so this is what makes the cell respond to
      // the click immediately.
      onDeployed()
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
        return
      }
      // `ApiError.message` is already the server's own `detail` (`api.ts::detailOf`),
      // and `deploys.py` writes those sentences deliberately for this banner — "this
      // device did not announce the `ota` capability…", "…is 2000000 bytes and this
      // device's OTA slot is 1966080". Render them VERBATIM; a client-side rewrite
      // would drop the numbers the operator's next action depends on.
      setError(err instanceof Error ? err.message : 'the deploy could not be sent')
    } finally {
      setBusy(false)
    }
  }

  const name = device.name ?? device.device_id

  return (
    <td data-testid="deploy-cell">
      {artifacts.length > 0 ? (
        <>
          {/* Named, because there are N identical selects on this page and neither a
              screen reader nor a test can tell them apart otherwise. */}
          <select
            aria-label={`Version for ${name}`}
            value={version ?? ''}
            onChange={(event) => setChosen(event.target.value)}
          >
            {artifacts.map((artifact) => (
              <option key={artifact.version} value={artifact.version}>
                {artifact.version}
              </option>
            ))}
          </select>{' '}
          <button type="button" onClick={() => void deploy()} disabled={busy}>
            {busy ? 'Deploying…' : 'Deploy'}
          </button>
        </>
      ) : artifactsLoaded ? (
        // Never a disabled button with no explanation: name the chip and the diagnosis
        // (S0-fe-4). The operator's next action is an upload for THIS target.
        <span className="muted">No {device.platform_type} image has been uploaded yet.</span>
      ) : (
        <span className="muted">Loading…</span>
      )}

      {error !== null && (
        <p className="bad" role="alert">
          {error}
        </p>
      )}

      {accepted !== null && error === null && (
        <p className="muted" data-testid="deploy-accepted">
          {acceptedMessage(accepted)}
        </p>
      )}

      <LiveState device={device} now={now} />
    </td>
  )
}
