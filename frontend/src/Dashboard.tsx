// The signed-in page body (R2b-fe-1). It exists to own the ONE `useFleet` of the page:
// each instance opens an EventSource and costs one of the API's `sse_max_clients` slots,
// so the strip and the table share this hook rather than opening two. Never render
// `<FleetView>` here for that reason.
//
// Which board the strip shows: the last one picked in the table, deployed to, or
// detected/flashed in the flasher; with nothing picked and exactly one board, that one.
// The flasher also reads the shared fleet (pre-flight card, R2b-fe-2) and the versions
// (result card, R2b-fe-3) — from the props this page already has, with no new hooks.

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
import { StatusStrip } from './StatusStrip'
import { describeBoard, describeVersions } from './statusStrip'

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
  const [selected, setSelected] = useState<string | null>(null)
  const board = describeBoard(selected, fleet.devices, fleet.arrivals)
  const effective = board.kind === 'board' ? board.deviceId : null

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
      />
      {/* Flashing mints its own token, so it sits above the manual token screen:
          the common path is "plug a board in", not "copy a string somewhere". */}
      <FlashBoard
        onSessionExpired={expire}
        onBoardIdentified={setSelected}
        fleet={fleet}
        versions={describeVersions(ui, health)}
        createFlasher={createFlasher}
      />
      <EnrollBoard onSessionExpired={expire} />
    </>
  )
}
