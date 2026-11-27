import { useEffect, useState } from 'react'
import { ApiError, api, type ArtifactSummary, type DeviceGroupSummary } from './api'
import { type Artifacts } from './deploy'
import { formatWhen, safeHttpHref } from './format'
import { shortSha } from './profiles'
import { SimulationBadge } from './SimulationBadge'

export function UploadedBuilds({
  artifacts,
  projectSlug,
  onGoToFleet,
  onDeployed,
  onSessionExpired,
}: {
  artifacts: Artifacts
  projectSlug: string
  onGoToFleet: () => void
  onDeployed: () => void
  onSessionExpired: () => void
}) {
  const [deployingKey, setDeployingKey] = useState<string | null>(null)
  const [bulkStatus, setBulkStatus] = useState<string | null>(null)
  const [bulkError, setBulkError] = useState<string | null>(null)
  const [copiedSnippet, setCopiedSnippet] = useState(false)
  const [pendingAction, setPendingAction] = useState<{
    rowKey: string
    kind: 'deploy-all' | 'cancel-rollout'
    version: string
    rowGroupId: string
  } | null>(null)
  const [groups, setGroups] = useState<DeviceGroupSummary[]>([])
  const [targetGroupByRow, setTargetGroupByRow] = useState<Record<string, string>>({})
  const [overrideSimByRow, setOverrideSimByRow] = useState<Record<string, boolean>>({})

  useEffect(() => {
    let active = true
    if (typeof api.listGroups === 'function') {
      api
        .listGroups(projectSlug !== 'default' ? projectSlug : undefined)
        .then((res) => {
          if (active) {
            setGroups(res.groups ?? [])
          }
        })
        .catch(() => {
          if (active) {
            setGroups([])
          }
        })
    }
    return () => {
      active = false
    }
  }, [projectSlug])

  const rows: ArtifactSummary[] = []
  for (const list of artifacts.byTarget.values()) {
    rows.push(...list)
  }

  async function bulkDeploy(
    version: string,
    targetFilter: 'dev' | 'all',
    rowGroupId = '',
    overrideCodes: readonly string[] = [],
  ) {
    const key = `${version}:${targetFilter}:${rowGroupId}`
    setDeployingKey(key)
    setBulkStatus(null)
    setBulkError(null)
    try {
      const res = await api.deployProject(projectSlug, {
        version,
        target_filter: targetFilter,
        apply: 'auto',
        ...(rowGroupId ? { group_id: rowGroupId } : {}),
        ...(overrideCodes.length > 0 ? { override: overrideCodes } : {}),
      })
      onDeployed()
      setPendingAction(null)
      const count = res.deployed.length
      const queuedReasons = new Set(['awaiting_canary_confirm', 'queued_next_wave'])
      const skippedEntries = Object.entries(res.skipped ?? {})
      const queuedEntries = skippedEntries.filter(([, r]) => queuedReasons.has(r))
      const actualSkipped = skippedEntries.filter(([, r]) => !queuedReasons.has(r))
      const summaryParts: string[] = []
      if (queuedEntries.length > 0) {
        const countsByReason: Record<string, number> = {}
        for (const [, reason] of queuedEntries) {
          countsByReason[reason] = (countsByReason[reason] ?? 0) + 1
        }
        const reasonBreakdown = Object.entries(countsByReason)
          .map(([reason, n]) => `${n} ${reason}`)
          .join(', ')
        summaryParts.push(`${queuedEntries.length} queued for next wave: ${reasonBreakdown}`)
      }
      if (actualSkipped.length > 0) {
        const countsByReason: Record<string, number> = {}
        for (const [, reason] of actualSkipped) {
          countsByReason[reason] = (countsByReason[reason] ?? 0) + 1
        }
        const reasonBreakdown = Object.entries(countsByReason)
          .map(([reason, n]) => `${n} ${reason}`)
          .join(', ')
        summaryParts.push(`${actualSkipped.length} skipped: ${reasonBreakdown}`)
      }
      const suffix = summaryParts.length > 0 ? ` (${summaryParts.join('; ')})` : ''
      setBulkStatus(
        `Staged ${version} to ${count} ${targetFilter} board(s)${suffix}.`,
      )
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
        return
      }
      setBulkError(err instanceof Error ? err.message : 'Project deploy failed')
    } finally {
      setDeployingKey(null)
    }
  }

  async function cancelBulkDeploy(version: string, rowGroupId = '') {
    const key = `${version}:cancel:${rowGroupId}`
    setDeployingKey(key)
    setBulkStatus(null)
    setBulkError(null)
    try {
      const res = await api.cancelProjectDeploy(projectSlug, {
        version,
        target_filter: 'all',
        ...(rowGroupId ? { group_id: rowGroupId } : {}),
      })
      onDeployed()
      setPendingAction(null)
      const count = res.cancelled.length
      const skippedCount = Object.keys(res.skipped ?? {}).length
      setBulkStatus(
        `Cancelled in-flight ${version} deploy on ${count} board(s)` +
          (skippedCount > 0 ? ` (${skippedCount} skipped)` : '') +
          '.',
      )
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
        return
      }
      setBulkError(err instanceof Error ? err.message : 'Project deploy cancel failed')
    } finally {
      setDeployingKey(null)
    }
  }

  const cliSnippet = `python -m fleetforge.cli push --project ${projectSlug} --target esp32s3 --version 1.0.0 build/app.bin\n# Cloud Build / GitHub Actions step:\n# env: FLEETFORGE_TOKEN=ffa_...\npython -m fleetforge.cli push --project ${projectSlug} --target esp32s3 --version "$BUILD_VERSION" --commit "$COMMIT_SHA" build/app.bin`

  return (
    <section
      className="console-card"
      aria-labelledby="uploaded-builds-heading"
      data-testid="uploaded-builds"
    >
      <div className="section-header">
        <h2 id="uploaded-builds-heading">Uploaded builds</h2>
        {rows.length > 0 && (
          <button type="button" onClick={onGoToFleet}>
            Deploy in Fleet &gt;
          </button>
        )}
      </div>
      <p className="muted">
        Firmware images stored for this project. Every compatible board offers these versions in
        its Deploy column on the Fleet tab, or you can stage a version across all dev/project
        boards here.
      </p>
      <details data-testid="cli-snippet-drawer">
        <summary>[📋 Copy CLI / CI Snippet]</summary>
        <pre>
          <code>{cliSnippet}</code>
        </pre>
        <p>
          <button
            type="button"
            data-testid="copy-cli-snippet"
            onClick={() => {
              void navigator.clipboard?.writeText?.(cliSnippet)?.catch(() => {})
              setCopiedSnippet(true)
            }}
          >
            {copiedSnippet ? 'Copied' : 'Copy snippet'}
          </button>
        </p>
      </details>
      {bulkStatus !== null && (
        <p className="ok" data-testid="bulk-deploy-status">
          {bulkStatus}
        </p>
      )}
      {bulkError !== null && (
        <p className="bad" role="alert" data-testid="bulk-deploy-error">
          {bulkError}
        </p>
      )}
      {artifacts.error !== null && (
        <p className="bad" role="alert" data-testid="uploaded-builds-error">
          {artifacts.error}{' '}
          <button type="button" onClick={artifacts.reload}>
            Retry
          </button>
        </p>
      )}
      {!artifacts.loaded && artifacts.error === null ? (
        <p aria-busy="true">Loading…</p>
      ) : rows.length === 0 ? (
        artifacts.error === null && (
          <p className="muted">
            No firmware builds uploaded yet. Upload an ESP-IDF app <code>.bin</code> above to
            enable OTA updates.
          </p>
        )
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">Target</th>
              <th scope="col">Version</th>
              <th scope="col">Layout</th>
              <th scope="col">Size</th>
              <th scope="col">SHA-256</th>
              <th scope="col">Uploaded</th>
              <th scope="col">Fleet deploy</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((a) => {
              const rowKey = `${a.target}:${a.version}`
              const rowGroupId = targetGroupByRow[rowKey] ?? ''
              const safeBuildHref = safeHttpHref(a.build_url)
              const safeRepoHref = safeHttpHref(a.repo_url)
              return (
                <tr
                  key={rowKey}
                  data-testid="uploaded-build-row"
                  data-target={a.target}
                  data-version={a.version}
                >
                  <td>
                    <code>{a.target}</code>
                  </td>
                  <td>
                    <strong>{a.version}</strong>
                    <div className="muted" data-testid="build-provenance">
                      {a.artifact_type && a.artifact_type !== 'app' && (
                        <code data-testid="build-artifact-type">{a.artifact_type} </code>
                      )}
                      {a.sig ? (
                        <span className="ok">✓ signed </span>
                      ) : (
                        <span className="warn" data-testid="build-unsigned-badge">
                          [! unsigned]{' '}
                        </span>
                      )}
                      {a.branch && <code>{a.branch} </code>}
                      {a.tag && <code>tag:{a.tag} </code>}
                      {a.commit_sha && <code>@{a.commit_sha.slice(0, 7)} </code>}
                      {a.test_passed === true && <span className="ok">✓ tests </span>}
                      {a.test_passed === false && <span className="bad">✗ tests </span>}
                      <SimulationBadge
                        sha256={a.sha256}
                        projectSlug={projectSlug}
                        simStatus={a.sim_status}
                        simDetail={a.sim_detail}
                        onSimulated={artifacts.reload}
                        onSessionExpired={onSessionExpired}
                      />
                      {safeBuildHref !== null && (
                        <>
                          <a
                            href={safeBuildHref}
                            target="_blank"
                            rel="noreferrer"
                            data-testid="build-ci-link"
                          >
                            ↗ CI run
                          </a>{' '}
                        </>
                      )}
                      {safeRepoHref !== null && (
                        <a
                          href={safeRepoHref}
                          target="_blank"
                          rel="noreferrer"
                          data-testid="build-repo-link"
                        >
                          ↗ repo
                        </a>
                      )}
                    </div>
                  </td>
                  <td>
                    <code>{a.partition_layout ?? 'ab-4m-v1'}</code>
                  </td>
                  <td>{a.size_bytes.toLocaleString('en-US')} bytes</td>
                  <td>
                    <code title={a.sha256}>{shortSha(a.sha256)}</code>
                  </td>
                  <td>{formatWhen(a.created_at)}</td>
                  <td>
                    {groups.length > 0 && (
                      <>
                        <select
                          aria-label="Target group"
                          value={rowGroupId}
                          onChange={(e) =>
                            setTargetGroupByRow((prev) => ({
                              ...prev,
                              [rowKey]: e.target.value,
                            }))
                          }
                          disabled={deployingKey !== null}
                        >
                          <option value="">All groups</option>
                          {groups.map((g) => (
                            <option key={g.id} value={g.id}>
                              {g.name}
                            </option>
                          ))}
                        </select>{' '}
                      </>
                    )}
                    {a.sim_status === 'failed' && (
                      <label className="choice" data-testid="override-sim-failed-label">
                        <input
                          type="checkbox"
                          data-testid="override-sim-failed-checkbox"
                          checked={Boolean(overrideSimByRow[rowKey])}
                          onChange={(e) =>
                            setOverrideSimByRow((prev) => ({
                              ...prev,
                              [rowKey]: e.target.checked,
                            }))
                          }
                        />{' '}
                        Override failed sim
                      </label>
                    )}{' '}
                    <button
                      type="button"
                      disabled={
                        deployingKey !== null ||
                        (a.sim_status === 'failed' && !overrideSimByRow[rowKey])
                      }
                      onClick={() =>
                        void bulkDeploy(
                          a.version,
                          'dev',
                          rowGroupId,
                          a.sim_status === 'failed' && overrideSimByRow[rowKey]
                            ? ['sim_failed']
                            : [],
                        )
                      }
                      title="Stage this version onto all dev boards in this project"
                    >
                      Deploy dev
                    </button>{' '}
                    {pendingAction?.rowKey === rowKey ? (
                      <span data-testid="confirm-bulk-action">
                        {pendingAction.kind === 'deploy-all' ? (
                          <button
                            type="button"
                            className="bad"
                            data-testid="confirm-deploy-all-btn"
                            disabled={
                              deployingKey !== null ||
                              (a.sim_status === 'failed' && !overrideSimByRow[rowKey])
                            }
                            onClick={() =>
                              void bulkDeploy(
                                pendingAction.version,
                                'all',
                                pendingAction.rowGroupId,
                                a.sim_status === 'failed' && overrideSimByRow[rowKey]
                                  ? ['sim_failed']
                                  : [],
                              )
                            }
                          >
                            Confirm deploy all
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="bad"
                            data-testid="confirm-cancel-rollout-btn"
                            disabled={deployingKey !== null}
                            onClick={() =>
                              void cancelBulkDeploy(
                                pendingAction.version,
                                pendingAction.rowGroupId,
                              )
                            }
                          >
                            Confirm cancel rollout
                          </button>
                        )}{' '}
                        <button
                          type="button"
                          data-testid="cancel-bulk-action-btn"
                          onClick={() => setPendingAction(null)}
                        >
                          Keep
                        </button>
                      </span>
                    ) : (
                      <>
                        <button
                          type="button"
                          disabled={deployingKey !== null}
                          onClick={() =>
                            setPendingAction({
                              rowKey,
                              kind: 'deploy-all',
                              version: a.version,
                              rowGroupId,
                            })
                          }
                          title="Stage this version onto all compatible boards in this project"
                        >
                          Deploy all
                        </button>{' '}
                        <button
                          type="button"
                          disabled={deployingKey !== null}
                          onClick={() =>
                            setPendingAction({
                              rowKey,
                              kind: 'cancel-rollout',
                              version: a.version,
                              rowGroupId,
                            })
                          }
                          title="Cancel in-flight deploys of this version across the selected cohort"
                        >
                          Cancel rollout
                        </button>
                      </>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </section>
  )
}
