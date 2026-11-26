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

import { useEffect, useRef, useState } from 'react'
import { api, type DeviceGroupSummary, type Me } from './api'
import { buildInfo, type BuildInfo } from './buildInfo'
import { BuildPipelineCard } from './BuildPipelineCard'
import { ContinuousDeploymentCard } from './ContinuousDeploymentCard'
import { useArtifacts } from './deploy'
import { DeployPolicyCard } from './DeployPolicyCard'
import { DeviceInspector } from './DeviceInspector'
import { DeviceLog } from './DeviceLog'
import { EnrollBoard } from './EnrollBoard'
import { type FlasherFactory } from './flasher'
import { FlashBoard } from './FlashBoard'
import { FleetTable } from './FleetView'
import { useFleet, type EventSourceFactory } from './fleet'
import { type HealthState } from './health'
import { PartitionProfiles } from './PartitionProfiles'
import { PitDockCockpit } from './PitDockCockpit'
import { ProjectAccess } from './ProjectAccess'
import { pendingProfiles, useProfiles } from './profiles'
import { StatusStrip } from './StatusStrip'
import { describeBoard, describeVersions } from './statusStrip'
import { SwarmTopologyCard } from './SwarmTopologyCard'
import { UploadBuild } from './UploadBuild'
import { UploadedBuilds } from './UploadedBuilds'

export type DashboardTab = 'fleet' | 'builds' | 'onboard' | 'access'

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
  const [dockDrawerOpen, setDockDrawerOpen] = useState(false)
  const sawDockBoard = useRef(false)

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

  const hasDockBoard = (fleet.devices ?? []).some((d) => d.link_type === 'dock')
  useEffect(() => {
    if (hasDockBoard && !sawDockBoard.current) {
      sawDockBoard.current = true
      setDockDrawerOpen(true)
    } else if (!hasDockBoard) {
      sawDockBoard.current = false
    }
  }, [hasDockBoard])

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
        <SwarmTopologyCard
          projectSlug={activeProject}
          active={activeTab === 'builds'}
          artifacts={artifacts}
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
          open={dockDrawerOpen}
          onToggle={(event) => setDockDrawerOpen(event.currentTarget.open)}
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
