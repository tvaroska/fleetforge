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

import { useEffect, useState } from 'react'
import {
  ApiError,
  api,
  type ArtifactSummary,
  type DeviceGroupSummary,
  type Me,
} from './api'
import { buildInfo, type BuildInfo } from './buildInfo'
import { BuildPipelineCard } from './BuildPipelineCard'
import { ContinuousDeploymentCard } from './ContinuousDeploymentCard'
import { useArtifacts, type Artifacts } from './deploy'
import { DeployPolicyCard } from './DeployPolicyCard'
import { DeviceInspector } from './DeviceInspector'
import { DeviceLog } from './DeviceLog'
import { EnrollBoard } from './EnrollBoard'
import { type FlasherFactory } from './flasher'
import { FlashBoard } from './FlashBoard'
import { FleetTable } from './FleetView'
import { useFleet, type EventSourceFactory } from './fleet'
import { formatWhen } from './format'
import { type HealthState } from './health'
import { PartitionProfiles } from './PartitionProfiles'
import { PitDockCockpit } from './PitDockCockpit'
import { ProjectAccess } from './ProjectAccess'
import { pendingProfiles, shortSha, useProfiles } from './profiles'
import { SimulationBadge } from './SimulationBadge'
import { StatusStrip } from './StatusStrip'
import { describeBoard, describeVersions } from './statusStrip'
import { UploadBuild } from './UploadBuild'

export type DashboardTab = 'fleet' | 'builds' | 'onboard' | 'access'

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
      const skippedCount = Object.keys(res.skipped).length
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
            {rows.map((a) => {
              const rowKey = `${a.target}:${a.version}`
              const rowGroupId = targetGroupByRow[rowKey] ?? ''
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
                      {a.build_url && (
                        <>
                          <a
                            href={a.build_url}
                            target="_blank"
                            rel="noreferrer"
                            data-testid="build-ci-link"
                          >
                            ↗ CI run
                          </a>{' '}
                        </>
                      )}
                      {a.repo_url && (
                        <a
                          href={a.repo_url}
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
  const [compactDensity, setCompactDensity] = useState(false)
  const [pitContrast, setPitContrast] = useState(false)

  useEffect(() => {
    if (typeof document === 'undefined') return
    if (compactDensity) {
      document.documentElement.setAttribute('data-density', 'compact')
    } else {
      document.documentElement.removeAttribute('data-density')
    }
  }, [compactDensity])

  useEffect(() => {
    if (typeof document === 'undefined') return
    if (pitContrast) {
      document.documentElement.setAttribute('data-theme', 'pit-contrast')
    } else {
      document.documentElement.removeAttribute('data-theme')
    }
  }, [pitContrast])

  const projectParam = activeProject !== 'default' ? activeProject : undefined
  const fleet = useFleet({ onSessionExpired: expire, createEventSource, project: projectParam })
  const artifacts = useArtifacts({ onSessionExpired: expire, project: projectParam })
  const profiles = useProfiles({ onSessionExpired: expire, hint: fleet.announceSeq })
  const [groups, setGroups] = useState<DeviceGroupSummary[]>([])

  useEffect(() => {
    let active = true
    if (typeof api.listGroups === 'function') {
      api
        .listGroups(projectParam)
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
  }, [projectParam])
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
      <header
        className="console-topbar"
        data-density={compactDensity ? 'compact' : undefined}
        data-theme={pitContrast ? 'pit-contrast' : undefined}
      >
        <h1>Fleetforge</h1>
        <p className="muted topbar-user">
          <button
            type="button"
            aria-pressed={compactDensity}
            data-testid="toggle-rack-density"
            onClick={() => setCompactDensity((v) => !v)}
          >
            32px Rack
          </button>{' '}
          <button
            type="button"
            aria-pressed={pitContrast}
            data-testid="toggle-pit-contrast"
            onClick={() => setPitContrast((v) => !v)}
          >
            ☀ Pit Contrast
          </button>{' '}
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
          devices={fleet.devices}
        />
      ) : (
        tabNav
      )}

      {/* Panel 1: FLEET — R0's done-when is "watch it come online", and after the
          first board this is what the page is opened for. */}
      <div
        className={`tab-panel${activeTab === 'fleet' ? ' active' : ''}`}
        data-testid="panel-fleet"
        data-density={compactDensity ? 'compact' : undefined}
        data-theme={pitContrast ? 'pit-contrast' : undefined}
      >
        <div className="workbench-split" data-testid="workbench-split">
          <div className="workbench-master">
            <FleetTable
              fleet={fleet}
              artifacts={artifacts}
              onSessionExpired={expire}
              selectedDeviceId={effective}
              onSelect={setSelected}
              versions={versions}
              onGoToOnboard={() => setActiveTab('onboard')}
              onGoToBuilds={() => setActiveTab('builds')}
              groups={groups}
            />
          </div>
          <div className="workbench-inspector-col">
            <DeviceInspector
              device={logDevice}
              selectedDeviceId={effective}
              devices={fleet.devices ?? []}
              artifacts={artifacts}
              groups={groups}
              project={activeProject}
              now={fleet.now}
              onUpdated={fleet.refresh}
              onDeployed={fleet.refresh}
              onSessionExpired={expire}
            />
            <DeviceLog
              deviceId={effective}
              device={logDevice}
              hasBoards={(fleet.devices?.length ?? 0) > 0}
              onSessionExpired={expire}
            />
          </div>
        </div>
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
        <DeployPolicyCard
          projectSlug={activeProject}
          active={activeTab === 'builds'}
          groups={groups}
          onDeployed={fleet.refresh}
          onSessionExpired={expire}
        />
        <BuildPipelineCard
          projectSlug={activeProject}
          active={activeTab === 'builds'}
          profiles={profiles.profiles}
          onArtifactsChanged={artifacts.reload}
          onSessionExpired={expire}
          createEventSource={createEventSource}
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
          groups={groups}
          onGoToFleet={() => setActiveTab('fleet')}
        />
        <EnrollBoard onSessionExpired={expire} project={projectParam} groups={groups} />
        <details
          className="console-card"
          data-testid="pit-dock-drawer"
          open={(fleet.devices ?? []).some((d) => d.link_type === 'dock')}
        >
          <summary>Offline Radio Lockout &amp; USB-C Dock Sync</summary>
          <PitDockCockpit
            devices={fleet.devices}
            artifacts={artifacts}
            project={projectParam}
            selectedDeviceId={effective}
            onSelectDevice={setSelected}
            onSynced={fleet.refresh}
            onSessionExpired={expire}
          />
        </details>
      </div>
    </>
  )
}
