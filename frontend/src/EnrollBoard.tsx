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
import {
  ApiError,
  api,
  type DeviceGroupSummary,
  type EnrollmentTokenIssued,
  type EnrollmentTokenSummary,
} from './api'
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

export type BatchMintedRow = {
  token: string
  suggested_name: string
  group_id: string
  expires_at: string
}

export function EnrollBoard({
  onSessionExpired,
  project,
  groups,
}: {
  onSessionExpired: () => void
  project?: string
  groups?: DeviceGroupSummary[]
}) {
  const [tokens, setTokens] = useState<EnrollmentTokenSummary[] | null>(null)
  const [issued, setIssued] = useState<EnrollmentTokenIssued | null>(null)
  const [selectedGroupId, setSelectedGroupId] = useState<string>('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // P2-3: Batch CLI Token & Factory Jig Generator state (in memory only, never persisted)
  const [batchCount, setBatchCount] = useState<number>(5)
  const [batchPrefix, setBatchPrefix] = useState<string>('gh-node-')
  const [batchMinted, setBatchMinted] = useState<BatchMintedRow[] | null>(null)
  const [batchCopied, setBatchCopied] = useState(false)

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
      setTokens(
        (await (project ? api.listEnrollmentTokens(project) : api.listEnrollmentTokens())).tokens,
      )
    } catch (err) {
      handle(err)
    }
  }, [handle, project])

  useEffect(() => {
    void refresh()
  }, [refresh])

  async function generate() {
    setBusy(true)
    setError(null)
    const groupId = selectedGroupId || null
    try {
      setIssued(
        await (project
          ? api.issueEnrollmentToken(groupId, project)
          : api.issueEnrollmentToken(groupId)),
      )
      await refresh()
    } catch (err) {
      handle(err)
    } finally {
      setBusy(false)
    }
  }

  async function generateBatch() {
    setBusy(true)
    setError(null)
    setBatchCopied(false)
    const groupId = selectedGroupId || null
    const count = Math.max(1, Math.min(50, Math.trunc(batchCount) || 1))
    const prefix = batchPrefix.trim() || 'board-'

    try {
      const mintedRows: BatchMintedRow[] = []
      for (let i = 1; i <= count; i += 1) {
        const res = await (project
          ? api.createEnrollmentToken(groupId, project)
          : api.createEnrollmentToken(groupId))
        mintedRows.push({
          token: res.token,
          suggested_name: `${prefix}${String(i).padStart(2, '0')}`,
          group_id: res.group_id ?? groupId ?? '',
          expires_at: res.expires_at,
        })
      }
      setBatchMinted(mintedRows)
      await refresh()
    } catch (err) {
      handle(err)
    } finally {
      setBusy(false)
    }
  }

  const batchCsv =
    batchMinted === null
      ? ''
      : [
          'token,suggested_name,group_id,expires_at',
          ...batchMinted.map(
            (r) => `${r.token},${r.suggested_name},${r.group_id},${r.expires_at}`,
          ),
        ].join('\n')

  const batchScript =
    batchMinted === null
      ? ''
      : [
          '#!/usr/bin/env bash',
          '# flash-batch.sh — Headless Factory Jig Runner (esptool.py)',
          'set -euo pipefail',
          'CSV_FILE="${1:-tokens.csv}"',
          'tail -n +2 "$CSV_FILE" | while IFS=, read -r TOKEN NAME GROUP_ID EXPIRES; do',
          '  echo "=== Provisioning $NAME (group: ${GROUP_ID:-ungrouped}) ==="',
          '  python -m fleetforge.cli build-ffcfg --token "$TOKEN" --out /tmp/ff_cfg.bin',
          '  esptool.py --chip auto --baud 921600 write_flash 0x12000 /tmp/ff_cfg.bin',
          '  rm -f /tmp/ff_cfg.bin',
          'done',
        ].join('\n')

  async function copyBatchCsv() {
    if (!batchCsv) return
    try {
      await navigator.clipboard.writeText(batchCsv)
      setBatchCopied(true)
    } catch {
      setBatchCopied(false)
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
    <section className="console-card" aria-labelledby="enroll-heading">
      <h2 id="enroll-heading">Enroll a board</h2>
      <p className="muted">
        Generate a token, then flash it onto the board with its Wi-Fi credentials. The board
        trades the token for its own broker credential the first time it boots.
      </p>

      {groups && groups.length > 0 && (
        <p>
          <label>
            Group{' '}
            <select
              value={selectedGroupId}
              onChange={(event) => setSelectedGroupId(event.target.value)}
              disabled={busy}
            >
              <option value="">Ungrouped</option>
              {groups.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          </label>
        </p>
      )}
      <button type="button" onClick={() => void generate()} disabled={busy}>
        {busy ? 'Generating…' : 'Generate enrollment token'}
      </button>

      {error !== null && (
        <p className="bad" role="alert">
          {error}
        </p>
      )}

      {issued !== null && <IssuedToken issued={issued} onDismiss={() => setIssued(null)} />}

      <details className="batch-jig" data-testid="batch-token-generator">
        <summary>Batch CLI Token &amp; Factory Jig Generator</summary>
        <p className="muted">
          Mint single-use tokens in bulk (1–50) for headless factory <code>esptool.py</code> benches
          and export a ready-to-run <code>tokens.csv</code> + <code>flash-batch.sh</code> jig
          script.
        </p>
        <div className="form-grid form-grid-3">
          <label>
            Batch count (1–50){' '}
            <input
              type="number"
              min={1}
              max={50}
              aria-label="Batch count"
              data-testid="batch-count-input"
              value={batchCount}
              onChange={(event) =>
                setBatchCount(Math.max(1, Math.min(50, Number(event.target.value) || 1)))
              }
              disabled={busy}
            />
          </label>
          <label>
            Name prefix{' '}
            <input
              type="text"
              aria-label="Batch name prefix"
              data-testid="batch-prefix-input"
              placeholder="e.g. gh-node-"
              value={batchPrefix}
              onChange={(event) => setBatchPrefix(event.target.value)}
              disabled={busy}
            />
          </label>
          {groups && groups.length > 0 && (
            <label>
              Cohort group{' '}
              <select
                aria-label="Batch group"
                data-testid="batch-group-select"
                value={selectedGroupId}
                onChange={(event) => setSelectedGroupId(event.target.value)}
                disabled={busy}
              >
                <option value="">Ungrouped</option>
                {groups.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.name}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
        <p>
          <button
            type="button"
            data-testid="mint-batch-tokens"
            onClick={() => void generateBatch()}
            disabled={busy}
          >
            {busy ? 'Minting batch…' : `Mint ${batchCount} batch token${batchCount === 1 ? '' : 's'}`}
          </button>
        </p>

        {batchMinted !== null && (
          <div className="issued" data-testid="batch-minted-output">
            <h4>Batch CSV (token,suggested_name,group_id,expires_at)</h4>
            <textarea
              className="log console"
              rows={Math.min(10, batchMinted.length + 2)}
              readOnly
              aria-label="Batch tokens CSV"
              data-testid="batch-tokens-csv"
              value={batchCsv}
            />
            <p>
              <button type="button" data-testid="copy-batch-csv" onClick={() => void copyBatchCsv()}>
                {batchCopied ? 'Copied CSV' : 'Copy batch CSV'}
              </button>{' '}
              <button
                type="button"
                data-testid="clear-batch-tokens"
                onClick={() => setBatchMinted(null)}
              >
                Clear batch
              </button>
            </p>
            <h4>Headless Factory Jig Helper (flash-batch.sh)</h4>
            <pre className="log" data-testid="batch-flash-script">
              {batchScript}
            </pre>
          </div>
        )}
      </details>

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
