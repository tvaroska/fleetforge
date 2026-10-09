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

import { useState } from 'react'
import { type Me } from './api'
import { buildInfo, type BuildInfo } from './buildInfo'
import { useArtifacts } from './deploy'
import { EnrollBoard } from './EnrollBoard'
import { type FlasherFactory } from './flasher'
import { FlashBoard } from './FlashBoard'
import { FleetTable } from './FleetView'
import { useFleet, type EventSourceFactory } from './fleet'
import { type HealthState } from './health'
import { PartitionProfiles } from './PartitionProfiles'
import { useProfiles } from './profiles'
import { StatusStrip } from './StatusStrip'
import { describeBoard, describeVersions } from './statusStrip'
import { UploadBuild } from './UploadBuild'

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
  const fleet = useFleet({ onSessionExpired: expire, createEventSource })
  const artifacts = useArtifacts({ onSessionExpired: expire })
  const profiles = useProfiles({ onSessionExpired: expire, hint: fleet.announceSeq })
  const [selected, setSelected] = useState<string | null>(null)
  const board = describeBoard(selected, fleet.devices, fleet.arrivals)
  const effective = board.kind === 'board' ? board.deviceId : null
  // One read for the flasher's result card and the table's update result cards (R2b-fe-10).
  const versions = describeVersions(ui, health)

  return (
    <>
      <StatusStrip ui={ui} health={health} board={board} />
      <header>
        <h1>Fleetforge</h1>
        <p className="muted">
          {me.subject}{' '}
          <button type="button" onClick={() => void signOut()}>
            Sign out
          </button>
        </p>
      </header>
      {/* The fleet first: R0's done-when is "watch it come online", and after the
          first board this is what the page is opened for. */}
      <FleetTable
        fleet={fleet}
        artifacts={artifacts}
        onSessionExpired={expire}
        selectedDeviceId={effective}
        onSelect={setSelected}
        versions={versions}
      />
      {/* Naming a detected flash map comes before uploading a build for it. */}
      <PartitionProfiles
        profiles={profiles}
        devices={fleet.devices}
        now={fleet.now}
        onSessionExpired={expire}
      />
      {/* Flow 2 step 1 sits next to the table whose Deploy column it feeds; a finished
          upload re-reads the one artifact list, so the new version shows with no reload. */}
      <UploadBuild
        devices={fleet.devices}
        profiles={profiles.profiles}
        onUploaded={artifacts.reload}
        onSessionExpired={expire}
      />
      {/* Flashing mints its own token, so it sits above the manual token screen:
          the common path is "plug a board in", not "copy a string somewhere". */}
      <FlashBoard
        onSessionExpired={expire}
        onBoardIdentified={setSelected}
        fleet={fleet}
        versions={versions}
        createFlasher={createFlasher}
      />
      <EnrollBoard onSessionExpired={expire} />
    </>
  )
}
