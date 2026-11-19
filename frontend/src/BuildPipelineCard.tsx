import { useEffect, useRef, useState, type FormEvent } from 'react'
import {
  ApiError,
  api,
  type BuildJobSummary,
  type BuildTarget,
  type PartitionProfileSummary,
} from './api'
import { type EventSourceFactory } from './fleet'
import { formatWhen } from './format'
import { shortSha } from './profiles'

const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'cancelled'])

export function BuildPipelineCard({
  projectSlug,
  active,
  profiles,
  onArtifactsChanged,
  onSessionExpired,
  createEventSource,
}: {
  projectSlug: string
  active: boolean
  profiles: PartitionProfileSummary[] | null
  onArtifactsChanged: () => void
  onSessionExpired: () => void
  createEventSource?: EventSourceFactory
}) {
  const [builds, setBuilds] = useState<BuildJobSummary[]>([])
  const [repoUrl, setRepoUrl] = useState('')
  const [gitRef, setGitRef] = useState('main')
  const [target, setTarget] = useState<BuildTarget>('esp32s3')
  const [version, setVersion] = useState('')
  const [layout, setLayout] = useState('ab-4m-v1')
  const [busy, setBusy] = useState(false)
  const [statusMsg, setStatusMsg] = useState<string | null>(null)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const [selectedBuildId, setSelectedBuildId] = useState<string | null>(null)
  const [logText, setLogText] = useState('')
  const [streaming, setStreaming] = useState(false)
  const streamRef = useRef<{ close: () => void } | null>(null)

  const layoutOptions = Array.from(
    new Set([
      'ab-4m-v1',
      ...(profiles ?? [])
        .filter((p) => p.deployable && p.layout_id)
        .map((p) => p.layout_id as string),
    ]),
  )

  async function refreshBuilds() {
    try {
      const res = await api.listBuilds(projectSlug)
      setBuilds(res.builds ?? [])
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
      .listBuilds(projectSlug)
      .then((res) => {
        if (!cancelled) {
          setBuilds(res.builds ?? [])
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

  useEffect(() => {
    return () => {
      streamRef.current?.close()
      streamRef.current = null
    }
  }, [])

  function openLogStream(buildId: string) {
    streamRef.current?.close()
    streamRef.current = null
    setSelectedBuildId(buildId)
    setLogText('')
    const url = `/v1/projects/${encodeURIComponent(projectSlug)}/builds/${encodeURIComponent(buildId)}/logs/stream`
    const factory =
      createEventSource ??
      (typeof EventSource !== 'undefined'
        ? (u: string) => new EventSource(u, { withCredentials: true })
        : null)

    if (!factory) {
      void api
        .getBuild(projectSlug, buildId)
        .then((detail) => {
          setLogText(detail.log_text ?? '')
        })
        .catch(() => {})
      return
    }

    setStreaming(true)
    const es = factory(url)
    streamRef.current = es
    es.onmessage = (ev: MessageEvent) => {
      try {
        const frame = JSON.parse(String(ev.data)) as {
          status?: BuildJobSummary['status']
          chunk?: string
          done?: boolean
          artifact_sha256?: string | null
          error_detail?: string | null
        }
        if (frame.chunk) {
          setLogText((prev) => prev + frame.chunk!)
        }
        if (frame.status) {
          setBuilds((prev) =>
            prev.map((b) =>
              b.id === buildId
                ? {
                    ...b,
                    status: frame.status!,
                    artifact_sha256: frame.artifact_sha256 ?? b.artifact_sha256,
                    error_detail: frame.error_detail ?? b.error_detail,
                  }
                : b,
            ),
          )
        }
        if (frame.done) {
          setStreaming(false)
          es.close()
          streamRef.current = null
          if (frame.status === 'succeeded') {
            onArtifactsChanged()
          }
          void refreshBuilds()
        }
      } catch {
        /* ignore malformed SSE frame */
      }
    }
    es.onerror = () => {
      setStreaming(false)
      es.close()
      streamRef.current = null
      void api
        .getBuild(projectSlug, buildId)
        .then((detail) => {
          setLogText(detail.log_text ?? '')
        })
        .catch(() => {})
    }
  }

  async function submitBuild(e: FormEvent) {
    e.preventDefault()
    if (gitRef.trim() === '' || version.trim() === '') return
    setBusy(true)
    setStatusMsg(null)
    setErrorMsg(null)
    try {
      const created = await api.createBuild(projectSlug, {
        ...(repoUrl.trim() !== '' ? { repo_url: repoUrl.trim() } : {}),
        git_ref: gitRef.trim(),
        target,
        version: version.trim(),
        partition_layout: layout,
      })
      setBuilds((prev) => [created, ...prev.filter((b) => b.id !== created.id)])
      setStatusMsg(
        `Queued ${created.target} build ${created.version} @ ${created.git_ref} (${created.status}).`,
      )
      setVersion('')
      openLogStream(created.id)
      if (created.status === 'succeeded') {
        onArtifactsChanged()
      }
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
        return
      }
      setErrorMsg(err instanceof Error ? err.message : 'Failed to queue container build')
    } finally {
      setBusy(false)
    }
  }

  async function handleCancel(buildId: string) {
    setBusy(true)
    setErrorMsg(null)
    try {
      const updated = await api.cancelBuild(projectSlug, buildId)
      setBuilds((prev) => prev.map((b) => (b.id === buildId ? updated : b)))
      setStatusMsg(`Cancelled build ${updated.version}.`)
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
        return
      }
      setErrorMsg(err instanceof Error ? err.message : 'Cancel build failed')
    } finally {
      setBusy(false)
    }
  }

  async function handleRetry(buildId: string) {
    setBusy(true)
    setErrorMsg(null)
    try {
      const retried = await api.retryBuild(projectSlug, buildId)
      setBuilds((prev) => [retried, ...prev])
      setStatusMsg(`Re-queued build ${retried.version} @ ${retried.git_ref}.`)
      openLogStream(retried.id)
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
        return
      }
      setErrorMsg(err instanceof Error ? err.message : 'Retry build failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section
      className="console-card"
      aria-labelledby="build-pipeline-heading"
      data-testid="build-pipeline-card"
    >
      <div className="section-header">
        <h2 id="build-pipeline-heading">Server-side build pipeline (ESP-IDF container)</h2>
      </div>
      <p className="muted">
        Clone a Git ref and compile firmware inside a pinned, networkless single-tenant ESP-IDF
        container for <code>{projectSlug}</code>. Cached builds reuse verified digests immediately.
      </p>
      <form className="compact-form" onSubmit={(e) => void submitBuild(e)}>
        <div className="form-grid form-grid-3">
          <label>
            Repository URL (optional override){' '}
            <input
              type="text"
              aria-label="Build repository URL"
              placeholder="https://github.com/org/firmware"
              value={repoUrl}
              onChange={(e) => setRepoUrl(e.target.value)}
              disabled={busy}
            />
          </label>
          <label>
            Git ref (branch / tag / SHA){' '}
            <input
              type="text"
              aria-label="Build git ref"
              placeholder="v1.2.0 or main"
              value={gitRef}
              onChange={(e) => setGitRef(e.target.value)}
              disabled={busy}
            />
          </label>
          <label>
            Version label{' '}
            <input
              type="text"
              aria-label="Build version"
              placeholder="1.2.0"
              value={version}
              onChange={(e) => setVersion(e.target.value)}
              disabled={busy}
            />
          </label>
        </div>
        <div className="form-grid form-grid-3">
          <label>
            Target chip{' '}
            <select
              aria-label="Build target"
              value={target}
              onChange={(e) => setTarget(e.target.value as BuildTarget)}
              disabled={busy}
            >
              <option value="esp32s3">esp32s3</option>
              <option value="esp32">esp32</option>
              <option value="esp32c3">esp32c3</option>
              <option value="esp32c6">esp32c6</option>
            </select>
          </label>
          <label>
            Partition layout{' '}
            <select
              aria-label="Build partition layout"
              value={layout}
              onChange={(e) => setLayout(e.target.value)}
              disabled={busy}
            >
              {layoutOptions.map((opt) => (
                <option key={opt} value={opt}>
                  {opt}
                </option>
              ))}
            </select>
          </label>
          <div style={{ display: 'flex', alignItems: 'flex-end' }}>
            <button
              type="submit"
              data-testid="queue-build-btn"
              disabled={busy || gitRef.trim() === '' || version.trim() === ''}
            >
              {busy ? 'Queueing…' : 'Queue container build'}
            </button>
          </div>
        </div>
      </form>
      {statusMsg !== null && (
        <p className="ok" data-testid="build-pipeline-status">
          {statusMsg}
        </p>
      )}
      {errorMsg !== null && (
        <p className="bad" role="alert" data-testid="build-pipeline-error">
          {errorMsg}
        </p>
      )}
      {builds.length > 0 && (
        <table data-testid="build-jobs-table">
          <thead>
            <tr>
              <th scope="col">Version</th>
              <th scope="col">Target</th>
              <th scope="col">Ref</th>
              <th scope="col">Status</th>
              <th scope="col">Artifact</th>
              <th scope="col">Queued</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {builds.map((b) => (
              <tr key={b.id} data-testid="build-job-row" data-status={b.status}>
                <td>
                  <strong>{b.version}</strong>
                  {b.cache_hit && (
                    <span className="ok" data-testid="build-cache-hit-badge">
                      {' '}
                      ⚡ cache hit
                    </span>
                  )}
                </td>
                <td>
                  <code>{b.target}</code>
                </td>
                <td>
                  <code>{b.git_ref}</code>
                  {b.commit_sha && <code> @{b.commit_sha.slice(0, 7)}</code>}
                </td>
                <td>
                  <span
                    className={
                      b.status === 'succeeded'
                        ? 'ok'
                        : b.status === 'failed'
                          ? 'bad'
                          : b.status === 'cancelled'
                            ? 'warn'
                            : 'muted'
                    }
                    data-testid="build-job-status"
                  >
                    {b.status}
                  </span>
                  {b.error_detail && <div className="bad">{b.error_detail}</div>}
                </td>
                <td>
                  {b.artifact_sha256 ? (
                    <code title={b.artifact_sha256}>{shortSha(b.artifact_sha256)}</code>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td>{formatWhen(b.created_at)}</td>
                <td>
                  <button
                    type="button"
                    data-testid="view-build-logs-btn"
                    onClick={() => openLogStream(b.id)}
                  >
                    Logs
                  </button>{' '}
                  {!TERMINAL_STATUSES.has(b.status) && (
                    <button
                      type="button"
                      data-testid="cancel-build-btn"
                      disabled={busy}
                      onClick={() => void handleCancel(b.id)}
                    >
                      Cancel
                    </button>
                  )}
                  {(b.status === 'failed' || b.status === 'cancelled') && (
                    <button
                      type="button"
                      data-testid="retry-build-btn"
                      disabled={busy}
                      onClick={() => void handleRetry(b.id)}
                    >
                      Retry
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {selectedBuildId !== null && (
        <div data-testid="build-log-viewer">
          <h3>
            Build log ({selectedBuildId.slice(0, 8)}){streaming ? ' · streaming…' : ''}
          </h3>
          <pre data-testid="build-log-pre">{logText || '(waiting for output…)'}</pre>
        </div>
      )}
    </section>
  )
}
