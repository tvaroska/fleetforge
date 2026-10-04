// The onboarding result card (R2b-fe-3). Renders `onboardingResult.ts` or a `FlashFailure`
// and decides nothing: which card shows, and when, is the caller's (`BoardConsole.tsx` for
// the console result, `FlashBoard.tsx` for a failed write). Only one renders at a time.
//
// No `aria-live` on the console card: the console ticks at 1 Hz. All device-controlled
// strings (ssid, ids, versions, error text) go through React's escaping.

import { Fragment, useId, type ReactNode } from 'react'
import { type FlashFailure } from './flashFailure'
import { type OnboardingResult, type ResultRow } from './onboardingResult'

type Props =
  | { result: OnboardingResult; action?: ReactNode; copy?: ReactNode }
  | { flashFailure: FlashFailure; details: string | null; action: ReactNode }

function Rows({ rows }: { rows: ResultRow[] }) {
  return (
    <dl data-testid="result-rows">
      {rows.map((row) => (
        <Fragment key={row.label}>
          <dt>{row.label}</dt>
          <dd className={row.tone ?? undefined}>{row.value}</dd>
        </Fragment>
      ))}
    </dl>
  )
}

export function ResultCard(props: Props) {
  const headingId = useId()

  if ('flashFailure' in props) {
    const failure = props.flashFailure
    return (
      <section
        className="issued"
        data-testid="result-card"
        data-outcome="flash-failed"
        aria-labelledby={headingId}
        role="alert"
      >
        <h3 id={headingId}>Result</h3>
        <p className="bad">
          <strong>{failure.cause}</strong>
        </p>
        <p>{failure.tryFirst}</p>
        <p>{props.action}</p>
        {props.details !== null && <p className="muted">Details: {props.details}</p>}
      </section>
    )
  }

  const result = props.result
  if (result.outcome === 'success') {
    return (
      <section
        className="issued"
        data-testid="result-card"
        data-outcome="success"
        aria-labelledby={headingId}
      >
        <h3 id={headingId}>Result</h3>
        <p className="ok" data-testid="console-online">
          This board enrolled and is on the fleet. You can release the port.
        </p>
        <Rows rows={result.rows} />
        {result.versionsDiffer && <p className="warn">UI and API differ.</p>}
      </section>
    )
  }

  const action = props.action ?? null
  return (
    <section
      className="issued"
      data-testid="result-card"
      data-outcome="failure"
      aria-labelledby={headingId}
      role="status"
    >
      <h3 id={headingId}>Result</h3>
      <p className="bad">
        <strong data-testid="result-headline">{result.headline}</strong>
      </p>
      <p data-testid="result-next">{action ?? result.next}</p>
      {props.copy != null && <p>{props.copy}</p>}
      <Rows rows={result.rows} />
      {result.versionsDiffer && <p className="warn">UI and API differ.</p>}
    </section>
  )
}
