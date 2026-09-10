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
// "Arriving" above the table is the same idea one step earlier (S0-fw-1): a board between
// "flashed" and "online" used to render as nothing at all, which is indistinguishable
// from a board that was never flashed. It is a separate list rather than a table row
// because an arriving board has no fleet row yet — most columns would be em-dashes and
// the `Status` one would be a lie.

import { type ArrivalSummary, type DeviceSummary } from './api'
import { useFleet, type EventSourceFactory } from './fleet'
import { formatAgo, formatWhen } from './format'

function StatusCell({ device }: { device: DeviceSummary }) {
  const label = device.online ? 'online' : 'offline'
  return (
    <span className={device.online ? 'ok' : 'muted'} aria-label={label}>
      <span aria-hidden="true">● </span>
      {label}
    </span>
  )
}

function DeviceRow({ device, now }: { device: DeviceSummary; now: number }) {
  // Why a sleepy board went offline is its wake interval, so put it where the pointer is.
  const power =
    device.power_class === 'sleepy' && device.expected_wake_interval_s !== null
      ? `wakes every ${device.expected_wake_interval_s} s`
      : device.power_class

  return (
    <tr data-testid="device-row" data-device-id={device.device_id}>
      <td>
        <StatusCell device={device} />
      </td>
      <td>
        {device.name ?? <code>{device.device_id}</code>}
        {device.name !== null && (
          <>
            <br />
            <code className="muted">{device.device_id}</code>
          </>
        )}
      </td>
      <td>{device.platform_type}</td>
      <td>{device.fw_version ?? '—'}</td>
      <td title={device.last_seen === null ? undefined : formatWhen(device.last_seen)}>
        {formatAgo(device.last_seen, now)}
      </td>
      <td title={power}>{device.power_class}</td>
    </tr>
  )
}

// A lookup with a fallback, deliberately NOT a switch: `stage` is device-controlled and
// the server whitelists no vocabulary, so an agent newer than this dashboard must render
// its stage as itself rather than vanish from the list.
const STAGE_LABELS: Record<string, string> = {
  link_up: 'network up',
  time_synced: 'clock set',
  enrolling: 'enrolling',
  enrolled: 'enrolled',
  mqtt_connected: 'connecting to the broker',
  mqtt_refused: 'the broker refused its credential',
  halted: 'stopped',
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

export function FleetView({
  onSessionExpired,
  createEventSource,
}: {
  onSessionExpired: () => void
  // Injected by the tests only: jsdom has no `EventSource` (see `fleet.ts`).
  createEventSource?: EventSourceFactory
}) {
  const { devices, arrivals, error, stream, now } = useFleet({
    onSessionExpired,
    createEventSource,
  })

  return (
    <section aria-labelledby="fleet-heading">
      <h2 id="fleet-heading">Fleet</h2>

      <p className="muted" data-testid="stream-status">
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

      {error !== null && (
        <p className="bad" role="alert">
          {error} — showing the last state that loaded.
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
        <p className="muted">
          {arrivals.length > 0
            ? 'No boards have finished enrolling yet.'
            : 'No boards yet. Generate an enrollment token below and flash a board.'}
        </p>
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">Status</th>
              <th scope="col">Device</th>
              <th scope="col">Platform</th>
              <th scope="col">Firmware</th>
              <th scope="col">Last seen</th>
              <th scope="col">Power</th>
            </tr>
          </thead>
          <tbody>
            {devices.map((device) => (
              <DeviceRow key={device.device_id} device={device} now={now} />
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
