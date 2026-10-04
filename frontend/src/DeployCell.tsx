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
// A finished transaction (R2-fe-1) gets a verdict word, `good` or `rolled back`, instead
// of a label with an arrow and a percentage. The verdict is withheld when the board's own
// announce contradicts it (`deploy.ts::deployOutcome`).
//
// Nothing is sent without the pre-check card (R2b-fe-8). **Deploy** asks
// `POST /deploy/precheck` (a dry run with no side effects) and opens `PrecheckCard`:
// current to target, what is refused or warned, the board's own rollback window. **Send**
// is a second click and the only thing that reaches `POST /deploy`. A refusal offers no
// Send. A non-gating warning is overridden by the Send click ("Send anyway"); a gating one
// (`needs_override`, R2b-be-7) also needs its tick, and only then does the deploy body
// carry `override: [code]`. The key is absent otherwise: today's `DeployRequest` forbids
// extra keys, so even `override: []` would be a 422. Changing the version, Cancel or a
// newer Deploy click discards the card, and a pre-check answer that arrives after that is
// ignored.
//
// Two things this cell deliberately does not do:
//
// * **No progress bar.** `pct` is a transition log, not a feed: our agent publishes
//   `downloading` once and the writer keeps the first `pct` per state, so a bar would sit
//   at one number through a two-minute download and read as a hang — the precise failure
//   this task exists to avoid. Text only, and no `role="progressbar"` anywhere.
// * **No client-side timeout.** Nothing here expires `awaiting_safe_window`; see
//   `deploy.ts`.

import { useRef, useState } from 'react'
import {
  ApiError,
  api,
  type ArtifactSummary,
  type DeployAccepted,
  type DeployPrecheck,
  type DeviceSummary,
} from './api'
import { overrideFor } from './deployPrecheck'
import { PrecheckCard } from './PrecheckCard'
import { DEPLOY_BAD_STATES, DEPLOY_STATE_LABELS, deployOutcome } from './deploy'
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

  const outcome = deployOutcome(deploy, device.fw_version)

  const age = (
    <span className="muted" title={formatWhen(deploy.at)}>
      ({formatAgo(deploy.at, now)})
    </span>
  )
  const detail = deploy.detail !== null && deploy.detail !== '' && (
    <>
      <br />
      {/* Device-controlled text, sanitised at write time and escaped by React.
          Nothing here interprets it. */}
      <span className="muted">{deploy.detail}</span>
    </>
  )

  if (outcome?.kind === 'verdict') {
    return (
      <p>
        <span className={outcome.tone} data-testid="deploy-state">
          {outcome.word}
        </span>{' '}
        — {outcome.note} {age}
        {detail}
      </p>
    )
  }

  if (outcome?.kind === 'drift') {
    return (
      <p>
        <span data-testid="deploy-state">{DEPLOY_STATE_LABELS.confirmed}</span>
        {deploy.artifact_version !== null && <> → {deploy.artifact_version}</>} {age}
        <br />
        <span className="muted" data-testid="deploy-drift">
          {outcome.reported === null
            ? 'the board has not reported a version since'
            : `the board has since reported ${outcome.reported}, so this is not what it runs now`}
        </span>
        {detail}
      </p>
    )
  }

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
      {deploy.pct !== null && <> {deploy.pct}%</>} {age}
      {detail}
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
  const [phase, setPhase] = useState<'idle' | 'checking' | 'sending'>('idle')
  const [precheck, setPrecheck] = useState<DeployPrecheck | null>(null)
  const [ticked, setTicked] = useState<ReadonlySet<string>>(new Set())
  const [accepted, setAccepted] = useState<DeployAccepted | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Bumped whenever the card stops being the thing the operator is looking at (version
  // change, Cancel, a newer Deploy click). A pre-check answer that comes back under an
  // older number describes something no longer on screen and is dropped.
  const seq = useRef(0)

  // Default to the newest, which is index 0 — the server orders each target's group
  // `created_at DESC`. Held as "no choice yet" rather than seeded into state, so a
  // newly uploaded artifact becomes the default without an effect to resynchronise.
  const version = chosen ?? artifacts[0]?.version ?? null

  // `ApiError.message` is already the server's own `detail` (`api.ts::detailOf`), and
  // `deploys.py` writes those sentences deliberately for this banner — "this device did
  // not announce the `ota` capability…", "…is 2000000 bytes and this device's OTA slot is
  // 1966080". Render them VERBATIM; a client-side rewrite would drop the numbers the
  // operator's next action depends on.
  function fail(err: unknown, fallback: string) {
    if (err instanceof ApiError && err.isUnauthorized) {
      onSessionExpired()
      return
    }
    setError(err instanceof Error ? err.message : fallback)
  }

  function discard() {
    seq.current++
    setPrecheck(null)
    setTicked(new Set())
    setPhase('idle')
  }

  async function check() {
    if (version === null) return
    const mine = ++seq.current
    setPhase('checking')
    setError(null)
    setAccepted(null)
    setTicked(new Set())
    try {
      const result = await api.precheckDeploy(device.device_id, version)
      if (mine !== seq.current) return
      setPrecheck(result)
    } catch (err) {
      if (mine !== seq.current) return
      setPrecheck(null)
      fail(err, 'the deploy could not be checked')
    }
    if (mine === seq.current) setPhase('idle')
  }

  async function send() {
    if (precheck === null) return
    // The version the card describes, not the select's current value; they are equal by
    // construction (a change discards the card), but be explicit.
    setPhase('sending')
    setError(null)
    try {
      setAccepted(
        await api.deployDevice(device.device_id, precheck.version, 'auto', overrideFor(precheck, ticked)),
      )
      setPrecheck(null)
      setTicked(new Set())
      // Re-read now rather than waiting up to `POLL_MS` for the next poll: the
      // `requested` row is already committed, so this is what makes the cell respond to
      // the click immediately.
      onDeployed()
    } catch (err) {
      // The card stays open and Send comes back: the refusal is about this board and
      // the operator may Cancel and try another version.
      fail(err, 'the deploy could not be sent')
    } finally {
      setPhase('idle')
    }
  }

  function tick(code: string, on: boolean) {
    setTicked((previous) => {
      const next = new Set(previous)
      if (on) next.add(code)
      else next.delete(code)
      return next
    })
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
            onChange={(event) => {
              // The card described the old version.
              discard()
              setChosen(event.target.value)
            }}
          >
            {artifacts.map((artifact) => (
              <option key={artifact.version} value={artifact.version}>
                {artifact.version}
              </option>
            ))}
          </select>{' '}
          <button type="button" onClick={() => void check()} disabled={phase !== 'idle'}>
            {phase === 'checking' ? 'Checking…' : 'Deploy'}
          </button>
        </>
      ) : artifactsLoaded ? (
        // Never a disabled button with no explanation: name the chip and the diagnosis
        // (S0-fe-4). The operator's next action is an upload for THIS target.
        <span className="muted">
          No {device.platform_type} image has been uploaded yet. Upload one under “Upload a build”.
        </span>
      ) : (
        <span className="muted">Loading…</span>
      )}

      {precheck !== null && (
        <PrecheckCard
          precheck={precheck}
          ticked={ticked}
          onTick={tick}
          onSend={() => void send()}
          onCancel={discard}
          sending={phase === 'sending'}
        />
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
