import { useEffect, useState, type FormEvent } from 'react'
import { ApiError, api, type AutoDeployTarget, type TrackMode } from './api'

export function ContinuousDeploymentCard({
  projectSlug,
  onDeployed,
  onSessionExpired,
}: {
  projectSlug: string
  onDeployed: () => void
  onSessionExpired: () => void
}) {
  const [repoUrl, setRepoUrl] = useState('')
  const [trackMode, setTrackMode] = useState<TrackMode>('off')
  const [trackRef, setTrackRef] = useState('')
  const [autoTarget, setAutoTarget] = useState<AutoDeployTarget>('dev')
  const [webhookUrl, setWebhookUrl] = useState('')
  const [webhookHeader, setWebhookHeader] = useState('')
  const [hasWebhookHeader, setHasWebhookHeader] = useState(false)
  const [busy, setBusy] = useState(false)
  const [statusMsg, setStatusMsg] = useState<string | null>(null)
  const [webhookWarning, setWebhookWarning] = useState<string | null>(null)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setStatusMsg(null)
    setWebhookWarning(null)
    setErrorMsg(null)
    api
      .listProjects()
      .then((res) => {
        if (cancelled) return
        const found = res.projects?.find((p) => p.slug === projectSlug)
        if (!found) return
        setRepoUrl(found.repo_url ?? '')
        setTrackMode(found.track_mode ?? 'off')
        setTrackRef(found.track_ref ?? '')
        setAutoTarget(
          found.auto_deploy_target && found.auto_deploy_target !== 'off'
            ? found.auto_deploy_target
            : 'dev',
        )
        setWebhookUrl(found.ci_webhook_url ?? '')
        setHasWebhookHeader(Boolean(found.has_ci_webhook_header))
        setWebhookHeader('')
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
  }, [projectSlug, onSessionExpired])

  async function save(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setStatusMsg(null)
    setWebhookWarning(null)
    setErrorMsg(null)
    try {
      const payload: Parameters<typeof api.updateProject>[1] = {
        repo_url: repoUrl.trim() || null,
        track_mode: trackMode,
        track_ref: trackMode === 'off' ? null : trackRef.trim() || null,
        auto_deploy_target: trackMode === 'off' ? 'off' : autoTarget,
        ci_webhook_url: webhookUrl.trim() || null,
      }
      if (webhookHeader.trim() !== '') {
        payload.ci_webhook_header = webhookHeader.trim()
      }
      const updated = await api.updateProject(projectSlug, payload)
      setHasWebhookHeader(Boolean(updated.has_ci_webhook_header))
      setWebhookHeader('')
      if (updated.ci_webhook_triggered === false && webhookUrl.trim() !== '') {
        setWebhookWarning(
          `CI webhook dispatch to ${webhookUrl.trim()} did not succeed. Check the webhook URL and Authorization header.`,
        )
      }
      const deployedCount = updated.auto_deployed_devices?.length ?? 0
      if (deployedCount > 0) {
        onDeployed()
        setStatusMsg(
          `Saved CD settings and staged matching build to ${deployedCount} board(s): ${updated.auto_deployed_devices!.join(', ')}.`,
        )
      } else if (updated.ci_webhook_triggered) {
        setStatusMsg(
          `Saved CD settings and dispatched CI build webhook for ${updated.track_mode} ${updated.track_ref ?? ''}.`,
        )
      } else {
        setStatusMsg(
          updated.track_mode === 'off'
            ? 'Continuous deployment turned off.'
            : `Tracking ${updated.track_mode} "${updated.track_ref ?? ''}" → auto-deploy to ${updated.auto_deploy_target} boards on upload.`,
        )
      }
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
        return
      }
      setErrorMsg(err instanceof Error ? err.message : 'Could not update CD settings')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section
      className="console-card"
      aria-labelledby="cd-settings-heading"
      data-testid="continuous-deployment"
    >
      <h2 id="cd-settings-heading">Continuous deployment (Git / CI)</h2>
      <p className="muted">
        Track a Git branch, tag pattern (e.g. <code>v*</code>), or commit for{' '}
        <code>{projectSlug}</code>. When CI uploads a passing build matching this rule (or when you
        switch the tracked ref to an already-uploaded build), Fleetforge automatically stages it to
        your <code>dev</code> boards or the entire project fleet.
      </p>
      <form className="compact-form" onSubmit={(e) => void save(e)}>
        <div className="form-grid form-grid-3">
          <label>
            Track mode{' '}
            <select
              aria-label="Track mode"
              value={trackMode}
              onChange={(e) => setTrackMode(e.target.value as TrackMode)}
              disabled={busy}
            >
              <option value="off">off (manual deploy)</option>
              <option value="branch">branch (e.g. main)</option>
              <option value="tag">tag glob (e.g. v*)</option>
              <option value="commit">commit SHA</option>
            </select>
          </label>
          <label>
            Tracked ref{' '}
            <input
              type="text"
              aria-label="Tracked ref"
              placeholder={
                trackMode === 'tag'
                  ? 'v*'
                  : trackMode === 'commit'
                    ? 'a1b2c3d'
                    : 'main'
              }
              value={trackRef}
              onChange={(e) => setTrackRef(e.target.value)}
              disabled={busy || trackMode === 'off'}
            />
          </label>
          <label>
            Auto-deploy target{' '}
            <select
              aria-label="Auto-deploy target"
              value={autoTarget}
              onChange={(e) => setAutoTarget(e.target.value as AutoDeployTarget)}
              disabled={busy || trackMode === 'off'}
            >
              <option value="dev">dev boards only</option>
              <option value="all">all project boards</option>
              <option value="off">off</option>
            </select>
          </label>
        </div>
        <div className="form-grid form-grid-3">
          <label>
            Git repository URL (optional){' '}
            <input
              type="text"
              aria-label="Git repository URL"
              placeholder="https://github.com/org/eink-firmware"
              value={repoUrl}
              onChange={(e) => setRepoUrl(e.target.value)}
              disabled={busy}
            />
          </label>
          <label>
            CI build webhook URL (optional){' '}
            <input
              type="text"
              aria-label="CI build webhook URL"
              placeholder="https://api.github.com/repos/org/eink-firmware/dispatches"
              value={webhookUrl}
              onChange={(e) => setWebhookUrl(e.target.value)}
              disabled={busy}
            />
          </label>
          <label>
            CI webhook Authorization header (optional){' '}
            <input
              type="password"
              aria-label="CI webhook Authorization header"
              placeholder={
                hasWebhookHeader ? '(configured — leave blank to keep)' : 'Bearer ghp_...'
              }
              value={webhookHeader}
              onChange={(e) => setWebhookHeader(e.target.value)}
              disabled={busy}
            />
          </label>
        </div>
        <p>
          <button
            type="submit"
            disabled={busy || (trackMode !== 'off' && trackRef.trim() === '')}
          >
            {busy ? 'Saving…' : 'Save & sync tracking'}
          </button>
        </p>
      </form>
      {statusMsg !== null && (
        <p className="ok" data-testid="cd-status">
          {statusMsg}
        </p>
      )}
      {webhookWarning !== null && (
        <p className="warn" role="status" data-testid="cd-webhook-warning">
          {webhookWarning}
        </p>
      )}
      {errorMsg !== null && (
        <p className="bad" role="alert" data-testid="cd-error">
          {errorMsg}
        </p>
      )}
    </section>
  )
}
