// What the pre-check card says before a deploy is sent (R2b-fe-8). Pure: no React, no
// fetch, so every rule is testable without a DOM. `PrecheckCard.tsx` renders the result
// and nothing else; it renders nothing, this decides everything the card shows.
//
// The server's `message` sentences are NOT touched here (`deploy_precheck.py` writes them
// to be shown verbatim). This module only builds the lines AROUND them: current to
// target, how the build fits the board, and the rollback promise.
//
// Gating warnings (`needs_override`) are R2b-be-7 and do not exist on the server yet, so
// absent means `false`. The only codes ever put in `override` are ones the pre-check
// raised as gating AND the operator ticked.

import { type DeployPrecheck, type PrecheckFinding } from './api'

const NOT_REPORTED = 'not reported'

function bytes(n: number): string {
  return n.toLocaleString('en-US')
}

export function isGating(finding: PrecheckFinding): boolean {
  return finding.needs_override === true
}

/** The warnings that need a tick before a send, in the server's order. */
export function gatingCodes(precheck: DeployPrecheck): string[] {
  return precheck.warnings.filter(isGating).map((w) => w.code)
}

export function canSend(precheck: DeployPrecheck, ticked: ReadonlySet<string>): boolean {
  return (
    precheck.deployable &&
    precheck.refusals.length === 0 &&
    gatingCodes(precheck).every((code) => ticked.has(code))
  )
}

/** The `override` list for the deploy body: ticked AND raised as gating, never anything else. */
export function overrideFor(precheck: DeployPrecheck, ticked: ReadonlySet<string>): string[] {
  return gatingCodes(precheck).filter((code) => ticked.has(code))
}

/** A warning is overridden by the Send click itself, so its label says so. */
export function sendLabel(precheck: DeployPrecheck): 'Send' | 'Send anyway' {
  return precheck.warnings.length > 0 ? 'Send anyway' : 'Send'
}

/** `1.4.2 → 1.5.0` then `esp32c6 · online · sleepy`. `—` for a version the board never reported. */
export function summaryLine(precheck: DeployPrecheck): string {
  const sleepy = precheck.power_class === 'sleepy' ? ' · sleepy' : ''
  return (
    `${precheck.from_version ?? '—'} → ${precheck.version}` +
    ` — ${precheck.target} · ${precheck.device_online ? 'online' : 'offline'}${sleepy}`
  )
}

/** Layout and size against the board's slot, or null when there is no build to describe. */
export function fitLine(precheck: DeployPrecheck): string | null {
  if (precheck.sha256 === null) return null
  // An adopted map: a board on it still announces `unknown`, so name the profile the gate
  // resolved and say what the board announced (R3-fe-1).
  const announced = precheck.device_partition_layout ?? NOT_REPORTED
  const profile = precheck.device_partition_profile
  const board =
    profile != null && profile !== precheck.device_partition_layout
      ? `${profile} (announces ${announced})`
      : announced
  const layout =
    `layout: board ${board}, ` +
    `build ${precheck.artifact_partition_layout ?? NOT_REPORTED}`
  if (precheck.size_bytes === null) return layout
  const size =
    precheck.ota_slot_size === null
      ? `${bytes(precheck.size_bytes)} bytes`
      : `${bytes(precheck.size_bytes)} of ${bytes(precheck.ota_slot_size)} bytes`
  return `${layout} · ${size}`
}

/** The device-armed rollback window, said only when the deploy would go out. */
export function rollbackLine(precheck: DeployPrecheck): string | null {
  if (!precheck.deployable) return null
  const back = precheck.from_version ?? 'the version it runs now'
  return (
    `If ${precheck.version} never reconnects within ${precheck.confirm_timeout_s} s of its ` +
    `reboot, the board rolls back on its own to ${back}.`
  )
}
