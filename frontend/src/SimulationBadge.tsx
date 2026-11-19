import { useState } from 'react'
import { ApiError, api, type SimStatus } from './api'

export function SimulationBadge({
  sha256,
  projectSlug,
  simStatus,
  simDetail,
  onSimulated,
  onSessionExpired,
}: {
  sha256: string
  projectSlug: string
  simStatus?: SimStatus | null
  simDetail?: string | null
  onSimulated: () => void
  onSessionExpired: () => void
}) {
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function runSim() {
    if (typeof api.simulateArtifact !== 'function') return
    setRunning(true)
    setError(null)
    try {
      await api.simulateArtifact(sha256, projectSlug !== 'default' ? projectSlug : undefined)
      onSimulated()
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
        return
      }
      setError(err instanceof Error ? err.message : 'Simulation failed')
    } finally {
      setRunning(false)
    }
  }

  return (
    <span data-testid="artifact-sim-status">
      {simStatus === 'passed' && (
        <span className="ok" data-testid="sim-passed-badge" title={simDetail ?? undefined}>
          ✓ sim passed{' '}
        </span>
      )}
      {simStatus === 'failed' && (
        <span className="bad" data-testid="sim-failed-badge" title={simDetail ?? undefined}>
          [! sim failed{simDetail ? `: ${simDetail}` : ''}]{' '}
        </span>
      )}
      {(simStatus === 'pending' || simStatus === 'running') && (
        <span className="muted" data-testid="sim-pending-badge">
          sim {simStatus}…{' '}
        </span>
      )}
      {simStatus === 'skipped' && (
        <span className="muted" data-testid="sim-skipped-badge" title={simDetail ?? undefined}>
          sim skipped{' '}
        </span>
      )}
      {typeof api.simulateArtifact === 'function' && (
        <button
          type="button"
          data-testid="run-sim-btn"
          disabled={running}
          onClick={() => void runSim()}
        >
          {running ? 'Simulating…' : simStatus ? 'Re-run sim' : 'Run sim'}
        </button>
      )}{' '}
      {error && (
        <span className="bad" role="alert" data-testid="sim-run-error">
          {error}{' '}
        </span>
      )}
    </span>
  )
}
