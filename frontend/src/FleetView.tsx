// The fleet — R0's "watch it come online".
//
// A table of what `GET /v1/devices` says, refreshed by `useFleet` (which holds every
// rule about *when* to re-read). This file renders and nothing else: it never derives
// presence, and it never reads an SSE payload. `online` is the server's answer.
//
// The status line above the table matters more than it looks. An operator must be able
// to tell "the fleet is quiet" from "this page stopped updating" — that distinction is
// the entire reason the backend sends keepalives.

import { type DeviceSummary } from './api'
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

export function FleetView({
  onSessionExpired,
  createEventSource,
}: {
  onSessionExpired: () => void
  // Injected by the tests only: jsdom has no `EventSource` (see `fleet.ts`).
  createEventSource?: EventSourceFactory
}) {
  const { devices, error, stream, now } = useFleet({ onSessionExpired, createEventSource })

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

      {devices === null ? (
        <p aria-busy="true">Loading…</p>
      ) : devices.length === 0 ? (
        <p className="muted">No boards yet. Generate an enrollment token below and flash a board.</p>
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
