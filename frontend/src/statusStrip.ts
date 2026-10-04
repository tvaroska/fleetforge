// What the status strip says (R2b-fe-1). Pure: no React, no fetch, so every rule is
// testable without a DOM. `StatusStrip.tsx` renders the result and nothing else.
//
// Nothing here is derived from device state: `online` and `deploy.is_terminal` are the
// server's answers, and the verdict is `deployOutcome` (the CUJ-1 drift rule). A later
// task (R2b-fe-3/-5) can append an onboarding-console segment to `state`.

import { type ArrivalSummary, type DeviceSummary } from './api'
import { UNKNOWN, describeBuild, type BuildInfo } from './buildInfo'
import { DEPLOY_BAD_STATES, DEPLOY_STATE_LABELS, deployOutcome } from './deploy'
import { STAGE_LABELS } from './fleet'
import { type HealthState } from './health'

export type Tone = 'ok' | 'bad' | 'warn' | null
export type Segment = { text: string; tone: Tone }
export type VersionLine = {
  ui: string
  uiTitle: string
  api: string
  apiTitle?: string
  differ: boolean
}
export type BoardLine =
  | { kind: 'loading' }
  | { kind: 'none'; reason: 'no-boards' | 'not-selected' }
  | {
      kind: 'board'
      deviceId: string
      name: string | null
      platform: string | null
      firmware: string
      state: Segment[]
    }

const COMMIT_PREFIX = 8

function commitsDiffer(a: string, b: string | undefined): boolean {
  if (b === undefined || a === UNKNOWN || a === '' || b === UNKNOWN || b === '') return false
  return a.slice(0, COMMIT_PREFIX).toLowerCase() !== b.slice(0, COMMIT_PREFIX).toLowerCase()
}

export function describeVersions(ui: BuildInfo, health: HealthState): VersionLine {
  const uiText = describeBuild(ui.version, ui.commit)
  const uiTitle = `built ${ui.builtAt}`
  if (health.phase === 'loading') {
    return { ui: uiText, uiTitle, api: 'checking…', differ: false }
  }
  if (health.phase === 'unreachable') {
    return { ui: uiText, uiTitle, api: 'unreachable', differ: false }
  }
  const h = health.health
  const versionsDiffer =
    ui.version !== UNKNOWN && h.version !== UNKNOWN && ui.version !== h.version
  return {
    ui: uiText,
    uiTitle,
    api: describeBuild(h.version, h.commit ?? UNKNOWN),
    apiTitle: h.built_at ? `built ${h.built_at}` : undefined,
    differ: versionsDiffer || commitsDiffer(ui.commit, h.commit),
  }
}

function deviceState(device: DeviceSummary): Segment[] {
  const segments: Segment[] = [
    device.online ? { text: 'online', tone: 'ok' } : { text: 'offline', tone: null },
  ]
  const deploy = device.deploy
  if (deploy === null) return segments
  if (!deploy.is_terminal) {
    segments.push({
      text: `updating: ${DEPLOY_STATE_LABELS[deploy.state] ?? deploy.state}`,
      tone: null,
    })
    return segments
  }
  const outcome = deployOutcome(deploy, device.fw_version)
  if (outcome !== null) {
    // Drift says nothing: the table row explains it, and the strip must not claim `good`.
    if (outcome.kind === 'verdict') {
      segments.push({ text: `last update ${outcome.word}`, tone: outcome.tone })
    }
  } else if (DEPLOY_BAD_STATES.has(deploy.state)) {
    segments.push({
      text: `last update: ${DEPLOY_STATE_LABELS[deploy.state] ?? deploy.state}`,
      tone: 'bad',
    })
  }
  return segments
}

function deviceFirmware(device: DeviceSummary): string {
  const deploy = device.deploy
  if (deploy !== null && !deploy.is_terminal && deploy.artifact_version !== null) {
    return `fw ${deploy.from_version ?? device.fw_version ?? '—'} → ${deploy.artifact_version}`
  }
  return `fw ${device.fw_version ?? '—'}`
}

export function describeBoard(
  selectedId: string | null,
  devices: DeviceSummary[] | null,
  arrivals: ArrivalSummary[],
): BoardLine {
  if (devices === null) return { kind: 'loading' }
  // Nothing picked and exactly one board: it is the only thing the operator can mean.
  const id = selectedId ?? (devices.length === 1 ? devices[0].device_id : null)
  if (id === null) {
    return { kind: 'none', reason: devices.length === 0 ? 'no-boards' : 'not-selected' }
  }
  const device = devices.find((d) => d.device_id === id)
  if (device !== undefined) {
    return {
      kind: 'board',
      deviceId: id,
      name: device.name,
      platform: device.platform_type,
      firmware: deviceFirmware(device),
      state: deviceState(device),
    }
  }
  const arrival = arrivals.find((a) => a.device_id === id)
  const state: Segment[] =
    arrival === undefined
      ? [{ text: 'not on the fleet yet', tone: null }]
      : [{ text: `arriving: ${STAGE_LABELS[arrival.stage] ?? arrival.stage}`, tone: null }]
  if (arrival?.stalled) state.push({ text: 'stalled', tone: 'bad' })
  return { kind: 'board', deviceId: id, name: null, platform: null, firmware: 'fw —', state }
}
