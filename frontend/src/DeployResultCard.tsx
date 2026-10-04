// The update result card (R2b-fe-10): one finished deploy, in one place. Render only; every
// decision is made in `deployResult.ts`.
//
// No `role`, no `aria-live`, on purpose. The Deploy cell re-renders every second (the shared
// `now` tick moves "(N s ago)") and on every poll, so a live region here would be re-read
// to a screen-reader user over and over. The card is plain content, found by its heading.
// (The onboarding `ResultCard` uses `role="status"`; that card does not tick.)

import { Fragment, useId } from 'react'
import { type DeployResult } from './deployResult'

export function DeployResultCard({ result }: { result: DeployResult }) {
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
