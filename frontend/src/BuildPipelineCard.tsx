import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import {
  api,
  type BuildJobDetail,
  type BuildJobSummary,
  type BuildTarget,
  type PartitionProfileSummary,
} from './api'
import { type EventSourceFactory } from './fleet'
import { formatWhen } from './format'
import {
  LOAD_IDLE,
  LOAD_PENDING,
  LOAD_READY,
  loadErrorMessage,
  loadFailed,
  resolveLoadError,
  type LoadState,
} from './loadState'
import { shortSha } from './profiles'

const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'cancelled'])

export const BUILD_POLL_INTERVAL_MS = 2000

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
  const [loadState, setLoadState] = useState<LoadState>(LOAD_IDLE)
  const [sessionExpired, setSessionExpired] = useState(false)
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
  const mountedRef = useRef(true)
  const projectSlugRef = useRef(projectSlug)
  projectSlugRef.current = projectSlug
  const buildsRef = useRef(builds)
  buildsRef.current = builds
  const selectedBuildIdRef = useRef(selectedBuildId)
  selectedBuildIdRef.current = selectedBuildId
  const streamingRef = useRef(streaming)
  streamingRef.current = streaming
  const onArtifactsChangedRef = useRef(onArtifactsChanged)
  onArtifactsChangedRef.current = onArtifactsChanged
  const onSessionExpiredRef = useRef(onSessionExpired)
  onSessionExpiredRef.current = onSessionExpired
  const notifiedSucceededRef = useRef<Set<string>>(new Set())
  const listSeqRef = useRef(0)

  const layoutOptions = Array.from(
    new Set([
      'ab-4m-v1',
      ...(profiles ?? [])
        .filter((p) => p.deployable && p.layout_id)
        .map((p) => p.layout_id as string),
    ]),
  )

  const notifyIfSucceeded = useCallback((buildId: string, status: BuildJobSummary['status']) => {
    if (status === 'succeeded' && !notifiedSucceededRef.current.has(buildId)) {
      notifiedSucceededRef.current.add(buildId)
      onArtifactsChangedRef.current()
    }
  }, [])

  const handleUnauthorized = useCallback(() => {
    setSessionExpired(true)
    onSessionExpiredRef.current()
  }, [])

  const refreshBuilds = useCallback(
    async (opts: { showLoading?: boolean; seedSucceeded?: boolean } = {}) => {
      const slug = projectSlugRef.current
      const seq = ++listSeqRef.current
      if (opts.showLoading) {
        setLoadState(LOAD_PENDING)
        setErrorMsg(null)
      }
      try {
        const res = await api.listBuilds(slug)
        if (!mountedRef.current || projectSlugRef.current !== slug || seq !== listSeqRef.current) {
          return
        }
        const nextBuilds = res.builds ?? []
        if (opts.seedSucceeded) {
          for (const b of nextBuilds) {
            if (b.status === 'succeeded') {
              notifiedSucceededRef.current.add(b.id)
            }
          }
        } else {
          for (const b of nextBuilds) {
            notifyIfSucceeded(b.id, b.status)
          }
        }
        setBuilds(nextBuilds)
        setLoadState(LOAD_READY)
      } catch (err) {
        if (!mountedRef.current || projectSlugRef.current !== slug || seq !== listSeqRef.current) {
          return
        }
        const msg = resolveLoadError(err, 'Could not load build jobs', handleUnauthorized)
        if (msg !== null) {
          setLoadState(loadFailed(msg))
        }
      }
    },
    [handleUnauthorized, notifyIfSucceeded],
  )

  const syncSelectedBuild = useCallback(
    async (buildId: string): Promise<BuildJobDetail | null> => {
      const slug = projectSlugRef.current
      try {
        const detail = await api.getBuild(slug, buildId)
        if (!mountedRef.current || projectSlugRef.current !== slug) return null
        if (selectedBuildIdRef.current === buildId) {
          setLogText(detail.log_text ?? '')
        }
        const { log_text: _logText, ...summary } = detail
        void _logText
        setBuilds((prev) => {
          const exists = prev.some((b) => b.id === buildId)
          return exists
            ? prev.map((b) => (b.id === buildId ? { ...b, ...summary } : b))
            : [summary, ...prev]
        })
        notifyIfSucceeded(buildId, detail.status)
        return detail
      } catch (err) {
        if (!mountedRef.current || projectSlugRef.current !== slug) return null
        const msg = resolveLoadError(err, 'Could not refresh build details', handleUnauthorized)
        if (msg !== null) {
          setErrorMsg(msg)
        }
        return null
      }
    },
    [handleUnauthorized, notifyIfSucceeded],
  )

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      streamRef.current?.close()
      streamRef.current = null
    }
  }, [])

  useEffect(() => {
    streamRef.current?.close()
    streamRef.current = null
    setStreaming(false)
    streamingRef.current = false
    setBuilds([])
    setSelectedBuildId(null)
    selectedBuildIdRef.current = null
    setLogText('')
    setStatusMsg(null)
    setErrorMsg(null)
    setSessionExpired(false)
    notifiedSucceededRef.current.clear()
    setLoadState(LOAD_IDLE)
  }, [projectSlug])

  useEffect(() => {
    if (!active) {
      streamRef.current?.close()
      streamRef.current = null
      setStreaming(false)
      streamingRef.current = false
      return
    }
    setSessionExpired(false)
    void refreshBuilds({ showLoading: true, seedSucceeded: true })
  }, [active, projectSlug, refreshBuilds])

  const hasNonTerminalBuild = builds.some((b) => !TERMINAL_STATUSES.has(b.status))
  const selectedBuild =
    selectedBuildId !== null ? (builds.find((b) => b.id === selectedBuildId) ?? null) : null
  const selectedIsNonTerminal =
    selectedBuildId !== null &&
    (selectedBuild === null || !TERMINAL_STATUSES.has(selectedBuild.status))
  const shouldPoll = active && !sessionExpired && (hasNonTerminalBuild || selectedIsNonTerminal)

  useEffect(() => {
    if (!shouldPoll) return
    let stopped = false
    let inFlight = false
    const timer = globalThis.setInterval(() => {
      if (stopped || inFlight) return
      inFlight = true
      const tasks: Array<Promise<unknown>> = []
      const currentSelected = selectedBuildIdRef.current
      const currentSelectedRow =
        currentSelected !== null
          ? (buildsRef.current.find((b) => b.id === currentSelected) ?? null)
          : null
      const needsDetail =
        currentSelected !== null &&
        !streamingRef.current &&
        (currentSelectedRow === null || !TERMINAL_STATUSES.has(currentSelectedRow.status))
      if (needsDetail) {
        tasks.push(syncSelectedBuild(currentSelected))
      }
      const hasOtherNonTerminal = buildsRef.current.some(
        (b) => (!needsDetail || b.id !== currentSelected) && !TERMINAL_STATUSES.has(b.status),
      )
      if (hasOtherNonTerminal) {
        tasks.push(refreshBuilds())
      }
      void Promise.all(tasks).finally(() => {
        inFlight = false
      })
    }, BUILD_POLL_INTERVAL_MS)
    return () => {
      stopped = true
      globalThis.clearInterval(timer)
    }
  }, [shouldPoll, projectSlug, refreshBuilds, syncSelectedBuild])

  function openLogStream(buildId: string) {
    streamRef.current?.close()
    streamRef.current = null
    setSelectedBuildId(buildId)
    selectedBuildIdRef.current = buildId
    setLogText('')
    const url = `/v1/projects/${encodeURIComponent(projectSlug)}/builds/${encodeURIComponent(buildId)}/logs/stream`
    const factory =
      createEventSource ??
      (typeof EventSource !== 'undefined'
        ? (u: string) => new EventSource(u, { withCredentials: true })
        : null)

    if (!factory) {
      setStreaming(false)
      streamingRef.current = false
      void syncSelectedBuild(buildId)
      return
    }

    setStreaming(true)
    streamingRef.current = true
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
          streamingRef.current = false
          es.close()
          streamRef.current = null
          if (frame.status === 'succeeded') {
            notifyIfSucceeded(buildId, 'succeeded')
          }
          void refreshBuilds()
        }
      } catch {
        /* ignore malformed SSE frame */
      }
    }
    es.onerror = () => {
      setStreaming(false)
      streamingRef.current = false
      es.close()
      streamRef.current = null
      void syncSelectedBuild(buildId)
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
      if (created.status === 'succeeded') {
        notifyIfSucceeded(created.id, 'succeeded')
      }
      openLogStream(created.id)
    } catch (err) {
      const msg = resolveLoadError(err, 'Failed to queue container build', handleUnauthorized)
      if (msg !== null) {
        setErrorMsg(msg)
      }
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
      const msg = resolveLoadError(err, 'Cancel build failed', handleUnauthorized)
      if (msg !== null) {
        setErrorMsg(msg)
      }
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
      const msg = resolveLoadError(err, 'Retry build failed', handleUnauthorized)
      if (msg !== null) {
        setErrorMsg(msg)
      }
    } finally {
      setBusy(false)
    }
  }

  const displayError = errorMsg ?? loadErrorMessage(loadState)

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
      {displayError !== null && (
        <p className="bad" role="alert" data-testid="build-pipeline-error">
          {displayError}
          {loadState.status === 'error' && (
            <>
              {' '}
              <button
                type="button"
                data-testid="build-pipeline-retry-btn"
                onClick={() => void refreshBuilds({ showLoading: true })}
              >
                Retry
              </button>
            </>
          )}
        </p>
      )}
      {loadState.status === 'loading' && builds.length === 0 && (
        <p className="muted" aria-busy="true" data-testid="build-pipeline-loading">
          Loading build jobs…
        </p>
      )}
      {loadState.status === 'loaded' && builds.length === 0 && (
        <p className="muted" data-testid="build-pipeline-empty">
          No container builds queued yet.
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
