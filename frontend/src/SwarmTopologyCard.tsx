import { useEffect, useState, type FormEvent } from 'react'
import {
  ApiError,
  api,
  type ArtifactSummary,
  type SwarmTopologyResponse,
} from './api'
import { type Artifacts } from './deploy'
import { shortSha } from './profiles'

export function SwarmTopologyCard({
  projectSlug,
  active,
  artifacts,
  onSessionExpired,
}: {
  projectSlug: string
  active: boolean
  artifacts: Artifacts
  onSessionExpired: () => void
}) {
  const [topology, setTopology] = useState<SwarmTopologyResponse | null>(null)
  const [fromSha, setFromSha] = useState('')
  const [toSha, setToSha] = useState('')
  const [busy, setBusy] = useState(false)
  const [statusMsg, setStatusMsg] = useState<string | null>(null)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)

  const artifactRows: ArtifactSummary[] = []
  for (const list of artifacts.byTarget.values()) {
    for (const item of list) {
      if ((item.artifact_type ?? 'app') === 'app') {
        artifactRows.push(item)
      }
    }
  }

  async function refreshTopology() {
    try {
      const res = await api.getSwarmTopology(projectSlug)
      setTopology(res)
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
      }
    }
  }

  useEffect(() => {
    if (!active) return
    let cancelled = false
    api
      .getSwarmTopology(projectSlug)
      .then((res) => {
        if (!cancelled) {
          setTopology(res)
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof ApiError && err.isUnauthorized) {
          onSessionExpired()
        }
      })
    return () => {
      cancelled = true
    }
  }, [active, projectSlug, onSessionExpired])

  async function handleGenerateDelta(e: FormEvent) {
    e.preventDefault()
    if (!fromSha || !toSha || fromSha === toSha) return
    setBusy(true)
    setStatusMsg(null)
    setErrorMsg(null)
    try {
      const created = await api.createArtifactDelta(
        toSha,
        fromSha,
        projectSlug !== 'default' ? projectSlug : undefined,
      )
      setStatusMsg(
        `Generated ${created.format} patch (${created.patch_size_bytes.toLocaleString('en-US')} B vs ` +
          `${created.target_size_bytes.toLocaleString('en-US')} B, ${created.savings_pct}% airtime saved).`,
      )
      await refreshTopology()
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
        return
      }
      setErrorMsg(err instanceof Error ? err.message : 'Delta generation failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section
      className="console-card"
      aria-labelledby="swarm-topology-heading"
      data-testid="swarm-topology-card"
    >
      <div className="section-header">
        <h2 id="swarm-topology-heading">Swarm edge-relay hierarchy &amp; binary delta OTA (V3)</h2>
      </div>
      <p className="muted">
        Parent UGV edge-relay gateways cache staged artifacts once over uplink and serve attached{' '}
        <code>parent_device_id</code> child drones locally. Precompute <code>ffdelta-v1</code>{' '}
        binary patches for constrained links (<code>lora</code>, <code>ble</code>,{' '}
        <code>thread</code>).
      </p>

      {artifactRows.length >= 2 && (
        <form className="compact-form" onSubmit={(e) => void handleGenerateDelta(e)}>
          <div className="form-grid form-grid-3">
            <label>
              Base image (active slot){' '}
              <select
                aria-label="Delta base artifact"
                value={fromSha}
                onChange={(e) => setFromSha(e.target.value)}
                disabled={busy}
              >
                <option value="">Select base version…</option>
                {artifactRows.map((a) => (
                  <option key={`from-${a.target}-${a.version}`} value={a.sha256}>
                    {a.target} · {a.version} ({shortSha(a.sha256)})
                  </option>
                ))}
              </select>
            </label>
            <label>
              Target image (new version){' '}
              <select
                aria-label="Delta target artifact"
                value={toSha}
                onChange={(e) => setToSha(e.target.value)}
                disabled={busy}
              >
                <option value="">Select target version…</option>
                {artifactRows.map((a) => (
                  <option key={`to-${a.target}-${a.version}`} value={a.sha256}>
                    {a.target} · {a.version} ({shortSha(a.sha256)})
                  </option>
                ))}
              </select>
            </label>
            <div style={{ display: 'flex', alignItems: 'flex-end' }}>
              <button
                type="submit"
                data-testid="generate-delta-btn"
                disabled={busy || !fromSha || !toSha || fromSha === toSha}
              >
                {busy ? 'Computing…' : 'Compute binary delta'}
              </button>
            </div>
          </div>
        </form>
      )}

      {statusMsg !== null && (
        <p className="ok" data-testid="swarm-delta-status">
          {statusMsg}
        </p>
      )}
      {errorMsg !== null && (
        <p className="bad" role="alert" data-testid="swarm-delta-error">
          {errorMsg}
        </p>
      )}

      {(topology?.gateways ?? []).length > 0 && (
        <table data-testid="swarm-gateways-table">
          <thead>
            <tr>
              <th scope="col">UGV Gateway</th>
              <th scope="col">Edge Relay Origin</th>
              <th scope="col">Cached Blobs</th>
              <th scope="col">Child Nodes</th>
            </tr>
          </thead>
          <tbody>
            {(topology?.gateways ?? []).map((gw) => (
              <tr key={gw.gateway_id} data-testid="swarm-gateway-row">
                <td>
                  <strong>{gw.name ?? gw.gateway_id}</strong>{' '}
                  <span className={gw.online ? 'ok' : 'muted'}>
                    ({gw.online ? 'online' : 'offline'})
                  </span>
                </td>
                <td>
                  <code>{gw.relay_base_url ?? '(unregistered)'}</code>
                </td>
                <td>{(gw.cached_artifacts ?? []).length}</td>
                <td>
                  {(gw.children ?? []).length === 0 ? (
                    <span className="muted">0 children</span>
                  ) : (
                    (gw.children ?? [])
                      .map((c) => `${c.name ?? c.device_id}${c.has_delta_capability ? ' [Δ]' : ''}`)
                      .join(', ')
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {(topology?.deltas ?? []).length > 0 && (
        <table data-testid="artifact-deltas-table">
          <thead>
            <tr>
              <th scope="col">From → To</th>
              <th scope="col">Patch size</th>
              <th scope="col">Full size</th>
              <th scope="col">Airtime saved</th>
            </tr>
          </thead>
          <tbody>
            {(topology?.deltas ?? []).map((d) => (
              <tr key={`${d.from_sha256}->${d.to_sha256}`} data-testid="artifact-delta-row">
                <td>
                  <code>{shortSha(d.from_sha256)}</code> → <code>{shortSha(d.to_sha256)}</code>
                </td>
                <td>{d.patch_size_bytes.toLocaleString('en-US')} B</td>
                <td>{d.target_size_bytes.toLocaleString('en-US')} B</td>
                <td>
                  <span className="ok">{d.savings_pct}%</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
