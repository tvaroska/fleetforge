// What the fleet table's Health cell says about a board (R4-fe-1). Pure: no React, no
// clock, no fetch. `FleetView.tsx::HealthCell` renders what these functions return and
// decides nothing.
//
// Rules, from `DECISIONS.md` (R4-be-1, R4-fw-1) and the plan:
//
// - The server is authoritative. Fault counts, `online` and the heartbeat's health group
//   arrive computed; nothing here re-derives them, and nothing reads an SSE body
//   (`fleet.ts` rule 1: a frame is a hint, `GET /v1/devices` is the record).
// - `last_reset_reason` is an OPEN vocabulary. A lookup with a fallback, never an
//   exhaustive switch: an agent newer than this dashboard shows its reason as itself,
//   unbadged, because we must not claim a fault we cannot name.
// - Monochrome theme (`index.css`): the warning is a word, a weight and a box, never a tint.
// - There is no low-heap or weak-RSSI threshold: neither the spec nor the server defines
//   one, and inventing a number would be a product decision, not rendering.
// - Not `health.ts`: that module is the API's `/v1/healthz` poll.

import { type FaultResets, type HealthSnapshot } from './api'
import { formatBytes, formatDuration } from './format'

// Plain-language labels, used in `title` only: the visible text is the raw device name so
// it matches serial logs. The 16 names of `spec/device-protocol.md` -> up/announce.
export const RESET_REASON_LABELS: Record<string, string> = {
  poweron: 'power-on or chip reset',
  ext: 'reset pin',
  sw: 'software restart',
  panic: 'crashed (panic)',
  int_wdt: 'interrupt watchdog',
  task_wdt: 'task watchdog',
  wdt: 'watchdog',
  deepsleep: 'woke from deep sleep',
  brownout: 'supply voltage dip (brownout)',
  sdio: 'SDIO reset',
  usb: 'USB reset',
  jtag: 'JTAG reset',
  efuse: 'eFuse error',
  pwr_glitch: 'power glitch',
  cpu_lockup: 'CPU lockup',
  unknown: 'not identified by the chip',
}

// The spontaneous / hardware-fault resets get the warning badge. Deliberately broader
// than the server's three counters (panic, task_wdt, brownout): the badge describes the
// current boot's cause, the counters are the server's history.
export const FAULT_RESET_REASONS: ReadonlySet<string> = new Set([
  'panic',
  'int_wdt',
  'task_wdt',
  'wdt',
  'brownout',
  'cpu_lockup',
  'pwr_glitch',
  'efuse',
])

/** How many metrics show inline; the rest go behind a `<details>`. */
export const METRICS_INLINE = 4

export type ResetDescription = { reason: string; label: string | null; fault: boolean }

/** `null` for "not reported" (absent, null, empty). An unknown name has `label: null`. */
export function describeReset(reason: string | null | undefined): ResetDescription | null {
  if (reason === null || reason === undefined || reason === '') return null
  return {
    reason,
    label: Object.hasOwn(RESET_REASON_LABELS, reason) ? RESET_REASON_LABELS[reason] : null,
    fault: FAULT_RESET_REASONS.has(reason),
  }
}

/** `false` means the running image has not confirmed yet; `null` means not reported. */
export function describeBoot(health: HealthSnapshot | null | undefined): 'ok' | 'unconfirmed' | null {
  const ok = health?.boot_ok
  if (ok === undefined || ok === null) return null
  return ok ? 'ok' : 'unconfirmed'
}

/** `2 h` for a whole number of hours, else a duration (`1 h 30 min`). */
export function formatWindow(windowS: number): string {
  return Number.isInteger(windowS / 3600) && windowS > 0
    ? `${windowS / 3600} h`
    : formatDuration(windowS * 1000)
}

/** `panic ×2, brownout ×1 in 24 h`, or `null` when nothing faulted (or nothing was sent). */
export function describeFaults(f: FaultResets | null | undefined): string | null {
  if (f === null || f === undefined) return null
  const parts = (['panic', 'task_wdt', 'brownout'] as const)
    .filter((name) => f[name] > 0)
    .map((name) => `${name} ×${f[name]}`)
  return parts.length === 0 ? null : `${parts.join(', ')} in ${formatWindow(f.window_s)}`
}

export type Vitals = { parts: string[]; stale: boolean; title: string }

/**
 * Heap low-water mark, uptime and signal, each only when reported. `stale` is an offline
 * board: its last heartbeat is what it said, not what is true now.
 */
export function describeVitals(
  health: HealthSnapshot | null | undefined,
  online: boolean,
  formatAt: (iso: string) => string = (iso) => iso,
): Vitals | null {
  if (health === null || health === undefined) return null
  const parts: string[] = []
  if (health.min_free_heap !== null) parts.push(`heap low ${formatBytes(health.min_free_heap)}`)
  if (health.uptime_s !== null) parts.push(`up ${formatDuration(health.uptime_s * 1000)}`)
  if (health.rssi !== null) parts.push(`${health.rssi} dBm`)
  if (parts.length === 0) return null

  const notes: string[] = []
  if (health.min_free_heap !== null) notes.push('heap low: lowest free heap since this boot')
  if (health.free_heap !== null) notes.push(`free now ${formatBytes(health.free_heap)}`)
  notes.push(`reported ${formatAt(health.at)}`)
  return { parts, stale: !online, title: notes.join('; ') }
}

/** Integers as they are, anything else to 6 significant digits without trailing zeros. */
export function formatMetricValue(v: number): string {
  if (!Number.isFinite(v)) return String(v)
  return Number.isInteger(v) ? String(v) : String(Number(v.toPrecision(6)))
}
