// The fleet workbench table — R0's "watch it come online".
//
// Renders `GET /v1/devices` refreshed by `useFleet` and `useArtifacts` without adding
// extra mount fetches, deriving presence locally, or reading SSE bodies directly.

import { useRef, useState } from 'react'
import {
  type DeployKpisSummary,
  type DeviceGroupSummary,
  type DeviceSummary,
} from './api'
import { useArtifacts, type Artifacts } from './deploy'
import { useFleet, type EventSourceFactory, type Fleet } from './fleet'
import { ArrivalRow, FleetDeviceRow } from './FleetDeviceRow'
import {
  DeployKpiStrip,
  FleetFilterBar,
  computeKpisFromDevices,
  matchesQuickFilter,
  type FleetGroupBy,
  type QuickFilter,
} from './FleetFilterBar'
import {
  ReleaseConvergenceRibbon,
  targetBaselineVersion,
} from './ReleaseConvergenceRibbon'
import { type VersionLine } from './statusStrip'

export type { FleetGroupBy, QuickFilter }

export function FleetView({
  onSessionExpired,
  createEventSource,
}: {
  onSessionExpired: () => void
  createEventSource?: EventSourceFactory
}) {
  const fleet = useFleet({ onSessionExpired, createEventSource })
  const artifacts = useArtifacts({ onSessionExpired })
  return <FleetTable fleet={fleet} artifacts={artifacts} onSessionExpired={onSessionExpired} />
}

export function FleetTable({
  fleet,
  artifacts,
  onSessionExpired,
  selectedDeviceId = null,
  onSelect,
  versions = null,
  onGoToOnboard,
  onGoToBuilds,
  kpis = null,
  groups,
  onOpenCompare,
}: {
  fleet: Fleet
  artifacts: Artifacts
  onSessionExpired: () => void
  selectedDeviceId?: string | null
  onSelect?: (deviceId: string) => void
  versions?: VersionLine | null
  onGoToOnboard?: () => void
  onGoToBuilds?: () => void
  kpis?: DeployKpisSummary | null
  groups?: DeviceGroupSummary[]
  onOpenCompare?: () => void
}) {
  const { devices, arrivals, error, stream, now, refresh } = fleet
  const effectiveKpis = kpis ?? (devices !== null ? computeKpisFromDevices(devices) : null)

  const [quickFilter, setQuickFilter] = useState<QuickFilter>('all')
  const [searchQuery, setSearchQuery] = useState('')
  const [groupBy, setGroupBy] = useState<FleetGroupBy>('none')
  const [freezeSortOnHover, setFreezeSortOnHover] = useState(true)
  const [hoveringTable, setHoveringTable] = useState(false)
  const frozenOrderRef = useRef<string[] | null>(null)

  const groupNameById = new Map<string, string>()
  for (const g of groups ?? []) {
    groupNameById.set(g.id, g.name)
  }

  const baselinesByTarget = new Map<string, string | null>()
  if (devices !== null) {
    const byTarget = new Map<string, DeviceSummary[]>()
    for (const d of devices) {
      const list = byTarget.get(d.platform_type)
      if (list) list.push(d)
      else byTarget.set(d.platform_type, [d])
    }
    for (const [target, list] of byTarget.entries()) {
      baselinesByTarget.set(target, targetBaselineVersion(target, list, artifacts))
    }
  }

  const maxSecretsRev =
    devices !== null
      ? Math.max(
          0,
          ...devices
            .map((d) => d.secrets_rev)
            .filter((r): r is number => typeof r === 'number'),
        )
      : 0

  const filteredDevices: DeviceSummary[] =
    devices === null
      ? []
      : devices.filter((d) => {
          if (!matchesQuickFilter(d, quickFilter, baselinesByTarget, maxSecretsRev)) return false
          const q = searchQuery.trim().toLowerCase()
          if (q === '') return true
          const haystack = [
            d.device_id,
            d.name ?? '',
            d.platform_type,
            d.fw_version ?? '',
            d.ssid ?? '',
            d.deploy?.state ?? '',
            d.deploy?.detail ?? '',
            d.deploy?.selftest?.reason ?? '',
            d.parent_device_id ?? '',
          ]
            .join(' ')
            .toLowerCase()
          return haystack.includes(q)
        })

  let displayedDevices = [...filteredDevices]
  if (groupBy === 'ring') {
    displayedDevices.sort((a, b) => {
      const ga = a.group_id ? (groupNameById.get(a.group_id) ?? a.group_id) : 'zz-ungrouped'
      const gb = b.group_id ? (groupNameById.get(b.group_id) ?? b.group_id) : 'zz-ungrouped'
      return ga.localeCompare(gb)
    })
  } else if (groupBy === 'parent') {
    displayedDevices.sort((a, b) => {
      const pa = a.parent_device_id ?? 'zz-direct'
      const pb = b.parent_device_id ?? 'zz-direct'
      return pa.localeCompare(pb)
    })
  }

  if (freezeSortOnHover && hoveringTable && frozenOrderRef.current !== null) {
    const orderMap = new Map(frozenOrderRef.current.map((id, i) => [id, i]))
    displayedDevices = [...displayedDevices].sort(
      (a, b) => (orderMap.get(a.device_id) ?? 9999) - (orderMap.get(b.device_id) ?? 9999),
    )
  } else {
    frozenOrderRef.current = displayedDevices.map((d) => d.device_id)
  }

  return (
    <section className="console-card" aria-labelledby="fleet-heading">
      <div className="section-header">
        <div className="section-title-group">
          <h2 id="fleet-heading">Fleet</h2>
          <p className="muted stream-pill" data-testid="stream-status">
            {stream === 'live' ? (
              <span className="ok">Live</span>
            ) : stream === 'connecting' ? (
              'Connecting…'
            ) : stream === 'reconnecting' ? (
              'Reconnecting…'
            ) : (
              'Not receiving updates'
            )}
          </p>
        </div>
        {(onGoToOnboard || onGoToBuilds) && (
          <div className="section-actions">
            {onGoToOnboard && (
              <button type="button" onClick={onGoToOnboard}>
                + Flash / Add board
              </button>
            )}
            {onGoToBuilds && (
              <button type="button" onClick={onGoToBuilds}>
                + Upload build
              </button>
            )}
          </div>
        )}
      </div>

      {error !== null && (
        <p className="bad" role="alert">
          {error} — showing the last state that loaded.
        </p>
      )}

      {artifacts.error !== null && (
        <p className="bad" data-testid="artifacts-error">
          {artifacts.error} — the versions to deploy could not be read.
        </p>
      )}

      {arrivals.length > 0 && (
        <div data-testid="arrivals">
          <h3>Arriving</h3>
          <p className="muted">
            Boards that have reported a boot stage and are not in the fleet yet. A board with no
            route to the server cannot report at all, so an empty list here is never proof that
            nothing else is booting.
          </p>
          <ul>
            {arrivals.map((arrival) => (
              <ArrivalRow key={arrival.device_id} arrival={arrival} now={now} />
            ))}
          </ul>
        </div>
      )}

      {devices === null ? (
        <p aria-busy="true">Loading…</p>
      ) : devices.length === 0 ? (
        <div className="empty-fleet">
          <p className="muted">
            {arrivals.length > 0
              ? 'No boards have finished enrolling yet.'
              : 'No boards yet. Generate an enrollment token below and flash a board.'}
          </p>
          {(onGoToOnboard || onGoToBuilds) && arrivals.length === 0 && (
            <div className="empty-onboarding" data-testid="empty-onboarding">
              <div className="onboarding-card">
                <h3>1 · Add your first board</h3>
                <p className="muted">
                  Plug an ESP32 into USB to detect its chip, bake in Wi-Fi credentials, and write
                  the Fleetforge agent directly from Chrome/Edge — or mint a CLI token.
                </p>
                {onGoToOnboard && (
                  <button type="button" onClick={onGoToOnboard}>
                    Flash or enroll a board &gt;
                  </button>
                )}
              </div>
              <div className="onboarding-card">
                <h3>2 · Upload firmware for OTA</h3>
                <p className="muted">
                  Upload your compiled ESP-IDF app <code>.bin</code> under Builds &amp; Profiles so
                  every matching board offers one-click OTA updates in the Deploy column.
                </p>
                {onGoToBuilds && (
                  <button type="button" onClick={onGoToBuilds}>
                    Upload firmware .bin &gt;
                  </button>
                )}
              </div>
            </div>
          )}
        </div>
      ) : (
        <>
          <ReleaseConvergenceRibbon
            devices={devices}
            artifacts={artifacts}
            groups={groups}
            activeFilter={quickFilter}
            onFilterChange={(f) => setQuickFilter(f as QuickFilter)}
            onOpenCompare={onOpenCompare}
            kpis={effectiveKpis}
          />
          <DeployKpiStrip
            kpis={effectiveKpis}
            onFilterRollback={
              effectiveKpis && effectiveKpis.rolled_back > 0
                ? () => setQuickFilter('attention')
                : undefined
            }
          />

          <FleetFilterBar
            totalCount={devices.length}
            quickFilter={quickFilter}
            searchQuery={searchQuery}
            groupBy={groupBy}
            freezeSortOnHover={freezeSortOnHover}
            onSetQuickFilter={setQuickFilter}
            onSetSearchQuery={setSearchQuery}
            onSetGroupBy={setGroupBy}
            onSetFreezeSortOnHover={setFreezeSortOnHover}
          />

          <table>
            <thead>
              <tr>
                <th scope="col">Status</th>
                <th scope="col">Device</th>
                <th scope="col">Platform</th>
                <th scope="col">Firmware</th>
                <th scope="col">Last seen</th>
                <th scope="col">Health</th>
                <th scope="col">Power</th>
                <th scope="col">Deploy</th>
              </tr>
            </thead>
            <tbody
              onMouseEnter={() => setHoveringTable(true)}
              onMouseLeave={() => setHoveringTable(false)}
            >
              {displayedDevices.map((device) => {
                const groupLabel =
                  groupBy === 'ring'
                    ? `Ring: ${device.group_id ? (groupNameById.get(device.group_id) ?? device.group_id) : 'ungrouped'}`
                    : groupBy === 'parent'
                      ? `Parent: ${device.parent_device_id ?? 'direct'}`
                      : null
                return (
                  <FleetDeviceRow
                    key={device.device_id}
                    device={device}
                    now={now}
                    artifacts={artifacts.byTarget.get(device.platform_type) ?? []}
                    artifactsLoaded={artifacts.loaded}
                    onDeployed={refresh}
                    onSessionExpired={onSessionExpired}
                    selected={device.device_id === selectedDeviceId}
                    onSelect={onSelect}
                    versions={versions}
                    onGoToBuilds={onGoToBuilds}
                    groups={groups}
                    groupByLabel={groupLabel}
                  />
                )
              })}
            </tbody>
          </table>
        </>
      )}
    </section>
  )
}
