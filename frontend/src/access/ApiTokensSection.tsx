import { useState, type FormEvent } from 'react'
import { api, type AdminTokenIssued, type AdminTokenSummary } from '../api'
import { formatWhen } from '../format'
import type { PendingConfirm } from './types'

export function ApiTokensSection({
  activeProject,
  tokens,
  busy,
  isViewer,
  pending,
  onSetBusy,
  onSetError,
  onSetPending,
  onRefresh,
  onHandleError,
}: {
  activeProject: string
  tokens: AdminTokenSummary[] | null
  busy: boolean
  isViewer: boolean
  pending: PendingConfirm | null
  onSetBusy: (busy: boolean) => void
  onSetError: (error: string | null) => void
  onSetPending: (pending: PendingConfirm | null) => void
  onRefresh: () => Promise<void>
  onHandleError: (err: unknown) => void
}) {
  const [issuedToken, setIssuedToken] = useState<AdminTokenIssued | null>(null)
  const [copiedToken, setCopiedToken] = useState(false)
  const [tokenName, setTokenName] = useState('')
  const [tokenScope, setTokenScope] = useState('deploy')
  const [tokenPinned, setTokenPinned] = useState(true)
  const [tokenTtlDays, setTokenTtlDays] = useState('')
  const [tokenEnrollScope, setTokenEnrollScope] = useState(false)

  async function handleIssueToken(event: FormEvent) {
    event.preventDefault()
    onSetBusy(true)
    onSetError(null)
    try {
      const baseScopes =
        tokenScope === 'admin'
          ? ['admin']
          : tokenScope === 'read'
            ? ['read']
            : ['read', 'write:artifacts', 'deploy']
      const scopes =
        tokenEnrollScope && !baseScopes.includes('enroll')
          ? [...baseScopes, 'enroll']
          : baseScopes
      const payload: Parameters<typeof api.issueAdminToken>[0] = {
        name: tokenName.trim(),
        project: tokenPinned ? activeProject : null,
        scopes,
      }
      if (tokenTtlDays !== '') {
        payload.expires_in_days = Number(tokenTtlDays)
      }
      const issued = await api.issueAdminToken(payload)
      setTokenName('')
      setTokenTtlDays('')
      setTokenEnrollScope(false)
      setCopiedToken(false)
      setIssuedToken(issued)
      await onRefresh()
    } catch (err) {
      onHandleError(err)
    } finally {
      onSetBusy(false)
    }
  }

  async function handleRevokeToken(id: string) {
    onSetError(null)
    try {
      await api.revokeAdminToken(id)
      setIssuedToken((curr) => (curr?.id === id ? null : curr))
      await onRefresh()
    } catch (err) {
      onHandleError(err)
    } finally {
      onSetPending(null)
    }
  }

  return (
    <>
      <h3>API tokens (CLI / CI)</h3>
      <form className="compact-form" onSubmit={(e) => void handleIssueToken(e)}>
        <div className="form-grid form-grid-3">
          <label>
            Token label
            <input
              type="text"
              aria-label="Token label"
              value={tokenName}
              onChange={(e) => setTokenName(e.target.value)}
              placeholder="github-actions"
              disabled={busy || isViewer}
              required
            />
          </label>
          <label>
            Preset
            <select
              aria-label="Token preset"
              value={tokenScope}
              onChange={(e) => setTokenScope(e.target.value)}
              disabled={busy || isViewer}
            >
              <option value="deploy">CI deploy (read + write:artifacts + deploy)</option>
              <option value="read">Read-only (read)</option>
              <option value="admin">Full access (admin)</option>
            </select>
          </label>
          <label>
            TTL (days)
            <select
              aria-label="TTL (days)"
              data-testid="token-ttl-select"
              value={tokenTtlDays}
              onChange={(e) => setTokenTtlDays(e.target.value)}
              disabled={busy || isViewer}
            >
              <option value="">Never</option>
              <option value="7">7d</option>
              <option value="30">30d</option>
              <option value="90">90d</option>
              <option value="365">365d</option>
            </select>
          </label>
        </div>
        <p>
          <label className="choice">
            <input
              type="checkbox"
              checked={tokenPinned}
              onChange={(e) => setTokenPinned(e.target.checked)}
              disabled={busy || isViewer}
            />{' '}
            Pin to <code>{activeProject}</code>
          </label>{' '}
          <label className="choice">
            <input
              type="checkbox"
              aria-label="Include enroll scope"
              data-testid="token-enroll-scope"
              checked={tokenEnrollScope}
              onChange={(e) => setTokenEnrollScope(e.target.checked)}
              disabled={busy || isViewer}
            />{' '}
            Include <code>enroll</code> scope
          </label>
        </p>
        <p>
          <button type="submit" disabled={busy || isViewer || !tokenName.trim()}>
            Issue API token
          </button>
        </p>
      </form>

      {issuedToken !== null && (
        <div className="issued" role="alert">
          <p className="warn">Copy this API token now — it is shown once:</p>
          <code data-testid="issued-admin-token">{issuedToken.token}</code>{' '}
          <button
            type="button"
            data-testid="copy-issued-token"
            onClick={() => {
              void navigator.clipboard?.writeText?.(issuedToken.token)?.catch(() => {})
              setCopiedToken(true)
            }}
          >
            {copiedToken ? 'Copied' : 'Copy token'}
          </button>{' '}
          <button
            type="button"
            onClick={() => {
              setIssuedToken(null)
              setCopiedToken(false)
            }}
          >
            Done
          </button>
        </div>
      )}

      {tokens !== null && tokens.length > 0 && (
        <table>
          <thead>
            <tr>
              <th scope="col">Label</th>
              <th scope="col">Project</th>
              <th scope="col">Scopes</th>
              <th scope="col">Created</th>
              <th scope="col">Lifecycle</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {tokens.map((t) => (
              <tr key={t.id} data-testid="admin-token-row">
                <td>{t.name}</td>
                <td>{t.project_slug ?? 'all'}</td>
                <td>
                  <code>{t.scopes.join(', ')}</code>
                </td>
                <td>{formatWhen(t.created_at)}</td>
                <td>
                  {t.last_used_at ? (
                    <span data-testid="token-last-used">used {formatWhen(t.last_used_at)}</span>
                  ) : (
                    <span className="muted">unused</span>
                  )}
                  {t.expires_at ? (
                    <>
                      {' · '}
                      <span data-testid="token-expires-at">exp {formatWhen(t.expires_at)}</span>
                    </>
                  ) : null}
                </td>
                <td>
                  {t.revoked_at === null ? (
                    <button
                      type="button"
                      aria-label={`Revoke ${t.name}`}
                      disabled={busy || isViewer}
                      onClick={() => onSetPending({ kind: 'revoke-token', id: t.id, name: t.name })}
                    >
                      Revoke
                    </button>
                  ) : (
                    <span className="muted">revoked</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {pending?.kind === 'revoke-token' && (
        <p data-testid="confirm-revoke-token">
          Revoke {pending.name}? Anything using it stops working at once.{' '}
          <button
            type="button"
            disabled={busy}
            onClick={() => void handleRevokeToken(pending.id)}
          >
            Revoke token
          </button>{' '}
          <button type="button" onClick={() => onSetPending(null)}>
            Keep
          </button>
        </p>
      )}
    </>
  )
}
