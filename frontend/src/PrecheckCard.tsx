// The pre-check card (R2b-fe-8). Renders `deployPrecheck.ts` over a `DeployPrecheck` and
// decides nothing: the fetch, the ticks and the send live in `DeployCell.tsx`.
//
// A refusal never has a send button here; a warning is overridden by the Send click, and a
// gating warning also needs its own tick. All server sentences go through unchanged, with
// the `Refused:` / `Warning:` label as a separate element (a word as well as a colour).
//
// No `aria-live` / `role="alert"`: the cell re-renders on every fleet poll. `useId` for the
// heading, because there is one Deploy cell per board.

import { useId } from 'react'
import { type DeployPrecheck } from './api'
import {
  canSend,
  fitLine,
  isGating,
  rollbackLine,
  sendLabel,
  summaryLine,
} from './deployPrecheck'

export type PrecheckCardProps = {
  precheck: DeployPrecheck
  ticked: ReadonlySet<string>
  onTick: (code: string, on: boolean) => void
  onSend: () => void
  onCancel: () => void
  sending: boolean
}

export function PrecheckCard({
  precheck,
  ticked,
  onTick,
  onSend,
  onCancel,
  sending,
}: PrecheckCardProps) {
  const headingId = useId()
  const fit = fitLine(precheck)
  const rollback = rollbackLine(precheck)

  return (
    <section
      className="precheck"
      data-testid="precheck-card"
      data-deployable={String(precheck.deployable)}
      aria-labelledby={headingId}
    >
      <h3 id={headingId}>Before you send</h3>
      <p data-testid="precheck-summary">{summaryLine(precheck)}</p>
      {fit !== null && (
        <p className="muted" data-testid="precheck-fit">
          {fit}
        </p>
      )}

      {precheck.refusals.length > 0 && (
        <>
          <ul data-testid="precheck-refusals">
            {precheck.refusals.map((refusal) => (
              <li key={refusal.code}>
                <strong className="bad">Refused:</strong> {refusal.message}
              </li>
            ))}
          </ul>
          <p>Refusals cannot be overridden.</p>
        </>
      )}

      {precheck.warnings.length > 0 && (
        <ul data-testid="precheck-warnings">
          {precheck.warnings.map((warning) => (
            <li key={warning.code}>
              <strong>Warning:</strong> {warning.message}
              {isGating(warning) && (
                <>
                  {' '}
                  <label>
                    <input
                      type="checkbox"
                      aria-label={`Send anyway: ${warning.code}`}
                      checked={ticked.has(warning.code)}
                      onChange={(event) => onTick(warning.code, event.target.checked)}
                    />{' '}
                    Send anyway despite this
                  </label>
                </>
              )}
            </li>
          ))}
        </ul>
      )}

      {rollback !== null && <p data-testid="precheck-rollback">{rollback}</p>}

      <p>
        {precheck.deployable && (
          <>
            <button type="button" onClick={onSend} disabled={sending || !canSend(precheck, ticked)}>
              {sending ? 'Sending…' : sendLabel(precheck)}
            </button>{' '}
          </>
        )}
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </p>
    </section>
  )
}
