// Individual device row, arrival item, and monochrome health/status cells for `FleetView`.

import { useState } from 'react'
import {
  ApiError,
  api,
  type ArrivalSummary,
  type ArtifactSummary,
  type DeviceGroupSummary,
  type DeviceSummary,
} from './api'
import { DeployCell } from './DeployCell'
import {
  METRICS_INLINE,
  describeBoot,
  describeFaults,
  describeReset,
  describeVitals,
  formatMetricValue,
} from './deviceHealth'
import { STAGE_LABELS } from './fleet'
import { formatAgo, formatBytes, formatWhen } from './format'
import { LAST_ON_TITLE, knowsNetworks, networkPlace } from './network'
import { type VersionLine } from './statusStrip'

function StatusCell({ device }: { device: DeviceSummary }) {
  const label = device.online ? 'online' : 'offline'
  return (
    <span className={device.online ? 'ok' : 'muted'} aria-label={label}>
      <span aria-hidden="true">{device.online ? '█' : '░'} </span>
      {label}
    </span>
  )
}

function NetworkNote({ device }: { device: DeviceSummary }) {
  const place = networkPlace(device)
  const knows = knowsNetworks(device.known_networks ?? null)
  if (place === null && knows === null) return null
  return (
    <>
      {place !== null && (
        <span data-testid="device-network" title={place.current ? undefined : LAST_ON_TITLE}>
          , {place.label} <bdi>{place.ssid}</bdi>
        </span>
      )}
      {knows !== null && (
        <>
          <br />
          <span className="muted" data-testid="device-networks-known">
            {knows}
          </span>
        </>
      )}
    </>
  )
}

function PresenceBadges({ device }: { device: DeviceSummary }) {
  const isDock = device.link_type === 'dock'
  if (!isDock) return null

  return (
    <div className="presence-badges" data-testid="presence-badges">
      <span className="badge" data-testid="presence-dock">
        █ DOCK{device.parent_device_id ? ` (@${device.parent_device_id})` : ''}
      </span>
    </div>
  )
}

function HealthCell({ device }: { device: DeviceSummary }) {
  const safeMode = Boolean(device.safe_mode)
  const reset = describeReset(device.last_reset_reason)
  const boot = describeBoot(device.health)
  const faults = describeFaults(device.fault_resets)
  const vitals = describeVitals(device.health, device.online, formatWhen)
  const freeHeap = device.health?.free_heap ?? null
  const metrics = device.metrics ?? []
  const inline = metrics.slice(0, METRICS_INLINE)
  const more = metrics.slice(METRICS_INLINE)

  if (
    !safeMode &&
    reset === null &&
    boot === null &&
    faults === null &&
    vitals === null &&
    freeHeap === null &&
    metrics.length === 0
  ) {
    return (
      <td data-testid="health-cell" className="muted">
        —
      </td>
    )
  }

  const metric = (m: (typeof metrics)[number]) => (
    <span key={m.name} data-testid="metric" data-name={m.name} title={formatWhen(m.at)}>
      {m.name} {formatMetricValue(m.value)}{' '}
    </span>
  )

  return (
    <td data-testid="health-cell">
      {safeMode && (
        <div>
          <strong className="bad emcon-badge badge" data-testid="safe-mode-badge">
            !! SAFE MODE
          </strong>
        </div>
      )}
      {reset !== null &&
        (reset.fault ? (
          <div>
            <strong
              className="warn badge"
              data-testid="reset-badge"
              data-reason={reset.reason}
              title={`The board's last reset: ${reset.label ?? reset.reason}`}
            >
              <span aria-hidden="true">! </span>
              last reset: {reset.reason}
            </strong>
          </div>
        ) : (
          <div
            className="muted"
            data-testid="reset-reason"
            title={reset.label ?? 'a reset reason this dashboard does not know'}
          >
            last reset: {reset.reason}
          </div>
        ))}
      {boot === 'unconfirmed' && (
        <div data-testid="boot-state">
          <strong
            className="bad"
            title="The running image has not confirmed yet; the board rolls back on its own if it never does."
          >
            boot not confirmed
          </strong>
        </div>
      )}
      {boot === 'ok' && (
        <div data-testid="boot-state" className="ok">
          boot ok
        </div>
      )}
      {faults !== null && (
        <div data-testid="fault-resets" className="warn">
          {faults}
        </div>
      )}
      {vitals !== null ? (
        <div
          data-testid="device-vitals"
          className={vitals.stale ? 'muted' : undefined}
          title={vitals.title}
        >
          {vitals.stale && 'last reported '}
          {vitals.parts.join(' · ')}
          {freeHeap !== null && ` · free ${formatBytes(freeHeap)}`}
        </div>
      ) : (
        freeHeap !== null && (
          <div data-testid="device-vitals" className={!device.online ? 'muted' : undefined}>
            {!device.online && 'last reported '}
            free {formatBytes(freeHeap)}
          </div>
        )
      )}
      {metrics.length > 0 && (
        <div data-testid="metrics">
          {inline.map(metric)}
          {more.length > 0 && (
            <details>
              <summary>+{more.length} more</summary>
              {more.map(metric)}
            </details>
          )}
        </div>
      )}
    </td>
  )
}

export function FleetDeviceRow({
  device,
  now,
  artifacts,
  artifactsLoaded,
  onDeployed,
  onSessionExpired,
  selected,
  onSelect,
  versions = null,
  onGoToBuilds,
  groups,
  groupByLabel = null,
}: {
  device: DeviceSummary
  now: number
  artifacts: ArtifactSummary[]
  artifactsLoaded: boolean
  onDeployed: () => void
  onSessionExpired: () => void
  selected: boolean
  onSelect?: (deviceId: string) => void
  versions?: VersionLine | null
  onGoToBuilds?: () => void
  groups?: DeviceGroupSummary[]
  groupByLabel?: string | null
}) {
  const power =
    device.power_class === 'sleepy' && device.expected_wake_interval_s !== null
      ? `wakes every ${device.expected_wake_interval_s} s`
      : device.power_class

  const identity = device.name ?? <code>{device.device_id}</code>
  const [confirmingDecommission, setConfirmingDecommission] = useState(false)
  const [rowError, setRowError] = useState<string | null>(null)

  async function toggleFlag(patch: {
    is_dev?: boolean
    stay_awake?: boolean
    group_id?: string | null
  }) {
    setRowError(null)
    try {
      await api.updateDevice(device.device_id, patch)
      onDeployed()
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
        return
      }
      setRowError(err instanceof Error ? err.message : 'Could not update board')
    }
  }

  async function decommission() {
    setRowError(null)
    try {
      await api.decommissionDevice(device.device_id)
      setConfirmingDecommission(false)
      onDeployed()
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
        return
      }
      setRowError(err instanceof Error ? err.message : 'Could not decommission board')
    }
  }

  return (
    <tr
      data-testid="device-row"
      data-device-id={device.device_id}
      data-selected={selected ? 'true' : undefined}
    >
      <td>
        <StatusCell device={device} />
        <NetworkNote device={device} />
        <PresenceBadges device={device} />
      </td>
      <td>
        {selected && <span aria-hidden="true">▶ </span>}
        {onSelect === undefined ? (
          identity
        ) : (
          <button
            type="button"
            className="pick-board"
            aria-pressed={selected}
            title="Show this board in the status strip and its log below"
            onClick={() => onSelect(device.device_id)}
          >
            {identity}
          </button>
        )}
        {device.name !== null && (
          <>
            <br />
            <code className="muted">{device.device_id}</code>
          </>
        )}
        {groupByLabel !== null && (
          <div>
            <span className="badge muted" data-testid="device-group-by-badge">
              {groupByLabel}
            </span>
          </div>
        )}
        {onSelect !== undefined && (
          <div className="device-flags" data-testid="device-flags">
            <button
              type="button"
              className={`flag-pill${device.is_dev ? ' active' : ''}`}
              aria-pressed={Boolean(device.is_dev)}
              data-testid="toggle-dev"
              title="Mark as a development target board for continuous deployment (auto_deploy_target='dev')"
              onClick={() => void toggleFlag({ is_dev: !device.is_dev })}
            >
              {device.is_dev ? '★ dev' : 'dev'}
            </button>
            {device.power_class === 'sleepy' && (
              <>
                {' '}
                <button
                  type="button"
                  className={`flag-pill${device.stay_awake ? ' active' : ''}`}
                  aria-pressed={Boolean(device.stay_awake)}
                  data-testid="toggle-stay-awake"
                  title="Keep this sleepy board awake on MQTT via retained dn/cfg during active iteration"
                  onClick={() => void toggleFlag({ stay_awake: !device.stay_awake })}
                >
                  {device.stay_awake ? '⚡ stay awake' : 'stay awake'}
                </button>
              </>
            )}
            {device.secrets_rev !== undefined && device.secrets_rev !== null && (
              <>
                {' '}
                <span
                  className="badge muted"
                  data-testid="device-secrets-rev"
                  title={`Board synced project secrets revision ${device.secrets_rev}`}
                >
                  sec:r{device.secrets_rev}
                </span>
              </>
            )}
            {groups && groups.length > 0 && (
              <>
                {' '}
                <select
                  aria-label="Device group"
                  data-testid="device-group-select"
                  className="flag-pill"
                  value={device.group_id ?? ''}
                  onChange={(e) => void toggleFlag({ group_id: e.target.value || null })}
                  title="Assign device to a cohort group"
                >
                  <option value="">(no group)</option>
                  {groups.map((g) => (
                    <option key={g.id} value={g.id}>
                      {g.name}
                    </option>
                  ))}
                </select>
              </>
            )}{' '}
            {!confirmingDecommission ? (
              <button
                type="button"
                className="flag-pill"
                data-testid="decommission-device"
                title="Decommission this board (revokes broker ACL, cancels in-flight deploys, and removes from fleet)"
                onClick={() => {
                  setRowError(null)
                  setConfirmingDecommission(true)
                }}
              >
                decommission
              </button>
            ) : (
              <span data-testid="decommission-device-confirm">
                <button
                  type="button"
                  className="flag-pill bad"
                  data-testid="decommission-device-confirm-btn"
                  onClick={() => void decommission()}
                >
                  confirm decommission
                </button>{' '}
                <button
                  type="button"
                  className="flag-pill"
                  data-testid="decommission-device-cancel-btn"
                  onClick={() => setConfirmingDecommission(false)}
                >
                  cancel
                </button>
              </span>
            )}
            {rowError !== null && (
              <div className="bad" role="alert" data-testid="device-row-error">
                {rowError}
              </div>
            )}
          </div>
        )}
      </td>
      <td>{device.platform_type}</td>
      <td>
        {device.fw_version ?? '—'}
        {device.sub_versions && Object.keys(device.sub_versions).length > 0 && (
          <div className="muted" data-testid="device-sub-versions">
            {Object.entries(device.sub_versions).map(([k, v]) => (
              <code key={k}>
                {k}:{v}{' '}
              </code>
            ))}
          </div>
        )}
      </td>
      <td title={device.last_seen === null ? undefined : formatWhen(device.last_seen)}>
        {formatAgo(device.last_seen, now)}
      </td>
      <HealthCell device={device} />
      <td title={power}>{device.power_class}</td>
      <DeployCell
        device={device}
        artifacts={artifacts}
        artifactsLoaded={artifactsLoaded}
        onDeployed={() => {
          onDeployed()
          onSelect?.(device.device_id)
        }}
        onSessionExpired={onSessionExpired}
        now={now}
        versions={versions}
        onGoToBuilds={onGoToBuilds}
      />
    </tr>
  )
}

export function ArrivalRow({ arrival, now }: { arrival: ArrivalSummary; now: number }) {
  return (
    <li data-testid="arrival-row" data-device-id={arrival.device_id}>
      <code>{arrival.device_id}</code> — {STAGE_LABELS[arrival.stage] ?? arrival.stage}
      {arrival.stalled && (
        <>
          {' '}
          <strong className="bad">stalled</strong>
        </>
      )}{' '}
      <span className="muted" title={formatWhen(arrival.at)}>
        ({formatAgo(arrival.at, now)})
      </span>
      {arrival.detail !== null && arrival.detail !== '' && (
        <>
          <br />
          <span className="muted">{arrival.detail}</span>
        </>
      )}
    </li>
  )
}
