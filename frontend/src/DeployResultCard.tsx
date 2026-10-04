// The update result card (R2b-fe-10): one finished deploy, in one place. Render only; every
// decision is made in `deployResult.ts`.
//
// No `role`, no `aria-live`, on purpose. The Deploy cell re-renders every second (the shared
// `now` tick moves "(N s ago)") and on every poll, so a live region here would be re-read
// to a screen-reader user over and over. The card is plain content, found by its heading.
// (The onboarding `ResultCard` uses `role="status"`; that card does not tick.)
//
// "Send again" (R2b-fe-11) shows only when the judgement offers it (`result.sendAgain`) AND
// the cell passes a handler (capability by prop). The click is the cell's: it opens the
// pre-check, it never posts. No local state here.

import { Fragment, useId } from 'react'
import { type DeployResult } from './deployResult'

export function DeployResultCard({
  result,
  onSendAgain,
  busy = false,
}: {
  result: DeployResult
  onSendAgain?: () => void
  busy?: boolean
}) {
  const headingId = useId()
  return (
    <section
      className="deploy-result"
      data-testid="deploy-result"
      data-outcome={result.outcome}
      aria-labelledby={headingId}
    >
      <h3 id={headingId}>Update result</h3>
      <p data-testid="deploy-verdict">
        <span className={result.tone ?? undefined} data-testid="deploy-state">
          {result.word}
        </span>
        {result.note !== null && <> — {result.note}</>}{' '}
        <span className="muted" title={result.agoTitle}>
          ({result.ago})
        </span>
      </p>
      {result.drift !== null && (
        <p className="muted" data-testid="deploy-drift">
          {result.drift}
        </p>
      )}
      {result.reason !== null && <p data-testid="deploy-result-reason">{result.reason}</p>}
      {result.next !== null && (
        <p data-testid="deploy-result-next">
          <strong>Next:</strong> {result.next}
        </p>
      )}
      {result.sendAgain !== null && onSendAgain && (
        <p>
          <button type="button" data-testid="deploy-send-again" disabled={busy} onClick={onSendAgain}>
            Send again
          </button>
        </p>
      )}
      <dl data-testid="deploy-result-rows">
        {result.rows.map((row) => (
          <Fragment key={row.label}>
            <dt>{row.label}</dt>
            <dd className={row.tone ?? undefined}>{row.value}</dd>
          </Fragment>
        ))}
      </dl>
      {result.versionsDiffer && <p className="warn">UI and API differ.</p>}
    </section>
  )
}
