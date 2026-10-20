// The signed-in page body (R2b-fe-1). It exists to own the ONE `useFleet` of the page:
// each instance opens an EventSource and costs one of the API's `sse_max_clients` slots,
// so the strip and the table share this hook rather than opening two. Never render
// `<FleetView>` here for that reason.
//
// Which board the strip shows: the last one picked in the table, deployed to, or
// detected/flashed in the flasher; with nothing picked and exactly one board, that one.
// The flasher also reads the shared fleet (pre-flight card, R2b-fe-2) and the versions
// (result card, R2b-fe-3), and the table's update result cards (R2b-fe-10) read the same
// versions — all from the props this page already has, with no new hooks.
// The upload form (R2b-fe-7) shares the one artifact list: it calls `artifacts.reload`.
// The profiles section (R3-fe-1) owns one profile read, re-read on the fleet's announce
// hint (`fleet.announceSeq`), and shares that list with the upload form, whose layout
// options are the adopted profile names.
// The device log (R4-fe-2) follows the strip's board and is re-read when THAT board's
// `last_seen` changes (an `up/log` batch moves it): no new stream, no frame body read.

import { useState, type FormEvent } from 'react'
import {
  ApiError,
  api,
  type ArtifactSummary,
  type AutoDeployTarget,
  type Me,
  type TrackMode,
} from './api'
import { buildInfo, type BuildInfo } from './buildInfo'
import { useArtifacts, type Artifacts } from './deploy'
import { DeviceLog } from './DeviceLog'
import { EnrollBoard } from './EnrollBoard'
import { type FlasherFactory } from './flasher'
import { FlashBoard } from './FlashBoard'
import { FleetTable } from './FleetView'
import { useFleet, type EventSourceFactory } from './fleet'
import { formatWhen } from './format'
import { type HealthState } from './health'
import { PartitionProfiles } from './PartitionProfiles'
import { ProjectAccess } from './ProjectAccess'
import { pendingProfiles, shortSha, useProfiles } from './profiles'
import { StatusStrip } from './StatusStrip'
import { describeBoard, describeVersions } from './statusStrip'
import { UploadBuild } from './UploadBuild'

export type DashboardTab = 'fleet' | 'builds' | 'onboard' | 'access'

function ContinuousDeploymentCard({
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
  const [busy, setBusy] = useState(false)
  const [statusMsg, setStatusMsg] = useState<string | null>(null)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)

  async function save(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setStatusMsg(null)
    setErrorMsg(null)
    try {
      const updated = await api.updateProject(projectSlug, {
        repo_url: repoUrl.trim() || null,
        track_mode: trackMode,
        track_ref: trackMode === 'off' ? null : trackRef.trim() || null,
        auto_deploy_target: trackMode === 'off' ? 'off' : autoTarget,
        ci_webhook_url: webhookUrl.trim() || null,
      })
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
        <div className="form-grid form-grid-2">
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
      {errorMsg !== null && (
        <p className="bad" role="alert" data-testid="cd-error">
          {errorMsg}
        </p>
      )}
    </section>
  )
}

function UploadedBuilds({
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

  const rows: ArtifactSummary[] = []
  for (const list of artifacts.byTarget.values()) {
    rows.push(...list)
  }

  async function bulkDeploy(version: string, targetFilter: 'dev' | 'all') {
    const key = `${version}:${targetFilter}`
    setDeployingKey(key)
    setBulkStatus(null)
    setBulkError(null)
    try {
      const res = await api.deployProject(projectSlug, {
        version,
        target_filter: targetFilter,
        apply: 'auto',
      })
      onDeployed()
      const count = res.deployed.length
      const skippedCount = Object.keys(res.skipped).length
      setBulkStatus(
        `Staged ${version} to ${count} ${targetFilter} board(s)` +
          (skippedCount > 0 ? ` (${skippedCount} skipped)` : '') +
          '.',
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
      {!artifacts.loaded && artifacts.error === null ? (
        <p aria-busy="true">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="muted">
          No firmware builds uploaded yet. Upload an ESP-IDF app <code>.bin</code> above to enable
          OTA updates.
        </p>
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
            {rows.map((a) => (
              <tr
                key={`${a.target}:${a.version}`}
                data-testid="uploaded-build-row"
                data-target={a.target}
                data-version={a.version}
              >
                <td>
                  <code>{a.target}</code>
                </td>
                <td>
                  <strong>{a.version}</strong>
                  {(a.branch || a.tag || a.commit_sha || a.test_passed != null) && (
                    <div className="muted" data-testid="build-provenance">
                      {a.branch && <code>{a.branch} </code>}
                      {a.tag && <code>tag:{a.tag} </code>}
                      {a.commit_sha && <code>@{a.commit_sha.slice(0, 7)} </code>}
                      {a.test_passed === true && <span className="ok">✓ tests</span>}
                      {a.test_passed === false && <span className="bad">✗ tests</span>}
                    </div>
                  )}
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
                  <button
                    type="button"
                    disabled={deployingKey !== null}
                    onClick={() => void bulkDeploy(a.version, 'dev')}
                    title="Stage this version onto all dev boards in this project"
                  >
                    Deploy dev
                  </button>{' '}
                  <button
                    type="button"
                    disabled={deployingKey !== null}
                    onClick={() => void bulkDeploy(a.version, 'all')}
                    title="Stage this version onto all compatible boards in this project"
                  >
                    Deploy all
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

export function Dashboard({
  me,
  expire,
  signOut,
  health,
  ui = buildInfo,
  createEventSource,
  createFlasher,
}: {
  me: Me
  expire: () => void
  signOut: () => void
  health: HealthState
  ui?: BuildInfo
  // Injected by the tests only: jsdom has no `EventSource` (see `fleet.ts`).
  createEventSource?: EventSourceFactory
  // Injected by the tests only: jsdom has no `navigator.serial`.
  createFlasher?: FlasherFactory
}) {
  const [activeProject, setActiveProject] = useState<string>(
    () => me.projects?.[0]?.slug ?? 'default',
  )
  const [activeTab, setActiveTab] = useState<DashboardTab>('fleet')
  const projectParam = activeProject !== 'default' ? activeProject : undefined
  const fleet = useFleet({ onSessionExpired: expire, createEventSource, project: projectParam })
  const artifacts = useArtifacts({ onSessionExpired: expire, project: projectParam })
  const profiles = useProfiles({ onSessionExpired: expire, hint: fleet.announceSeq })
  const [selected, setSelected] = useState<string | null>(null)
  const board = describeBoard(selected, fleet.devices, fleet.arrivals)
  const effective = board.kind === 'board' ? board.deviceId : null
  // An arrival is a `board` with no fleet row yet: the log endpoint would 404 for it.
  const logDevice =
    effective === null ? null : (fleet.devices?.find((d) => d.device_id === effective) ?? null)
  // One read for the flasher's result card and the table's update result cards (R2b-fe-10).
  const versions = describeVersions(ui, health)

  const deviceCount = fleet.devices?.length ?? 0
  let artifactCount = 0
  for (const list of artifacts.byTarget.values()) {
    artifactCount += list.length
  }
  const unnamedProfiles = pendingProfiles(profiles.profiles ?? []).length

  const tabNav = (
    <nav className="console-tabs" aria-label="Console sections" data-testid="console-tabs">
      <button
        type="button"
        className={`tab-btn${activeTab === 'fleet' ? ' active' : ''}`}
        aria-pressed={activeTab === 'fleet'}
        data-testid="tab-fleet"
        onClick={() => setActiveTab('fleet')}
      >
        1. FLEET ({deviceCount})
      </button>
      <button
        type="button"
        className={`tab-btn${activeTab === 'builds' ? ' active' : ''}`}
        aria-pressed={activeTab === 'builds'}
        data-testid="tab-builds"
        onClick={() => setActiveTab('builds')}
      >
        2. BUILDS ({artifactCount}){unnamedProfiles > 0 ? ` · ! ${unnamedProfiles} MAP` : ''}
      </button>
      <button
        type="button"
        className={`tab-btn${activeTab === 'onboard' ? ' active' : ''}`}
        aria-pressed={activeTab === 'onboard'}
        data-testid="tab-onboard"
        onClick={() => setActiveTab('onboard')}
      >
        3. ADD BOARD
      </button>
      {me.projects && (
        <button
          type="button"
          className={`tab-btn${activeTab === 'access' ? ' active' : ''}`}
          aria-pressed={activeTab === 'access'}
          data-testid="tab-access"
          onClick={() => setActiveTab('access')}
        >
          4. ACCESS
        </button>
      )}
    </nav>
  )

  return (
    <>
      <StatusStrip
        ui={ui}
        health={health}
        board={board}
        project={me.projects ? activeProject : undefined}
      />
      <header className="console-topbar">
        <h1>Fleetforge</h1>
        <p className="muted topbar-user">
          <span>{me.subject}</span>{' '}
          <button type="button" onClick={() => void signOut()}>
            Sign out
          </button>
        </p>
      </header>
      {me.projects ? (
        <ProjectAccess
          me={me}
          activeProject={activeProject}
          onSelectProject={(slug) => {
            setSelected(null)
            setActiveProject(slug)
          }}
          onSessionExpired={expire}
          open={activeTab === 'access'}
          onToggleOpen={() => setActiveTab((curr) => (curr === 'access' ? 'fleet' : 'access'))}
          tabs={tabNav}
        />
      ) : (
        tabNav
      )}

      {/* Panel 1: FLEET — R0's done-when is "watch it come online", and after the
          first board this is what the page is opened for. */}
      <div
        className={`tab-panel${activeTab === 'fleet' ? ' active' : ''}`}
        data-testid="panel-fleet"
      >
        <FleetTable
          fleet={fleet}
          artifacts={artifacts}
          onSessionExpired={expire}
          selectedDeviceId={effective}
          onSelect={setSelected}
          versions={versions}
          onGoToOnboard={() => setActiveTab('onboard')}
          onGoToBuilds={() => setActiveTab('builds')}
        />
        <DeviceLog
          deviceId={effective}
          device={logDevice}
          hasBoards={(fleet.devices?.length ?? 0) > 0}
          onSessionExpired={expire}
        />
      </div>

      {/* Panel 2: BUILDS — Flow 2 step 1 ("Upload a build"), the uploaded builds catalog,
          and partition profiles ("Naming a detected flash map"). */}
      <div
        className={`tab-panel${activeTab === 'builds' ? ' active' : ''}`}
        data-testid="panel-builds"
      >
        <div className="cuj-flow-strip" data-testid="ota-workflow-guide">
          <span>
            <strong>OTA UPDATE FLOW:</strong>
          </span>
          <span>1. Upload ESP-IDF app .bin</span>
          <span aria-hidden="true">→</span>
          <span>2. Match board partition map</span>
          <span aria-hidden="true">→</span>
          <span>3. Select version &amp; Deploy in Fleet</span>
        </div>
        <ContinuousDeploymentCard
          projectSlug={activeProject}
          onDeployed={fleet.refresh}
          onSessionExpired={expire}
        />
        <UploadBuild
          devices={fleet.devices}
          profiles={profiles.profiles}
          onUploaded={artifacts.reload}
          onSessionExpired={expire}
          project={projectParam}
          onGoToFleet={() => setActiveTab('fleet')}
        />
        <UploadedBuilds
          artifacts={artifacts}
          projectSlug={activeProject}
          onGoToFleet={() => setActiveTab('fleet')}
          onDeployed={fleet.refresh}
          onSessionExpired={expire}
        />
        <PartitionProfiles
          profiles={profiles}
          devices={fleet.devices}
          now={fleet.now}
          onSessionExpired={expire}
        />
      </div>

      {/* Panel 3: ADD BOARD — Web Serial browser flasher above manual CLI token screen. */}
      <div
        className={`tab-panel${activeTab === 'onboard' ? ' active' : ''}`}
        data-testid="panel-onboard"
      >
        <FlashBoard
          onSessionExpired={expire}
          onBoardIdentified={setSelected}
          fleet={fleet}
          versions={versions}
          createFlasher={createFlasher}
          project={projectParam}
          onGoToFleet={() => setActiveTab('fleet')}
        />
        <EnrollBoard onSessionExpired={expire} project={projectParam} />
      </div>
    </>
  )
}
