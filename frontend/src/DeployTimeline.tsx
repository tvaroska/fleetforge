// The update timeline (R2b-fe-9): the rendering half. Everything it shows is decided by
// `deployTimeline.ts`; this file only lays it out.
//
// In flight it is an open list with the elapsed counter, the current state's "for N s",
// the board's own deadline (muted) and, once that deadline has passed, the stall sentence
// (`warn`). Once the server says the transaction is terminal it folds into a closed
// `<details>` whose summary is "took N s", so a finished row stays one line.
//
// No `role`, no `aria-live`: the cell re-renders every second from the shared tick and on
// every poll, and an announcing region would read the counter out once a second. No
// progress bar and no `%` (see `DeployCell.tsx`).

import { type DeploySummary, type DeviceSummary } from './api'
import { deployTimeline, type TimelineItem } from './deployTimeline'

const GLYPHS: Record<TimelineItem['status'], string> = {
  done: '✓',
  implied: '–',
  current: '…',
  pending: '·',
  failed: '✗',
}

function Items({ items, current }: { items: TimelineItem[]; current: string | null }) {
  return (
    <ol className="milestones deploy-timeline">
      {items.map((item) => (
        <li key={item.key} className={item.status} data-state={item.status}>
          <span aria-hidden="true">{GLYPHS[item.status]}</span> {item.label}
          {item.offset !== null && <span className="muted"> {item.offset}</span>}
          {item.status === 'implied' && <span className="muted"> — not reported</span>}
          {item.status === 'current' && current !== null && (
            <span className="muted"> — {current}</span>
          )}
        </li>
      ))}
    </ol>
  )
}

export function DeployTimeline({
  deploy,
  device,
  now,
}: {
  deploy: DeploySummary
  device: DeviceSummary
  now: number
}) {
  const timeline = deployTimeline(deploy, device, now)

  if (timeline.terminal) {
    return (
      <details className="deploy-timeline" data-testid="deploy-timeline">
        <summary className="muted" data-testid="deploy-elapsed">
          {timeline.elapsed}
        </summary>
        <Items items={timeline.items} current={null} />
      </details>
    )
  }

  return (
    <div data-testid="deploy-timeline">
      <p className="muted" data-testid="deploy-elapsed">
        {timeline.elapsed}
      </p>
      <Items items={timeline.items} current={timeline.current} />
      {timeline.deadline !== null && (
        <p className="muted" data-testid="deploy-deadline">
          {timeline.deadline}
        </p>
      )}
      {timeline.stall !== null && (
        <p className="warn" data-testid="deploy-stall">
          {timeline.stall}
        </p>
      )}
    </div>
  )
}
