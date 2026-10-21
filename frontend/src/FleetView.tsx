// The fleet — R0's "watch it come online".
//
// A table of what `GET /v1/devices` says, refreshed by `useFleet` (which holds every
// rule about *when* to re-read). This file renders and nothing else: it never derives
// presence, and it never reads an SSE payload. `online` is the server's answer.
//
// The status line above the table matters more than it looks. An operator must be able
// to tell "the fleet is quiet" from "this page stopped updating" — that distinction is
// the entire reason the backend sends keepalives.
//
// The Status cell also carries the board's last announced network (R2b-fe-13). It says
// "last on" for an offline board because the server cannot tell out of range from powered
// off; no cause is ever claimed here.
//
// "Arriving" above the table is the same idea one step earlier (S0-fw-1): a board between
// "flashed" and "online" used to render as nothing at all, which is indistinguishable
// from a board that was never flashed. It is a separate list rather than a table row
// because an arriving board has no fleet row yet — most columns would be em-dashes and
// the `Status` one would be a lie.
//
// The Health cell (R4-fe-1) shows what the server stored about the board's current boot:
// the last reset reason, boot confirmation, the server's own fault count, the newest
// heartbeat's heap, uptime and signal, and the latest metrics. It renders server answers
// only (fresh data arrives through `useFleet`'s re-read, never from an SSE body). A fault
// reset is a boxed bold word, not a tint: the theme is monochrome and state is never
// carried by colour alone.

import { ApiError, api, type ArrivalSummary, type ArtifactSummary, type DeviceSummary } from './api'
import { DeployCell } from './DeployCell'
import { useArtifacts, type Artifacts } from './deploy'
import { STAGE_LABELS, useFleet, type Fleet, type EventSourceFactory } from './fleet'
import {
  METRICS_INLINE,
  describeBoot,
  describeFaults,
  describeReset,
  describeVitals,
  formatMetricValue,
} from './deviceHealth'
import { formatAgo, formatWhen } from './format'
import { LAST_ON_TITLE, knowsNetworks, networkPlace } from './network'
import { type VersionLine } from './statusStrip'

// The glyph differs by state, and that is a requirement rather than a flourish (S0-fe-2).
// The theme is monochrome — one hue ramp — so green-vs-grey no longer says anything, and
// this is the only place in the app where presence was carried by colour alone (every
// other state renders its own word: "active"/"used", "Live", a full sentence). Filled vs
// stippled block, plus the weight and lightness `.ok`/`.muted` carry, means the online
// row is still tellable from the offline one in a greyscale screenshot. WCAG 1.4.1.
// `aria-label` is unchanged: a screen reader always heard the word, never the glyph.
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
  const knows = knowsNetworks(device.known_networks)
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

function HealthCell({ device }: { device: DeviceSummary }) {
  const reset = describeReset(device.last_reset_reason)
  const boot = describeBoot(device.health)
  const faults = describeFaults(device.fault_resets)
  const vitals = describeVitals(device.health, device.online, formatWhen)
  const metrics = device.metrics ?? []
  const inline = metrics.slice(0, METRICS_INLINE)
  const more = metrics.slice(METRICS_INLINE)

  if (reset === null && boot === null && faults === null && vitals === null && metrics.length === 0) {
    return (
      <td data-testid="health-cell" className="muted">
        —
      </td>
    )
  }

  const metric = (m: (typeof metrics)[number]) => (
    <span key={m.name} data-testid="metric" data-name={m.name} title={formatWhen(m.at)}>
      {/* Device-controlled name: React text only. */}
      {m.name} {formatMetricValue(m.value)}{' '}
    </span>
  )

  return (
    <td data-testid="health-cell">
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
      {vitals !== null && (
        <div
          data-testid="device-vitals"
          className={vitals.stale ? 'muted' : undefined}
          title={vitals.title}
        >
          {vitals.stale && 'last reported '}
          {vitals.parts.join(' · ')}
        </div>
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

function DeviceRow({
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
}) {
  // Why a sleepy board went offline is its wake interval, so put it where the pointer is.
  const power =
    device.power_class === 'sleepy' && device.expected_wake_interval_s !== null
      ? `wakes every ${device.expected_wake_interval_s} s`
      : device.power_class

  const identity = device.name ?? <code>{device.device_id}</code>

  async function toggleFlag(patch: { is_dev?: boolean; stay_awake?: boolean }) {
    try {
      await api.updateDevice(device.device_id, patch)
      onDeployed()
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
      }
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
      </td>
      <td>
        {/* The marker is a glyph, not a colour: the theme is monochrome. */}
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
          </div>
        )}
      </td>
      <td>{device.platform_type}</td>
      <td>{device.fw_version ?? '—'}</td>
      <td title={device.last_seen === null ? undefined : formatWhen(device.last_seen)}>
        {formatAgo(device.last_seen, now)}
      </td>
      <HealthCell device={device} />
      <td title={power}>{device.power_class}</td>
      <DeployCell
        device={device}
        artifacts={artifacts}
        artifactsLoaded={artifactsLoaded}
        // A deploy makes its row the strip's board: "after a deploy, one glance".
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

function ArrivalRow({ arrival, now }: { arrival: ArrivalSummary; now: number }) {
  return (
    <li data-testid="arrival-row" data-device-id={arrival.device_id}>
      <code>{arrival.device_id}</code> — {STAGE_LABELS[arrival.stage] ?? arrival.stage}
      {/* The stalled marker is a WORD, not a colour: `bad` only tints what is already
          readable, so an operator who cannot see the tint still reads "stalled". */}
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
          {/* Device-controlled text. React escapes it and nothing here interprets it. */}
          <span className="muted">{arrival.detail}</span>
        </>
      )}
    </li>
  )
}

// One read of the artifact list for the whole table, not one per row. It is read once on
// mount and never polled — see `deploy.ts`.
export function FleetView({
  onSessionExpired,
  createEventSource,
}: {
  onSessionExpired: () => void
  // Injected by the tests only: jsdom has no `EventSource` (see `fleet.ts`).
  createEventSource?: EventSourceFactory
}) {
  const fleet = useFleet({ onSessionExpired, createEventSource })
  const artifacts = useArtifacts({ onSessionExpired })
  return <FleetTable fleet={fleet} artifacts={artifacts} onSessionExpired={onSessionExpired} />
}

// The table without its own `useFleet`: the signed-in page opens ONE fleet hook (one
// EventSource, one of the API's `sse_max_clients` slots) and shares it with the status
// strip (R2b-fe-1). `onSelect` is passed only by that page; without it the rows render
// exactly as before.
//
// Both hooks are read by the caller, fleet first: effects of a child run before its
// parent's, and the devices read has always been the first request this table makes.
export function FleetTable({
  fleet,
  artifacts,
  onSessionExpired,
  selectedDeviceId = null,
  onSelect,
  versions = null,
  onGoToOnboard,
  onGoToBuilds,
}: {
  fleet: Fleet
  artifacts: Artifacts
  onSessionExpired: () => void
  selectedDeviceId?: string | null
  onSelect?: (deviceId: string) => void
  // The update result card's UI / API row (R2b-fe-10); the standalone `FleetView` has none.
  versions?: VersionLine | null
  onGoToOnboard?: () => void
  onGoToBuilds?: () => void
}) {
  const { devices, arrivals, error, stream, now, refresh } = fleet

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

      {/* A broken artifact list costs the Deploy column and nothing else, so it is said
          here rather than allowed to blank the table — and it is said, rather than
          letting every cell claim that nothing has been uploaded. */}
      {artifacts.error !== null && (
        <p className="bad" data-testid="artifacts-error">
          {artifacts.error} — the versions to deploy could not be read.
        </p>
      )}

      {arrivals.length > 0 && (
        <div data-testid="arrivals">
          <h3>Arriving</h3>
          <p className="muted">
            Boards that have reported a boot stage and are not in the fleet yet. A board with
            no route to the server cannot report at all, so an empty list here is never proof
            that nothing else is booting.
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
          <tbody>
            {devices.map((device) => (
              <DeviceRow
                key={device.device_id}
                device={device}
                now={now}
                // Filtered by the board's own chip: the server refuses a mismatch
                // (`_check_compatible`), so offering one would be offering a 409.
                artifacts={artifacts.byTarget.get(device.platform_type) ?? []}
                artifactsLoaded={artifacts.loaded}
                onDeployed={refresh}
                onSessionExpired={onSessionExpired}
                selected={device.device_id === selectedDeviceId}
                onSelect={onSelect}
                versions={versions}
                onGoToBuilds={onGoToBuilds}
              />
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
