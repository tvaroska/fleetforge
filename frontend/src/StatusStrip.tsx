// The one status strip (R2b-fe-1). Renders `statusStrip.ts` and decides nothing.
//
// No `aria-live`: it re-renders on every fleet poll and a live region would chatter.
// All device-controlled text goes through React's escaping.

import { Fragment } from 'react'
import { type BuildInfo } from './buildInfo'
import { type HealthState } from './health'
import { describeVersions, type BoardLine } from './statusStrip'

function BoardHalf({ board }: { board: BoardLine }) {
  if (board.kind === 'loading') return <span data-testid="strip-board">Board: loading…</span>
  if (board.kind === 'none') {
    return (
      <span data-testid="strip-board">
        {board.reason === 'no-boards'
          ? 'Board: none yet'
          : 'Board: none selected — pick one in the Fleet table'}
      </span>
    )
  }
  return (
    <span data-testid="strip-board">
      Board <code>{board.name ?? board.deviceId}</code>
      {board.name !== null && <code className="muted"> ({board.deviceId})</code>}
      {board.platform !== null && <> · {board.platform}</>} · {board.firmware}
      {board.state.map((segment, index) => (
        <Fragment key={index}>
          {' · '}
          <span className={segment.tone ?? (segment.text === 'offline' ? 'muted' : undefined)}>
            {segment.text}
          </span>
        </Fragment>
      ))}
    </span>
  )
}

export function StatusStrip({
  ui,
  health,
  board,
}: {
  ui: BuildInfo
  health: HealthState
  board?: BoardLine
}) {
  const versions = describeVersions(ui, health)
  return (
    <div className="status-strip" role="region" aria-label="Status" data-testid="status-strip">
      <span data-testid="strip-versions">
        [ <span title={versions.uiTitle}>UI {versions.ui}</span> ·{' '}
        <span title={versions.apiTitle}>API {versions.api}</span> ]
      </span>
      {versions.differ && (
        <>
          {' '}
          <strong className="warn" data-testid="strip-mismatch">
            UI and API differ — reload the page; if it stays, the two were not deployed together
          </strong>
        </>
      )}
      {board !== undefined && (
        <>
          {' '}
          <BoardHalf board={board} />
        </>
      )}
    </div>
  )
}
