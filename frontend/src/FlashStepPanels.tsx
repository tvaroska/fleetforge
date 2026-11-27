// Presentational step panels for `FlashBoard` (capability banner, stepper rail, USB detection,
// and flash execution / completion status).

import type { ReactNode } from 'react'
import type { AgentManifest, DeviceGroupSummary } from './api'
import { OTHER_BOARD_ID, type BoardCandidate } from './boards'
import { formatBytes, type FlashBoardState } from './flash'
import type { ChipInfo } from './flasher'
import { BAUD_RATES } from './FlashConfigForm'
import { PortHelp } from './PortHelp'
import { PreflightCard } from './PreflightCard'
import type { Preflight } from './preflight'
import { ResultCard } from './ResultCard'

export function FlashUnavailable({ reason }: { reason: 'no-web-serial' | 'insecure' }) {
  return (
    <section className="console-card" aria-labelledby="flash-heading">
      <h2 id="flash-heading">Flash a board</h2>
      <p className="muted">
        {reason === 'no-web-serial' ? (
          <>
            Flashing needs the Web Serial API: use Chrome or Edge. Chromium-only is an accepted v1
            limit (<code>spec/prd.md</code> — client constraint). Everything else on this page works
            in any browser; you can still generate a token below and flash the board with{' '}
            <code>esptool.py</code>.
          </>
        ) : (
          <>
            The browser will not grant serial access over an insecure connection. Open this
            dashboard over HTTPS, or on <code>http://localhost</code>, and reload.
          </>
        )}
      </p>
    </section>
  )
}

export function FlashStepperRail({
  step1Done,
  step2Done,
  step3Done,
  flashing,
}: {
  step1Done: boolean
  step2Done: boolean
  step3Done: boolean
  flashing: boolean
}) {
  return (
    <ol className="stepper-rail" data-testid="flash-stepper" aria-label="Flash steps">
      <li className={step1Done ? 'done' : 'active'}>
        <span aria-hidden="true">{step1Done ? '✓ ' : '▶ '}</span>1. DETECT USB
      </li>
      <li className={step2Done ? 'done' : step1Done ? 'active' : ''}>
        <span aria-hidden="true">{step2Done ? '✓ ' : step1Done ? '▶ ' : '· '}</span>2. CONFIGURE
        WI-FI
      </li>
      <li className={step3Done ? 'done' : flashing ? 'active' : ''}>
        <span aria-hidden="true">{step3Done ? '✓ ' : flashing ? '▶ ' : '· '}</span>
        3. WRITE FLASH
      </li>
      <li className={step3Done ? 'active' : ''}>
        <span aria-hidden="true">{step3Done ? '▶ ' : '· '}</span>4. VERIFY BOOT
      </li>
    </ol>
  )
}

export function FlashDetectSection({
  baudRate,
  busy,
  chip,
  predicted,
  candidates,
  board,
  portHelpOpen,
  preflight,
  fleetNow,
  showPreflight,
  onChangeBaud,
  onConnect,
  onTogglePortHelp,
  onSelectBoard,
}: {
  baudRate: number
  busy: boolean
  chip: ChipInfo | null
  predicted: string | null
  candidates: BoardCandidate[]
  board: string
  portHelpOpen: boolean
  preflight: Preflight | null
  fleetNow?: number
  showPreflight: boolean
  onChangeBaud: (baudRate: number) => void
  onConnect: () => void
  onTogglePortHelp: (open: boolean) => void
  onSelectBoard: (boardId: string) => void
}) {
  return (
    <>
      <h3>1 · The board</h3>
      <p>
        <label>
          Baud rate{' '}
          <select
            value={baudRate}
            onChange={(event) => onChangeBaud(Number(event.target.value))}
            disabled={busy}
          >
            {BAUD_RATES.map((rate) => (
              <option key={rate} value={rate}>
                {rate}
              </option>
            ))}
          </select>
        </label>{' '}
        <button type="button" onClick={onConnect} disabled={busy}>
          {chip === null ? 'Select port and detect' : 'Select a different port'}
        </button>
      </p>
      <PortHelp open={portHelpOpen} onToggle={onTogglePortHelp} />

      {chip !== null && (
        <>
          <dl data-testid="chip-info">
            <dt>Chip</dt>
            <dd>{chip.description}</dd>
            <dt>MAC</dt>
            <dd>{chip.macAddress ?? 'unavailable'}</dd>
            <dt>Flash</dt>
            <dd>{formatBytes(chip.flashSizeBytes)}</dd>
            <dt>Features</dt>
            <dd>{chip.features.join(', ') || '—'}</dd>
            <dt>Device ID</dt>
            <dd>
              {predicted === null ? 'unknown' : <code>{predicted}</code>}{' '}
              <span className="muted">(predicted — the board reads its own eFuse MAC at boot)</span>
            </dd>
          </dl>

          <fieldset>
            <legend>Which board is this?</legend>
            <p className="muted">
              Detection is chip-level. This picks nothing on the server — it is a label for you.
            </p>
            {candidates.map((candidate) => (
              <label key={candidate.id} className="choice">
                <input
                  type="radio"
                  name="board"
                  value={candidate.id}
                  checked={board === candidate.id}
                  onChange={() => onSelectBoard(candidate.id)}
                />{' '}
                {candidate.label}
              </label>
            ))}
            <label className="choice">
              <input
                type="radio"
                name="board"
                value={OTHER_BOARD_ID}
                checked={board === OTHER_BOARD_ID}
                onChange={() => onSelectBoard(OTHER_BOARD_ID)}
              />{' '}
              Other — enter manually
            </label>
          </fieldset>
          {showPreflight && preflight !== null && fleetNow !== undefined && (
            <PreflightCard preflight={preflight} now={fleetNow} />
          )}
        </>
      )}
    </>
  )
}

export function FlashActionSection({
  state,
  chip,
  manifest,
  configError,
  preflight,
  groups,
  selectedGroupId,
  batchPrefix,
  formattedBatchSeq,
  suggestedBatchName,
  retryAction,
  onSelectGroup,
  onChangeBatchPrefix,
  onFlash,
  onFlashAnother,
  onGoToFleet,
}: {
  state: FlashBoardState
  chip: ChipInfo | null
  manifest: AgentManifest | null
  configError: string | null
  preflight: Preflight | null
  groups?: DeviceGroupSummary[]
  selectedGroupId: string
  batchPrefix: string
  formattedBatchSeq: string
  suggestedBatchName: string | null
  retryAction: ReactNode
  onSelectGroup: (groupId: string) => void
  onChangeBatchPrefix: (prefix: string) => void
  onFlash: () => void
  onFlashAnother: () => void
  onGoToFleet?: () => void
}) {
  const failure = state.failure

  return (
    <>
      <h3>3 · Flash</h3>
      <p className="muted">
        Every flash mints a fresh single-use token. The board notices the new token on its first
        boot, clears the broker credential it was holding and enrols again — so a board that has
        already been enrolled re-registers without erasing its cached radio calibration.
      </p>
      {configError !== null && (
        <p className="bad" role="alert" data-testid="config-error">
          {configError}
        </p>
      )}
      {groups && groups.length > 0 && (
        <p>
          <label>
            Group{' '}
            <select
              value={selectedGroupId}
              onChange={(event) => onSelectGroup(event.target.value)}
              disabled={state.busy}
            >
              <option value="">Ungrouped</option>
              {groups.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          </label>
        </p>
      )}
      <p>
        <label>
          Auto-increment name prefix (batch mode){' '}
          <input
            type="text"
            data-testid="batch-name-prefix"
            placeholder="e.g. gh-node-"
            value={batchPrefix}
            onChange={(event) => onChangeBatchPrefix(event.target.value)}
            disabled={state.busy}
          />
        </label>
        {suggestedBatchName !== null && (
          <>
            {' '}
            <span className="muted" data-testid="batch-next-name">
              Counter <code>#{formattedBatchSeq}</code> · Next board name:{' '}
              <code>{suggestedBatchName}</code>
            </span>
          </>
        )}
      </p>
      <p>
        <button
          type="button"
          onClick={onFlash}
          disabled={state.busy || chip === null || configError !== null || manifest === null}
        >
          {preflight?.kind === 'known' ? 'Re-flash and re-enrol this board' : 'Flash this board'}
        </button>
      </p>

      {failure !== null ? (
        <ResultCard flashFailure={failure} details={state.error} action={retryAction} />
      ) : (
        state.error !== null && (
          <p className="bad" role="alert">
            {state.error}
          </p>
        )
      )}

      {state.step !== '' && (
        <p aria-live="polite" data-testid="flash-step">
          {state.step}
        </p>
      )}

      {state.progress !== null && (
        <p data-testid="flash-progress">
          {state.progress.label} — part {state.progress.partIndex + 1} of {state.progress.partCount}
          <progress value={state.progress.written} max={state.progress.total || 1} />
        </p>
      )}

      {state.phase === 'done' && (
        <section className="issued" aria-labelledby="flashed-heading">
          <h3 id="flashed-heading">Flashed</h3>
          <p>
            The board is rebooting
            {state.flashedDeviceId === null ? (
              '.'
            ) : (
              <>
                {' '}
                as <code>{state.flashedDeviceId}</code>.
              </>
            )}{' '}
            Watch it come up below — the console opens by itself, resets the board once so the log
            starts at the top, and names the cause if it stops short of the fleet.
          </p>
          {suggestedBatchName !== null && (
            <p className="muted" data-testid="batch-flashed-name">
              Batch name queued: <code>{suggestedBatchName}</code>
            </p>
          )}
          <p>
            <button type="button" onClick={onFlashAnother}>
              Flash another board
            </button>
            {onGoToFleet && (
              <>
                {' '}
                <button type="button" onClick={onGoToFleet}>
                  View board in Fleet &gt;
                </button>
              </>
            )}
          </p>
        </section>
      )}

      {state.log.length > 0 && (
        <details data-testid="flash-log">
          <summary>esptool output ({state.log.length} lines)</summary>
          <pre className="log">{state.log.join('\n')}</pre>
        </details>
      )}
    </>
  )
}
