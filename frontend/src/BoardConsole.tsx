// "4 · Watch the board" — the panel that turns a silent board into a diagnosis.
//
// Before this existed, the flash page ended at "it should appear in the fleet above within
// a few seconds". When it didn't, there was nothing to look at: the operator had to guess
// between a bad SSID, an unset clock and a spent token, and the only way to find out was
// `screen /dev/tty.usbserial-… 115200` in another window. That is the dead end this closes.
//
// All the judgement is in `boardConsole.ts`. This file renders it.

import { useEffect, useRef } from 'react'
import {
  MILESTONES,
  MILESTONE_LABELS,
  useBoardConsole,
  type ConsoleFactory,
  type ConsoleLevel,
  type Milestone,
} from './boardConsole'
import { explainFlashError } from './flasher'

const LEVEL_CLASS: Record<ConsoleLevel, string> = {
  error: 'bad',
  warn: 'warn',
  info: '',
  plain: 'muted',
}

function Checklist({ reached, waitingFor }: { reached: Milestone[]; waitingFor: Milestone | null }) {
  return (
    <ol className="milestones" data-testid="boot-milestones">
      {MILESTONES.map((milestone) => {
        const done = reached.includes(milestone)
        const state = done ? 'done' : milestone === waitingFor ? 'waiting' : 'pending'
        return (
          <li key={milestone} className={state} data-state={state}>
            <span aria-hidden="true">{done ? '✓' : milestone === waitingFor ? '…' : '·'}</span>{' '}
            {MILESTONE_LABELS[milestone]}
            {state === 'waiting' && <span className="muted"> — waiting</span>}
          </li>
        )
      })}
    </ol>
  )
}

export function BoardConsolePanel({
  /** Set once a flash has finished: the panel opens the port by itself, no second click. */
  autoWatch,
  createConsole,
}: {
  autoWatch: boolean
  // Injected by the tests only: jsdom has no `navigator.serial` (see `boardConsole.ts`).
  createConsole?: ConsoleFactory
}) {
  const state = useBoardConsole({ createConsole, explainError: explainFlashError })
  const { watch } = state

  // One auto-open per flash. `autoWatch` goes true when the flash completes and false again
  // on "Flash another board", so the ref resets with it; without the ref, any re-render
  // during a session would try to reopen a port we already hold.
  const armed = useRef(false)
  useEffect(() => {
    if (!autoWatch) {
      armed.current = false
      return
    }
    if (armed.current) return
    armed.current = true
    // 'granted', not 'prompt': the flasher never calls `port.forget()`, so the permission
    // from the flash is still live and no user gesture is needed here.
    void watch('granted')
  }, [autoWatch, watch])

  const tail = useRef<HTMLPreElement>(null)
  useEffect(() => {
    const element = tail.current
    if (element !== null) element.scrollTop = element.scrollHeight
  }, [state.events.length])

  const { fault } = state.summary
  const busy = state.opening

  return (
    <section aria-labelledby="console-heading">
      <h3 id="console-heading">4 · Watch the board</h3>
      <p className="muted">
        Reads the board's own log over the same USB cable at 115200 baud — the boot, the
        Wi-Fi join, the enrolment. Nothing here talks to the server.
      </p>

      <p>
        {state.watching || state.opening ? (
          <>
            <button type="button" onClick={() => void state.release()}>
              Release the port
            </button>{' '}
            <button type="button" onClick={() => void state.reboot()} disabled={!state.watching}>
              Reboot the board
            </button>
          </>
        ) : (
          <button type="button" onClick={() => void watch('prompt')} disabled={busy}>
            Watch a board
          </button>
        )}{' '}
        {state.events.length > 0 && (
          <button type="button" onClick={state.clear}>
            Clear
          </button>
        )}
      </p>

      {state.opening && <p className="muted">Opening the port…</p>}

      {state.watching && (
        <Checklist reached={state.summary.reached} waitingFor={state.summary.waitingFor} />
      )}

      {fault !== null && (
        <p className="bad" role="status" data-testid="console-fault">
          <strong>{fault.text}</strong>
          <br />
          {fault.hint}
        </p>
      )}

      {state.summary.waitingFor === null && state.summary.reached.length === MILESTONES.length && (
        <p className="ok" role="status" data-testid="console-online">
          This board enrolled and is on the fleet. You can release the port.
        </p>
      )}

      {state.error !== null && <p className="warn">{state.error}</p>}

      {state.events.length > 0 && (
        <pre className="log console" ref={tail} data-testid="board-console">
          {state.events.map((event) => (
            <span key={event.seq} className={LEVEL_CLASS[event.level]}>
              {event.raw}
              {'\n'}
            </span>
          ))}
        </pre>
      )}

      {!state.watching && !state.opening && state.events.length === 0 && (
        <p className="muted">
          The port is free — <code>screen</code>, <code>idf.py monitor</code> and{' '}
          <code>esptool.py</code> can all have it while this panel is idle.
        </p>
      )}
    </section>
  )
}
