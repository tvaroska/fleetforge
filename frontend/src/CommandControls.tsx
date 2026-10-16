// The command controls (R5-fe-1): Apply, Cancel update and Reboot, and the board's answer
// to the newest verb. Like `DeployCell`, this component ACTS: it owns the three POSTs and
// the little state they need (in flight, the reboot confirm step, the last error, the
// "queued" line). Which verbs are offered and how the answer reads is decided in
// `commands.ts`.
//
// The answer is read from `device.last_command` — the server's read model on
// `GET /v1/devices`, re-read on every `device.command` SSE frame — never from anything this
// component remembers, so a reload or a second tab shows the same line. After a 202 the
// cell's `onSent` (the fleet's `refresh`) re-reads at once so the line paints immediately.
//
// * Apply and Cancel update post on one click: Apply is already a deliberate second step
//   (the operator chose "hold at staged"), and Cancel is the safe direction.
// * Reboot needs a confirm step; only `Reboot now` posts.
// * Server refusals (409 a capability the board lacks, a deploy already ended, …) are shown
//   VERBATIM in a `role="alert"` paragraph, the `DeployCell.fail()` precedent. A 401 bounces
//   to the login gate.
// * No `aria-live`, no spinner and no timeout: the cell re-renders every second, and a
//   missing or `deferred` answer may legitimately last forever (`commands.ts`).
// * Every button names the board (`aria-label`), because there is one of these per row,
//   and the pre-check card's own `Cancel` sits in the same cell (hence `Cancel update`).

import { useState } from 'react'
import { ApiError, api, type CommandAccepted, type DeviceSummary } from './api'
import { commandAcceptedMessage, lastCommandLine, offeredCommands } from './commands'

export type CommandControlsProps = {
  device: DeviceSummary
  now: number
  /** The fleet's own `refresh`, so `last_command` paints immediately after a 202. */
  onSent: () => void
  onSessionExpired: () => void
}

export function CommandControls({ device, now, onSent, onSessionExpired }: CommandControlsProps) {
  const [sending, setSending] = useState(false)
  const [confirmingReboot, setConfirmingReboot] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [queued, setQueued] = useState<string | null>(null)

  const offer = offeredCommands(device)
  const name = device.name ?? device.device_id
  const last = device.last_command ?? null
  const line = last !== null ? lastCommandLine(last, device, now) : null
  const anyButton = offer.apply !== null || offer.cancel !== null || offer.reboot

  async function post(call: () => Promise<CommandAccepted>) {
    setSending(true)
    setError(null)
    setQueued(null)
    setConfirmingReboot(false)
    try {
      const accepted = await call()
      setQueued(commandAcceptedMessage(accepted))
      onSent()
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
        return
      }
      setError(err instanceof Error ? err.message : 'the command could not be sent')
    } finally {
      setSending(false)
    }
  }

  if (line === null && !anyButton && error === null && queued === null) return null

  const apply = offer.apply
  const cancel = offer.cancel

  return (
    <div className="command-controls" data-testid="command-controls">
      {line !== null && (
        <p data-testid="last-command" data-result={line.result}>
          {line.verb} ·{' '}
          <span className={line.tone ?? (line.result === 'none' ? 'muted' : undefined)}>{line.answer}</span>
          {/* Device text, rendered as text and never interpreted. */}
          {line.detail !== null && <>: {line.detail}</>}{' '}
          <span className="muted" title={line.agoTitle}>
            ({line.ago})
          </span>
        </p>
      )}

      {anyButton && (
        <p>
          {apply !== null && (
            <>
              <button
                type="button"
                data-testid="command-apply"
                aria-label={`Apply the staged update on ${name}`}
                disabled={sending}
                onClick={() => void post(() => api.applyDeploy(device.device_id, apply))}
              >
                Apply
              </button>{' '}
            </>
          )}
          {cancel !== null && (
            <>
              <button
                type="button"
                data-testid="command-cancel"
                aria-label={`Cancel the update on ${name}`}
                disabled={sending}
                onClick={() => void post(() => api.cancelDeploy(device.device_id, cancel))}
              >
                Cancel update
              </button>{' '}
            </>
          )}
          {offer.reboot && !confirmingReboot && (
            <button
              type="button"
              data-testid="command-reboot"
              aria-label={`Reboot ${name}`}
              disabled={sending}
              onClick={() => {
                setError(null)
                setQueued(null)
                setConfirmingReboot(true)
              }}
            >
              Reboot
            </button>
          )}
        </p>
      )}

      {offer.reboot && confirmingReboot && (
        <p data-testid="command-reboot-confirm">
          Reboot {name} now? It restarts on {device.fw_version ?? 'its current image'}.{' '}
          <button
            type="button"
            disabled={sending}
            onClick={() => void post(() => api.rebootDevice(device.device_id))}
          >
            Reboot now
          </button>{' '}
          <button type="button" onClick={() => setConfirmingReboot(false)}>
            Keep running
          </button>
        </p>
      )}

      {error !== null && (
        <p className="bad" role="alert">
          {error}
        </p>
      )}

      {queued !== null && error === null && (
        <p className="muted" data-testid="command-queued">
          {queued}
        </p>
      )}
    </div>
  )
}
