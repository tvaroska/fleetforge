import { useEffect, useState, type FormEvent } from 'react'
import { api, type DeviceGroupSummary, type DeviceSummary, type ProjectSecretSummary } from '../api'
import { formatWhen } from '../format'
import type { PendingConfirm } from './types'

export function ProjectSecretsSection({
  activeProject,
  open,
  hasOtelHeaders,
  canManage,
  isViewer,
  busy,
  pending,
  secretsRev,
  secrets,
  projectBoards,
  otelEndpoint,
  groups,
  onSetBusy,
  onSetError,
  onSetPending,
  onSetSecretsState,
  onSetOtelEndpoint,
  onRefresh,
  onHandleError,
}: {
  activeProject: string
  open: boolean
  hasOtelHeaders: boolean
  canManage: boolean
  isViewer: boolean
  busy: boolean
  pending: PendingConfirm | null
  secretsRev: number
  secrets: ProjectSecretSummary[]
  projectBoards: DeviceSummary[]
  otelEndpoint: string
  groups: DeviceGroupSummary[]
  onSetBusy: (busy: boolean) => void
  onSetError: (error: string | null) => void
  onSetPending: (pending: PendingConfirm | null) => void
  onSetSecretsState: (rev: number, items: ProjectSecretSummary[]) => void
  onSetOtelEndpoint: (endpoint: string) => void
  onRefresh: () => Promise<void>
  onHandleError: (err: unknown) => void
}) {
  const [secretKey, setSecretKey] = useState('')
  const [secretValue, setSecretValue] = useState('')
  const [otelHeaders, setOtelHeaders] = useState('')
  const [clearOtelHeaders, setClearOtelHeaders] = useState(false)
  const [otelStatus, setOtelStatus] = useState<string | null>(null)
  const [groupName, setGroupName] = useState('')
  const [groupDesc, setGroupDesc] = useState('')

  useEffect(() => {
    setOtelStatus(null)
  }, [activeProject, open])

  async function handleUpsertSecret(event: FormEvent) {
    event.preventDefault()
    onSetBusy(true)
    onSetError(null)
    try {
      const res = await api.upsertProjectSecret(activeProject, secretKey.trim(), secretValue)
      setSecretKey('')
      setSecretValue('')
      onSetSecretsState(res.secrets_rev ?? secretsRev + 1, res.secrets ?? [])
    } catch (err) {
      onHandleError(err)
    } finally {
      onSetBusy(false)
    }
  }

  async function handleDeleteSecret(key: string) {
    onSetBusy(true)
    onSetError(null)
    try {
      const res = await api.deleteProjectSecret(activeProject, key)
      onSetSecretsState(res.secrets_rev ?? secretsRev + 1, res.secrets ?? [])
    } catch (err) {
      onHandleError(err)
    } finally {
      onSetBusy(false)
      onSetPending(null)
    }
  }

  async function handleSaveOtel(event: FormEvent) {
    event.preventDefault()
    onSetBusy(true)
    onSetError(null)
    setOtelStatus(null)
    try {
      const patch: { otel_endpoint?: string | null; otel_headers?: string | null } = {
        otel_endpoint: otelEndpoint.trim() || null,
      }
      if (clearOtelHeaders) {
        patch.otel_headers = ''
      } else if (otelHeaders.trim() !== '') {
        patch.otel_headers = otelHeaders.trim()
      }
      const updated = await api.updateProject(activeProject, patch)
      setOtelHeaders('')
      setClearOtelHeaders(false)
      setOtelStatus(
        updated.otel_endpoint
          ? `Forwarding device logs & metrics to ${updated.otel_endpoint}.`
          : 'OpenTelemetry forwarding turned off.',
      )
      await onRefresh()
    } catch (err) {
      onHandleError(err)
    } finally {
      onSetBusy(false)
    }
  }

  async function handleCreateGroup(event: FormEvent) {
    event.preventDefault()
    if (!groupName.trim()) {
      return
    }
    onSetBusy(true)
    onSetError(null)
    try {
      await api.createGroup(
        { name: groupName.trim(), description: groupDesc.trim() || null },
        activeProject !== 'default' ? activeProject : undefined,
      )
      setGroupName('')
      setGroupDesc('')
      await onRefresh()
    } catch (err) {
      onHandleError(err)
    } finally {
      onSetBusy(false)
    }
  }

  async function handleDeleteGroup(groupId: string) {
    onSetError(null)
    try {
      await api.deleteGroup(groupId)
      await onRefresh()
    } catch (err) {
      onHandleError(err)
    } finally {
      onSetPending(null)
    }
  }

  const syncedBoards = projectBoards.filter((d) => (d.secrets_rev ?? 0) >= secretsRev)
  const laggingBoards = projectBoards.filter((d) => (d.secrets_rev ?? 0) < secretsRev)

  return (
    <>
      <h3>
        Device secrets (NVS <code>ff_sec</code>) ·{' '}
        <span className="badge muted" data-testid="project-secrets-rev">
          rev {secretsRev}
        </span>
      </h3>
      <p className="muted">
        Encrypted key-value secrets synced over HTTPS to boards in <code>{activeProject}</code>{' '}
        whenever <code>dn/cfg.secrets_rev</code> advances (read on the board with{' '}
        <code>Fleetforge.getSecret(key, buf, len)</code>).
      </p>
      <p className="muted" data-testid="secrets-convergence">
        <strong>
          {syncedBoards.length}/{projectBoards.length} synced to rev {secretsRev}
        </strong>
        {laggingBoards.length > 0 && (
          <span data-testid="secrets-lagging-boards">
            {' '}
            · Pending sync:{' '}
            {laggingBoards
              .map((d) => `${d.name ?? d.device_id} (rev ${d.secrets_rev ?? 0})`)
              .join(', ')}
          </span>
        )}
      </p>
      {secrets.length > 0 && (
        <table data-testid="project-secrets-table">
          <thead>
            <tr>
              <th scope="col">Key</th>
              <th scope="col">Updated</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {secrets.map((s) => (
              <tr key={s.key} data-testid="project-secret-row">
                <td>
                  <code>{s.key}</code>
                </td>
                <td>{formatWhen(s.updated_at)}</td>
                <td>
                  <button
                    type="button"
                    aria-label={`Delete secret ${s.key}`}
                    disabled={busy || isViewer}
                    onClick={() => onSetPending({ kind: 'delete-secret', key: s.key })}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {pending?.kind === 'delete-secret' && (
        <p data-testid="confirm-delete-secret">
          Delete secret <code>{pending.key}</code>? Boards will erase it on next sync.{' '}
          <button
            type="button"
            disabled={busy}
            onClick={() => void handleDeleteSecret(pending.key)}
          >
            Yes, delete {pending.key}
          </button>{' '}
          <button type="button" onClick={() => onSetPending(null)}>
            Keep
          </button>
        </p>
      )}
      <form className="compact-form" onSubmit={(e) => void handleUpsertSecret(e)}>
        <div className="form-grid">
          <label>
            Secret key
            <input
              type="text"
              aria-label="Secret key"
              value={secretKey}
              onChange={(e) => setSecretKey(e.target.value)}
              placeholder="API_KEY"
              disabled={busy || isViewer}
            />
          </label>
          <label>
            Secret value
            <input
              type="password"
              aria-label="Secret value"
              value={secretValue}
              onChange={(e) => setSecretValue(e.target.value)}
              placeholder="sk-live-..."
              disabled={busy || isViewer}
            />
          </label>
        </div>
        <p>
          <button type="submit" disabled={busy || isViewer || !secretKey.trim() || !secretValue}>
            Save / Rotate secret
          </button>
        </p>
      </form>

      <h3>OpenTelemetry export (OTLP/HTTP)</h3>
      <p className="muted">
        Forward board <code>up/log</code> lines and <code>up/telemetry</code> metrics as
        OpenTelemetry <code>LogRecord</code>s and <code>Gauge</code>s with{' '}
        <code>service.name={activeProject}</code>.
      </p>
      <form className="compact-form" onSubmit={(e) => void handleSaveOtel(e)}>
        <div className="form-grid">
          <label>
            OTLP/HTTP endpoint
            <input
              type="text"
              aria-label="OTLP/HTTP endpoint"
              value={otelEndpoint}
              onChange={(e) => onSetOtelEndpoint(e.target.value)}
              placeholder="https://otlp.example.com"
              disabled={busy || isViewer}
            />
          </label>
          <label>
            OTLP headers (optional)
            <input
              type="password"
              aria-label="OTLP headers"
              value={otelHeaders}
              onChange={(e) => setOtelHeaders(e.target.value)}
              placeholder={
                hasOtelHeaders ? '(configured — enter to replace)' : 'Authorization=Bearer ...'
              }
              disabled={busy || isViewer || clearOtelHeaders}
            />
          </label>
        </div>
        <p>
          <label className="choice">
            <input
              type="checkbox"
              aria-label="Clear saved headers"
              data-testid="clear-otel-headers"
              checked={clearOtelHeaders}
              onChange={(e) => setClearOtelHeaders(e.target.checked)}
              disabled={busy || isViewer}
            />{' '}
            Clear saved headers
          </label>
        </p>
        <p>
          <button type="submit" disabled={busy || isViewer}>
            Save OTel settings
          </button>
        </p>
      </form>
      {otelStatus !== null && (
        <p className="ok" data-testid="otel-status">
          {otelStatus}
        </p>
      )}

      <h3>Device groups (cohorts)</h3>
      <p className="muted">
        Named release rings (e.g. <code>canary</code>, <code>prod</code>) or sub-locations in{' '}
        <code>{activeProject}</code> for targeted rollouts.
      </p>
      {groups.length > 0 && (
        <table data-testid="project-groups-table">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Description</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => (
              <tr key={g.id} data-testid="project-group-row">
                <td>
                  <code>{g.name}</code>
                </td>
                <td>{g.description ?? <span className="muted">—</span>}</td>
                <td>
                  {canManage && (
                    <button
                      type="button"
                      aria-label={`Delete group ${g.name}`}
                      disabled={busy}
                      onClick={() => onSetPending({ kind: 'delete-group', id: g.id, name: g.name })}
                    >
                      Delete
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {pending?.kind === 'delete-group' && (
        <p data-testid="confirm-delete-group">
          Delete group <code>{pending.name}</code>? Boards in it become ungrouped.{' '}
          <button
            type="button"
            disabled={busy}
            onClick={() => void handleDeleteGroup(pending.id)}
          >
            Yes, delete {pending.name}
          </button>{' '}
          <button type="button" onClick={() => onSetPending(null)}>
            Keep
          </button>
        </p>
      )}
      {canManage && (
        <form className="compact-form" onSubmit={(e) => void handleCreateGroup(e)}>
          <div className="form-grid">
            <label>
              Group name
              <input
                type="text"
                aria-label="Group name"
                value={groupName}
                onChange={(e) => setGroupName(e.target.value)}
                placeholder="canary"
                disabled={busy}
                required
              />
            </label>
            <label>
              Description (optional)
              <input
                type="text"
                aria-label="Group description"
                value={groupDesc}
                onChange={(e) => setGroupDesc(e.target.value)}
                placeholder="Canary test cohort"
                disabled={busy}
              />
            </label>
          </div>
          <p>
            <button type="submit" disabled={busy || !groupName.trim()}>
              Create group
            </button>
          </p>
        </form>
      )}
    </>
  )
}
