// What the pre-flight card says before a flash (R2b-fe-2). Pure: no React, no fetch, so
// every rule is testable without a DOM. `PreflightCard.tsx` renders the result and
// nothing else.
//
// The input is a PREDICTED device id (`predictDeviceId`) and the fleet list the page
// already holds. Nothing here is derived from device state: `online` and
// `deploy.is_terminal` are the server's answers. A retired board is not in
// `GET /v1/devices`, so it reads as "not on the fleet" — the copy claims no more.

import { type AgentManifest, type ArrivalSummary, type DeviceSummary } from './api'
import { DEPLOY_STATE_LABELS } from './deploy'
import { STAGE_LABELS } from './fleet'
import { type ChipInfo } from './flasher'

export type PreflightInstall = { agentVersion: string; layout: string }
export type Preflight =
  | { kind: 'unknown-id' }
  | { kind: 'checking'; error: string | null }
  | {
      kind: 'new'
      deviceId: string
      install: PreflightInstall | null
      arrival: { stage: string; stalled: boolean } | null
    }
  | {
      kind: 'known'
      deviceId: string
      name: string | null
      platform: string
      firmware: string | null
      online: boolean
      lastSeen: string | null
      updating: string | null
      install: PreflightInstall | null
      layoutChange: { from: string; to: string } | null
    }

/** The one build for this chip, or null. Never throws (`selectBuild` does, for the flash). */
export function installFor(
  manifest: AgentManifest | null,
  chip: ChipInfo,
): PreflightInstall | null {
  if (manifest === null) return null
  const builds = manifest.builds.filter((b) => b.chip_family === chip.chipName)
  if (builds.length !== 1) return null
  return {
    agentVersion: builds[0].agent_version,
    layout: builds[0].partition_layout,
  }
}

export function describePreflight(input: {
  predictedId: string | null
  devices: DeviceSummary[] | null
  arrivals: ArrivalSummary[]
  fleetError: string | null
  install: PreflightInstall | null
}): Preflight {
  const { predictedId, devices, arrivals, fleetError, install } = input
  if (predictedId === null) return { kind: 'unknown-id' }
  if (devices === null) return { kind: 'checking', error: fleetError }
  const id = predictedId.toLowerCase()
  const device = devices.find((d) => d.device_id.toLowerCase() === id)
  if (device !== undefined) {
    const from = device.partition_layout
    return {
      kind: 'known',
      deviceId: device.device_id,
      name: device.name,
      platform: device.platform_type,
      firmware: device.fw_version,
      online: device.online,
      lastSeen: device.last_seen,
      updating:
        device.deploy !== null && !device.deploy.is_terminal
          ? (DEPLOY_STATE_LABELS[device.deploy.state] ?? device.deploy.state)
          : null,
      install,
      layoutChange:
        from !== null && install !== null && from !== install.layout
          ? { from, to: install.layout }
          : null,
    }
  }
  const arrival = arrivals.find((a) => a.device_id.toLowerCase() === id)
  return {
    kind: 'new',
    deviceId: id,
    install,
    arrival:
      arrival === undefined
        ? null
        : {
            stage: STAGE_LABELS[arrival.stage] ?? arrival.stage,
            stalled: arrival.stalled,
          },
  }
}
