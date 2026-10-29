// Progressive 3-Tier Release & Convergence Ribbon (P1-4 + H6 Slide-Down Matrix Drawer).
//
// - Tier 1 (Maker Summary Bar when `(groups?.length ?? 0) === 0`): Computes convergence
//   scoped per target / sketch (`device.platform_type`) so heterogeneous maker boards
//   (`esp32`, `esp32c3`, `esp32s3`) never trigger false project-wide version drift.
// - Tier 2/3 (4-Stage Release Pipeline Rail when `groups.length > 0`): Shows stage
//   convergence across `0. Dev (★dev)` -> `1. Canary` -> `2. Beta` -> `3. Prod` with
//   1-bit block bars (`████░░`), circuit-breaker halt status (`[✗ HALTED — ROLLBACK DETECTED]`),
//   companion cargo `sub_versions` summary, and `secrets_rev` convergence chip.
// - H6 Enhancement #1: On-Demand Slide-Down `Artifact × Ring × ff_sec` Matrix Drawer
//   toggled via `[▼ Expand Release & Cargo Matrix]` (`data-testid="toggle-release-matrix"`).
//
// Purely derived from already-loaded `devices`, `artifacts`, and `groups` props —
// never triggers any mount `fetch` call.

import { useState } from 'react'
import {
  type DeployKpisSummary,
  type DeviceGroupSummary,
  type DeviceSummary,
} from './api'
import { type Artifacts } from './deploy'

export type ReleaseConvergenceRibbonProps = {
  devices: DeviceSummary[]
  artifacts: Artifacts
  groups?: DeviceGroupSummary[]
  activeFilter?: string
  onFilterChange?: (filter: string) => void
  onOpenCompare?: () => void
  kpis?: DeployKpisSummary | null
}

type StageSummary = {
  key: 'dev' | 'canary' | 'beta' | 'prod'
  label: string
  devices: DeviceSummary[]
  converged: number
  total: number
  halted: boolean
  blockBar: string
}

/** 6-cell 1-bit block bar (`████░░`) from converged / total. */
export function formatBlockBar(converged: number, total: number, width = 6): string {
  if (total <= 0) return '░'.repeat(width)
  const filled = Math.max(0, Math.min(width, Math.round((converged / total) * width)))
  return '█'.repeat(filled) + '░'.repeat(width - filled)
}

/** Latest `app` artifact version for a target chip, or fallback to cohort majority version. */
export function targetBaselineVersion(
  platformType: string,
  targetDevices: DeviceSummary[],
  artifacts: Artifacts,
): string | null {
  const targetList = artifacts.byTarget.get(platformType) ?? []
  const appArtifacts = targetList.filter((a) => !a.artifact_type || a.artifact_type === 'app')
  if (appArtifacts.length > 0) {
    return appArtifacts[0]!.version
  }
  const counts = new Map<string, number>()
  let bestVersion: string | null = null
  let bestCount = 0
  for (const d of targetDevices) {
    if (d.fw_version !== null && d.fw_version !== '') {
      const next = (counts.get(d.fw_version) ?? 0) + 1
      counts.set(d.fw_version, next)
      if (next > bestCount) {
        bestCount = next
        bestVersion = d.fw_version
      }
    }
  }
  return bestVersion
}

/** True when `device` has converged to its own target's baseline `app` version. */
export function isDeviceConvergedForTarget(
  device: DeviceSummary,
  baselinesByTarget: ReadonlyMap<string, string | null>,
): boolean {
  const baseline = baselinesByTarget.get(device.platform_type) ?? null
  if (baseline === null) return device.fw_version !== null
  return device.fw_version === baseline
}

function classifyStage(
  device: DeviceSummary,
  groupNameById: ReadonlyMap<string, string>,
): 'dev' | 'canary' | 'beta' | 'prod' {
  if (device.is_dev) return 'dev'
  const rawName = (device.group_id ? groupNameById.get(device.group_id) ?? '' : '').toLowerCase()
  if (/dev|bench|lab/.test(rawName)) return 'dev'
  if (/canary|alpha|ring[-_\s]*1/.test(rawName)) return 'canary'
  if (/beta|stag|ring[-_\s]*2|soak/.test(rawName)) return 'beta'
  return 'prod'
}

export function ReleaseConvergenceRibbon({
  devices,
  artifacts,
  groups = [],
  activeFilter,
  onFilterChange,
  onOpenCompare,
}: ReleaseConvergenceRibbonProps) {
  const [matrixOpen, setMatrixOpen] = useState(false)

  if (devices.length === 0) return null

  // Group devices by platform_type so Maker convergence is strictly per-target / per-sketch.
  const devicesByTarget = new Map<string, DeviceSummary[]>()
  for (const d of devices) {
    const bucket = devicesByTarget.get(d.platform_type)
    if (bucket) bucket.push(d)
    else devicesByTarget.set(d.platform_type, [d])
  }

  const baselinesByTarget = new Map<string, string | null>()
  for (const [target, list] of devicesByTarget.entries()) {
    baselinesByTarget.set(target, targetBaselineVersion(target, list, artifacts))
  }

  const targetSummaries = Array.from(devicesByTarget.entries()).map(([target, list]) => {
    const baseline = baselinesByTarget.get(target) ?? null
    const converged = list.filter((d) => isDeviceConvergedForTarget(d, baselinesByTarget)).length
    const drifted = list.length - converged
    return { target, baseline, converged, total: list.length, drifted }
  })

  const totalConverged = targetSummaries.reduce((acc, t) => acc + t.converged, 0)
  const totalDrifted = devices.length - totalConverged

  // Companion cargo `sub_versions` aggregation across the fleet.
  const cargoMap = new Map<string, Map<string, number>>()
  for (const d of devices) {
    if (!d.sub_versions) continue
    for (const [key, ver] of Object.entries(d.sub_versions)) {
      let verCounts = cargoMap.get(key)
      if (!verCounts) {
        verCounts = new Map<string, number>()
        cargoMap.set(key, verCounts)
      }
      verCounts.set(ver, (verCounts.get(ver) ?? 0) + 1)
    }
  }

  const cargoSummaries = Array.from(cargoMap.entries()).map(([key, verCounts]) => {
    let topVer = '—'
    let topCount = 0
    let totalReporting = 0
    for (const [ver, count] of verCounts.entries()) {
      totalReporting += count
      if (count > topCount) {
        topCount = count
        topVer = ver
      }
    }
    const skewCount = totalReporting - topCount
    return { key, topVer, topCount, totalReporting, skewCount }
  })

  // Secrets revision (`ff_sec` / `secrets_rev`) convergence across devices.
  const reportedSecretsRevs = devices
    .map((d) => d.secrets_rev)
    .filter((r): r is number => typeof r === 'number')
  const maxSecretsRev = reportedSecretsRevs.length > 0 ? Math.max(...reportedSecretsRevs) : 0
  const laggingSecretsBoards =
    maxSecretsRev > 0
      ? devices.filter((d) => d.secrets_rev != null && d.secrets_rev < maxSecretsRev)
      : []
  const syncedSecretsCount =
    maxSecretsRev > 0
      ? devices.filter((d) => d.secrets_rev === maxSecretsRev).length
      : devices.length

  // Circuit-breaker detection: any board in `rolled_back`, `failed`, or `safe_mode`.
  const haltedBoards = devices.filter(
    (d) =>
      (d as DeviceSummary & { safe_mode?: boolean }).safe_mode === true ||
      d.deploy?.state === 'rolled_back' ||
      d.deploy?.state === 'failed',
  )

  const groupNameById = new Map<string, string>()
  for (const g of groups) {
    groupNameById.set(g.id, g.name)
  }

  const stageDefs: ReadonlyArray<{ key: StageSummary['key']; label: string }> = [
    { key: 'dev', label: '0. Dev (★dev)' },
    { key: 'canary', label: '1. Canary' },
    { key: 'beta', label: '2. Beta' },
    { key: 'prod', label: '3. Prod' },
  ]

  const stages: StageSummary[] = stageDefs.map(({ key, label }) => {
    const stageDevices = devices.filter((d) => classifyStage(d, groupNameById) === key)
    const converged = stageDevices.filter((d) =>
      isDeviceConvergedForTarget(d, baselinesByTarget),
    ).length
    const halted = stageDevices.some(
      (d) =>
        (d as DeviceSummary & { safe_mode?: boolean }).safe_mode === true ||
        d.deploy?.state === 'rolled_back' ||
        d.deploy?.state === 'failed',
    )
    return {
      key,
      label,
      devices: stageDevices,
      converged,
      total: stageDevices.length,
      halted,
      blockBar: formatBlockBar(converged, stageDevices.length, 6),
    }
  })

  const isMultiRing = groups.length > 0

  return (
    <div
      className="release-ribbon"
      data-testid="release-convergence-ribbon"
      data-tier={isMultiRing ? 'pipeline' : 'maker'}
    >
      {!isMultiRing ? (
        /* Tier 1: Maker Summary Bar (scoped per target / sketch so heterogeneous boards never false-drift) */
        <div className="maker-convergence-bar" data-testid="maker-convergence-bar">
          <span data-testid="maker-convergence-summary">
            <strong>
              {totalConverged}/{devices.length} converged
            </strong>{' '}
            · {targetSummaries.length}{' '}
            {targetSummaries.length === 1 ? 'target sketch' : 'target sketches'} (
            {totalDrifted === 0 ? '0 drift ✓' : `! ${totalDrifted} drift`})
          </span>
          {' · '}
          <span className="maker-target-list" data-testid="maker-target-list">
            {targetSummaries.map((t, idx) => (
              <span key={t.target} data-testid="maker-target-pill" data-target={t.target}>
                {idx > 0 && ' · '}
                <code>{t.target}</code>: {t.baseline ?? '—'} ({t.converged}/{t.total}{' '}
                {t.drifted === 0 ? '✓' : `! ${t.drifted} drift`})
              </span>
            ))}
          </span>
        </div>
      ) : (
        /* Tier 2/3: 4-Stage Release Pipeline Rail */
        <div className="release-pipeline-rail" data-testid="release-pipeline-rail">
          <div className="pipeline-stages">
            {stages.map((stage, idx) => (
              <span
                key={stage.key}
                className={`pipeline-stage${stage.halted ? ' bad' : ''}`}
                data-testid="pipeline-stage"
                data-stage={stage.key}
              >
                {idx > 0 && <span aria-hidden="true"> → </span>}
                <strong>{stage.label}</strong>{' '}
                <code data-testid="stage-block-bar">{stage.blockBar}</code>{' '}
                <span>
                  {stage.converged}/{stage.total}{' '}
                  {stage.halted ? '✗ HALT' : stage.total > 0 && stage.converged === stage.total ? '✓' : ''}
                </span>
              </span>
            ))}
          </div>

          {haltedBoards.length > 0 && (
            <div className="bad circuit-breaker-banner" data-testid="circuit-breaker-banner">
              <strong>[✗ HALTED — ROLLBACK DETECTED]</strong>{' '}
              <span>
                {haltedBoards
                  .map((b) => {
                    const reason = b.deploy?.selftest?.reason ?? b.deploy?.detail ?? b.deploy?.state ?? 'safe_mode'
                    const slot = b.deploy?.selftest?.returned_to ?? 'previous slot'
                    return `${b.name ?? b.device_id} (${reason} → ${slot})`
                  })
                  .join(' · ')}
              </span>{' '}
              {(onOpenCompare || onFilterChange) && (
                <button
                  type="button"
                  className="flag-pill"
                  data-testid="ribbon-open-compare"
                  onClick={() => {
                    onFilterChange?.('attention')
                    onOpenCompare?.()
                  }}
                >
                  Compare {haltedBoards.length} failed ▸
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {/* Companion cargo sub_versions, secrets_rev convergence chip, and H6 Matrix toggle */}
      <div className="ribbon-footer">
        {cargoSummaries.length > 0 && (
          <span className="muted" data-testid="ribbon-cargo-summary">
            Cargo:{' '}
            {cargoSummaries.map((c, idx) => (
              <span key={c.key}>
                {idx > 0 && ' · '}
                <code>
                  {c.key}={c.topVer}
                </code>{' '}
                ({c.topCount}/{c.totalReporting}{' '}
                {c.skewCount === 0 ? '✓' : `! ${c.skewCount} skew`})
              </span>
            ))}
            {' · '}
          </span>
        )}

        {maxSecretsRev > 0 && (
          <span data-testid="ribbon-secrets-chip">
            Secrets: <code>sec:r{maxSecretsRev}</code> ({syncedSecretsCount}/{devices.length} ✓)
            {laggingSecretsBoards.length > 0 && (
              <>
                {' '}
                <button
                  type="button"
                  className={`flag-pill warn${activeFilter === 'drift' ? ' active' : ''}`}
                  data-testid="secrets-drift-chip"
                  title={`Pending wake / sync: ${laggingSecretsBoards.map((d) => d.name ?? d.device_id).join(', ')}`}
                  onClick={() => onFilterChange?.('drift')}
                >
                  [! {laggingSecretsBoards.length} on older sec_rev ▸]
                </button>
              </>
            )}
            {' · '}
          </span>
        )}

        <button
          type="button"
          className="flag-pill"
          data-testid="toggle-release-matrix"
          aria-expanded={matrixOpen}
          onClick={() => setMatrixOpen((prev) => !prev)}
        >
          {matrixOpen ? '▲ Hide Release & Cargo Matrix' : '▼ Expand Release & Cargo Matrix'}
        </button>
      </div>

      {/* H6 On-Demand Slide-Down Artifact × Ring × ff_sec Matrix Drawer */}
      {matrixOpen && (
        <div className="matrix-drawer" data-testid="release-matrix-drawer">
          <table>
            <thead>
              <tr>
                <th scope="col">Artifact / Cargo</th>
                <th scope="col">Target / Baseline</th>
                {stages.map((s) => (
                  <th key={s.key} scope="col">
                    {s.label} ({s.total})
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {targetSummaries.map((t) => (
                <tr key={`app:${t.target}`} data-testid="matrix-row-app">
                  <td>
                    <code>[app]</code> {t.baseline ?? '—'}
                  </td>
                  <td>
                    <code>{t.target}</code>
                  </td>
                  {stages.map((s) => {
                    const inStage = s.devices.filter((d) => d.platform_type === t.target)
                    const conv = inStage.filter((d) =>
                      isDeviceConvergedForTarget(d, baselinesByTarget),
                    ).length
                    return (
                      <td key={s.key}>
                        {inStage.length === 0
                          ? '—'
                          : `${conv}/${inStage.length} ${conv === inStage.length ? '✓' : '! drift'}`}
                      </td>
                    )
                  })}
                </tr>
              ))}
              {cargoSummaries.map((c) => (
                <tr key={`cargo:${c.key}`} data-testid="matrix-row-cargo">
                  <td>
                    <code>[{c.key}]</code> {c.topVer}
                  </td>
                  <td>companion cargo</td>
                  {stages.map((s) => {
                    const withCargo = s.devices.filter(
                      (d) => d.sub_versions && d.sub_versions[c.key] !== undefined,
                    )
                    const matching = withCargo.filter(
                      (d) => d.sub_versions?.[c.key] === c.topVer,
                    ).length
                    return (
                      <td key={s.key}>
                        {withCargo.length === 0
                          ? '—'
                          : `${matching}/${withCargo.length} ${matching === withCargo.length ? '✓' : '! skew'}`}
                      </td>
                    )
                  })}
                </tr>
              ))}
              <tr data-testid="matrix-row-secrets">
                <td>
                  <code>[ff_sec]</code> Secrets rev {maxSecretsRev}
                </td>
                <td>NVS AES-256-GCM</td>
                {stages.map((s) => {
                  const synced =
                    maxSecretsRev === 0
                      ? s.total
                      : s.devices.filter((d) => d.secrets_rev === maxSecretsRev).length
                  return (
                    <td key={s.key}>
                      {s.total === 0
                        ? '—'
                        : `${synced}/${s.total} ${synced === s.total ? '✓' : `! ${s.total - synced} pending`}`}
                    </td>
                  )
                })}
              </tr>
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
