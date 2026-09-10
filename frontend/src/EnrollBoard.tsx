// "Enroll a board" — R0-fe-1.
//
// Flow 1 of spec/flows.md, the operator half: mint a single-use enrollment token and
// copy it into the flasher's baked config (R0-fe-3). The device half is `POST
// /v1/enroll`, which burns it.
//
// The one rule that shapes this file: **the plaintext is shown exactly once.** The
// server cannot re-derive it, so if it leaves this component's state without the
// operator having copied it, the token is dead and they must issue another. It is
// therefore held in component state only — never localStorage, never a URL, never an
// error message — and the history list below it can refetch freely without disturbing
// it.

import { useCallback, useEffect, useState } from 'react'
import { ApiError, api, type EnrollmentTokenIssued, type EnrollmentTokenSummary } from './api'
import { formatWhen } from './format'

function IssuedToken({ issued, onDismiss }: { issued: EnrollmentTokenIssued; onDismiss: () => void }) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    try {
      await navigator.clipboard.writeText(issued.token)
      setCopied(true)
    } catch {
      // Clipboard needs a secure context and a permission; the token is on screen
      // either way, so a failure here is not worth an error state.
      setCopied(false)
    }
  }

  return (
    <section className="issued" aria-labelledby="issued-heading">
      <h3 id="issued-heading">Enrollment token</h3>
      <p className="warn" role="alert">
        Copy this now — it is shown once and cannot be retrieved.
      </p>
      <code data-testid="issued-token">{issued.token}</code>
      <p>
        <button type="button" onClick={() => void copy()}>
          {copied ? 'Copied' : 'Copy'}
        </button>{' '}
        <button type="button" onClick={onDismiss}>
          Done
        </button>
      </p>
      <p className="muted">Expires {formatWhen(issued.expires_at)}. Valid for one board.</p>
    </section>
  )
}

export function EnrollBoard({ onSessionExpired }: { onSessionExpired: () => void }) {
  const [tokens, setTokens] = useState<EnrollmentTokenSummary[] | null>(null)
  const [issued, setIssued] = useState<EnrollmentTokenIssued | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // A dead cookie must bounce to the login screen rather than render as a page error.
  const handle = useCallback(
    (err: unknown) => {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
        return
      }
      setError(err instanceof Error ? err.message : 'request failed')
    },
    [onSessionExpired],
  )

  const refresh = useCallback(async () => {
    try {
      setTokens((await api.listEnrollmentTokens()).tokens)
    } catch (err) {
      handle(err)
    }
  }, [handle])

  useEffect(() => {
    void refresh()
  }, [refresh])

  async function generate() {
    setBusy(true)
    setError(null)
    try {
      // Ungrouped: R0 has no group CRUD, so there is nothing to scope to yet.
      setIssued(await api.issueEnrollmentToken(null))
      await refresh()
    } catch (err) {
      handle(err)
    } finally {
      setBusy(false)
    }
  }

  async function revoke(id: string) {
    setError(null)
    try {
      await api.revokeEnrollmentToken(id)
      // If the operator revoked the token still on screen, take it off the screen.
      setIssued((current) => (current?.id === id ? null : current))
      await refresh()
    } catch (err) {
      handle(err)
    }
  }

  return (
    <section aria-labelledby="enroll-heading">
      <h2 id="enroll-heading">Enroll a board</h2>
      <p className="muted">
        Generate a token, then flash it onto the board with its Wi-Fi credentials. The board
        trades the token for its own broker credential the first time it boots.
      </p>

      <button type="button" onClick={() => void generate()} disabled={busy}>
        {busy ? 'Generating…' : 'Generate enrollment token'}
      </button>

      {error !== null && (
        <p className="bad" role="alert">
          {error}
        </p>
      )}

      {issued !== null && <IssuedToken issued={issued} onDismiss={() => setIssued(null)} />}

      <h3>Issued tokens</h3>
      {tokens === null ? (
        <p aria-busy="true">Loading…</p>
      ) : tokens.length === 0 ? (
        <p className="muted">No tokens issued yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">Status</th>
              <th scope="col">Created</th>
              <th scope="col">Expires</th>
              <th scope="col">Device</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {tokens.map((token) => (
              <tr key={token.id}>
                <td>
                  <span className={token.status === 'active' ? 'ok' : 'muted'}>{token.status}</span>
                </td>
                <td>{formatWhen(token.created_at)}</td>
                <td>{formatWhen(token.expires_at)}</td>
                <td>{token.used_by_device_id ?? '—'}</td>
                <td>
                  {token.status === 'active' && (
                    <button type="button" onClick={() => void revoke(token.id)}>
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
