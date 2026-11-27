// Quick-filter pills, search/cohort group-by bar, and deploy KPI strip for `FleetView`.

import type { DeployKpisSummary, DeviceSummary } from './api'
import { isDeviceConvergedForTarget } from './ReleaseConvergenceRibbon'

export type QuickFilter = 'all' | 'attention' | 'updating' | 'drift' | 'rssi' | 'offline'
export type FleetGroupBy = 'none' | 'ring' | 'parent'

export function computeKpisFromDevices(devices: DeviceSummary[]): DeployKpisSummary | null {
  let confirmed = 0
  let rolledBack = 0
  let failed = 0
  let cancelled = 0
  let inFlight = 0

  for (const d of devices) {
    if (!d.deploy) continue
    if (!d.deploy.is_terminal) {
      inFlight += 1
      continue
    }
    if (d.deploy.state === 'cancelled') {
      cancelled += 1
    } else if (d.deploy.state === 'confirmed') {
      confirmed += 1
    } else if (d.deploy.state === 'rolled_back') {
      rolledBack += 1
    } else {
      failed += 1
    }
  }

  const total = confirmed + rolledBack + failed
  if (total === 0) return null
  const deliveryPct = Math.round((confirmed / total) * 1000) / 10
  const safetyPct = Math.round(((confirmed + rolledBack) / total) * 1000) / 10
  const gapPct = Math.round((rolledBack / total) * 1000) / 10
  return {
    total_completed: total,
    confirmed,
    rolled_back: rolledBack,
    failed,
    cancelled,
    in_flight: inFlight,
    delivery_success_pct: deliveryPct,
    fleet_safety_pct: safetyPct,
    safety_gap_pct: gapPct,
    delivery_target_pct: 90,
    safety_target_pct: 100,
    delivery_target_met: confirmed * 100 >= total * 90,
    safety_target_met: confirmed + rolledBack === total,
  }
}

export function matchesQuickFilter(
  d: DeviceSummary,
  f: QuickFilter,
  baselinesByTarget: Map<string, string | null>,
  maxSecretsRev: number,
): boolean {
  const safeMode = Boolean(d.safe_mode)
  if (f === 'attention') {
    return (
      safeMode ||
      d.deploy?.state === 'rolled_back' ||
      d.deploy?.state === 'failed' ||
      d.health?.boot_ok === false
    )
  }
  if (f === 'updating') {
    return d.deploy != null && !d.deploy.is_terminal
  }
  if (f === 'drift') {
    const fwDrifted = !isDeviceConvergedForTarget(d, baselinesByTarget)
    const secDrifted =
      maxSecretsRev > 0 && d.secrets_rev != null && d.secrets_rev < maxSecretsRev
    return fwDrifted || secDrifted
  }
  if (f === 'rssi') {
    return d.health?.rssi != null && d.health.rssi < -75
  }
  if (f === 'offline') {
    return !d.online
  }
  return true
}

export function DeployKpiStrip({
  kpis,
  onFilterRollback,
}: {
  kpis: DeployKpisSummary | null
  onFilterRollback?: () => void
}) {
  if (kpis === null || kpis.total_completed === 0) return null
  const deliveryMet = kpis.delivery_target_met ?? false
  const safetyMet = kpis.safety_target_met ?? false
  return (
    <div className="kpi-strip muted" data-testid="deploy-kpis">
      <span data-testid="kpi-delivery">
        Delivery success:{' '}
        <strong className={deliveryMet ? 'ok' : 'warn'}>
          {kpis.delivery_success_pct?.toFixed(1)}%
        </strong>{' '}
        ({kpis.confirmed}/{kpis.total_completed} confirmed · target &ge;{kpis.delivery_target_pct}%)
      </span>
      {' · '}
      <span data-testid="kpi-safety">
        Fleet safety:{' '}
        <strong className={safetyMet ? 'ok' : 'bad'}>
          {kpis.fleet_safety_pct?.toFixed(1)}%
        </strong>{' '}
        ({kpis.confirmed + kpis.rolled_back}/{kpis.total_completed} safe · target{' '}
        {kpis.safety_target_pct}%)
      </span>
      {kpis.rolled_back > 0 && (
        <>
          {' · '}
          <span
            data-testid="kpi-rollback-saves"
            onClick={onFilterRollback}
            style={onFilterRollback ? { cursor: 'pointer' } : undefined}
          >
            Rollback saves: <strong>{kpis.rolled_back}</strong> (
            {kpis.safety_gap_pct?.toFixed(1)}% caught &amp; returned to previous slot)
          </span>
        </>
      )}
    </div>
  )
}

export function FleetFilterBar({
  totalCount,
  quickFilter,
  searchQuery,
  groupBy,
  freezeSortOnHover,
  onSetQuickFilter,
  onSetSearchQuery,
  onSetGroupBy,
  onSetFreezeSortOnHover,
}: {
  totalCount: number
  quickFilter: QuickFilter
  searchQuery: string
  groupBy: FleetGroupBy
  freezeSortOnHover: boolean
  onSetQuickFilter: (filter: QuickFilter) => void
  onSetSearchQuery: (query: string) => void
  onSetGroupBy: (groupBy: FleetGroupBy) => void
  onSetFreezeSortOnHover: (freeze: boolean) => void
}) {
  return (
    <div className="fleet-filter-bar" data-testid="fleet-filter-bar">
      <div className="fleet-quick-filters" data-testid="fleet-quick-filters">
        <button
          type="button"
          className={`flag-pill${quickFilter === 'all' ? ' active' : ''}`}
          aria-pressed={quickFilter === 'all'}
          data-testid="filter-all"
          onClick={() => onSetQuickFilter('all')}
        >
          All ({totalCount})
        </button>{' '}
        <button
          type="button"
          className={`flag-pill${quickFilter === 'attention' ? ' active' : ''}`}
          aria-pressed={quickFilter === 'attention'}
          data-testid="filter-attention"
          onClick={() => onSetQuickFilter('attention')}
        >
          ! Attention / Rolled Back
        </button>{' '}
        <button
          type="button"
          className={`flag-pill${quickFilter === 'updating' ? ' active' : ''}`}
          aria-pressed={quickFilter === 'updating'}
          data-testid="filter-updating"
          onClick={() => onSetQuickFilter('updating')}
        >
          ↺ Updating
        </button>{' '}
        <button
          type="button"
          className={`flag-pill${quickFilter === 'drift' ? ' active' : ''}`}
          aria-pressed={quickFilter === 'drift'}
          data-testid="filter-drift"
          onClick={() => onSetQuickFilter('drift')}
        >
          ≠ Drift
        </button>{' '}
        <button
          type="button"
          className={`flag-pill${quickFilter === 'rssi' ? ' active' : ''}`}
          aria-pressed={quickFilter === 'rssi'}
          data-testid="filter-weak-rssi"
          onClick={() => onSetQuickFilter('rssi')}
        >
          ! Weak RSSI
        </button>{' '}
        <button
          type="button"
          className={`flag-pill${quickFilter === 'offline' ? ' active' : ''}`}
          aria-pressed={quickFilter === 'offline'}
          data-testid="filter-offline"
          onClick={() => onSetQuickFilter('offline')}
        >
          ○ Offline
        </button>
      </div>

      <div className="fleet-filter-controls">
        <input
          type="search"
          data-testid="fleet-search-input"
          aria-label="Search fleet"
          placeholder="Search boards, chip, SSID, version…"
          value={searchQuery}
          onChange={(e) => onSetSearchQuery(e.target.value)}
        />{' '}
        <span className="fleet-group-by" data-testid="fleet-group-by">
          <span className="muted">Group by: </span>
          <button
            type="button"
            className={`flag-pill${groupBy === 'none' ? ' active' : ''}`}
            aria-pressed={groupBy === 'none'}
            onClick={() => onSetGroupBy('none')}
          >
            None
          </button>{' '}
          <button
            type="button"
            className={`flag-pill${groupBy === 'ring' ? ' active' : ''}`}
            aria-pressed={groupBy === 'ring'}
            onClick={() => onSetGroupBy('ring')}
          >
            Ring
          </button>{' '}
          <button
            type="button"
            className={`flag-pill${groupBy === 'parent' ? ' active' : ''}`}
            aria-pressed={groupBy === 'parent'}
            onClick={() => onSetGroupBy('parent')}
          >
            Parent
          </button>
        </span>{' '}
        <label className="muted">
          <input
            type="checkbox"
            data-testid="freeze-sort-toggle"
            checked={freezeSortOnHover}
            onChange={(e) => onSetFreezeSortOnHover(e.target.checked)}
          />{' '}
          Freeze sort on hover
        </label>
      </div>
    </div>
  )
}
