// The device log tail (R4-fe-2): the lines a board sent over the network (`up/log`) for the
// board the status strip shows. Render only; the read and the pure helpers are in
// `deviceLog.ts`.
//
// Every line is device-controlled text. The server has already stripped control characters,
// replaced URLs with `<url>` and cut it to 200 characters; it still renders as React text,
// inside `<bdi>`, and never as markup.
//
// No `aria-live`: the table re-renders on every heartbeat of the selected board (the
// `StatusStrip.tsx` reason).

import { type DeviceSummary } from './api'
import {
  describeLevel,
  formatDropped,
  LOG_TAIL_LIMIT,
  tailRows,
  useDeviceLog,
} from './deviceLog'
import { formatWhen } from './format'

export function DeviceLog({
  deviceId,
  device,
  hasBoards,
  onSessionExpired,
}: {
  /** The effective board id (picked, or the only board), or `null` for none. */
  deviceId: string | null
  /** The fleet row for `deviceId`, or `null` when there is none (an arrival, or no pick). */
  device: DeviceSummary | null
  hasBoards: boolean
  onSessionExpired: () => void
}) {
  const { lines, error } = useDeviceLog({
    deviceId: device !== null ? deviceId : null,
    hint: device?.last_seen ?? null,
    onSessionExpired,
  })

  if (!hasBoards) return null

  let body
  if (deviceId === null) {
    body = (
      <p className="muted" data-testid="device-log-none">
        Pick a board in the Fleet table to read the log it sent.
      </p>
    )
  } else if (device === null) {
    body = (
      <p className="muted" data-testid="device-log-pending">
        This board is not in the fleet yet; its log appears once it has enrolled.
      </p>
    )
  } else {
    body = (
      <>
        <p>
          {device.name !== null ? <bdi>{device.name}</bdi> : <code>{device.device_id}</code>}{' '}
          <span className="muted">
            newest first · last 7 days · up to {LOG_TAIL_LIMIT} lines. This is the log the board
            sent over the network (<code>up/log</code>), not the serial console.
          </span>
        </p>
        {error !== null && (
          <p className="bad" role="alert" data-testid="device-log-error">
            {error}
            {lines !== null && ' — showing the last lines that loaded.'}
          </p>
        )}
        {lines === null ? (
          error === null && <p aria-busy="true">Loading…</p>
        ) : lines.length === 0 ? (
          <p className="muted" data-testid="device-log-empty">
            No log lines from this board in the last 7 days. A board sends its log only when its
            firmware opts in.
          </p>
        ) : (
          <>
            <table className="log device-log" data-testid="device-log-lines">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Level</th>
                  <th>Line</th>
                </tr>
              </thead>
              <tbody>
                {tailRows(lines).map((row) => {
                  if (row.kind === 'gap') {
                    return (
                      <tr key={row.key} data-testid="log-gap" data-dropped={row.dropped}>
                        <td
                          colSpan={3}
                          className="warn"
                          title="lines the board dropped, or sent in a form the server could not store"
                        >
                          <span aria-hidden="true">⋯ </span>
                          {formatDropped(row.dropped)}
                        </td>
                      </tr>
                    )
                  }
                  const { line } = row
                  const level = describeLevel(line.level)
                  return (
                    <tr
                      key={line.id}
                      data-testid="log-line"
                      data-id={line.id}
                      data-level={line.level}
                    >
                      <td
                        title={`logged ${formatWhen(line.logged_at)}, received ${formatWhen(line.received_at)}`}
                      >
                        {formatWhen(line.logged_at)}
                      </td>
                      <td
                        className={level.tone ?? undefined}
                        title={level.label ?? 'a level this dashboard does not know'}
                      >
                        {level.level}
                      </td>
                      <td className="log-text">
                        <bdi>{line.text}</bdi>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            {lines.length === LOG_TAIL_LIMIT && (
              <p className="muted" data-testid="device-log-capped">
                Showing the newest {LOG_TAIL_LIMIT} lines.
              </p>
            )}
          </>
        )}
      </>
    )
  }

  return (
    <section
      className="console-card"
      aria-labelledby="device-log-heading"
      data-testid="device-log"
      data-device-id={deviceId}
    >
      <h2 id="device-log-heading">Device log</h2>
      {body}
    </section>
  )
}
