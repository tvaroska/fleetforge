// "4 · Watch the board" — the panel that turns a silent board into a diagnosis.
//
// Before this existed, the flash page ended at "it should appear in the fleet above within
// a few seconds". When it didn't, there was nothing to look at: the operator had to guess
// between a bad SSID, an unset clock and a spent token, and the only way to find out was
// `screen /dev/tty.usbserial-… 115200` in another window. That is the dead end this closes.
//
// All the judgement is in `boardConsole.ts`. This file renders it. R2b-fe-3's result card
// lives here too; its judgement is in `onboardingResult.ts`. R2b-fe-5's server view (the
// fleet's rows, so a native-USB port loss never reads as "no board") is judged in
// `serverWatch.ts` and merged into the console's summary here. R2b-fe-6's name form
// (`NameBoard.tsx`) hangs off the success card when the page gave the panel `naming`.

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  MILESTONES,
  MILESTONE_LABELS,
  REMEDY_LABELS,
  describeRestarts,
  resetLabel,
  useBoardConsole,
  type ConsoleAcquire,
  type ConsoleFactory,
  type ConsoleLevel,
  type Milestone,
  type Remedy,
} from './boardConsole'
import { buildDiagnosticBundle, type DiagnosticContext } from './diagnostics'
import { explainFlashError } from './flasher'
import { describeOnboardingResult, type ResultContext } from './onboardingResult'
import { NameBoard } from './NameBoard'
import { ResultCard } from './ResultCard'
import {
  SERVER_WAIT_MS,
  describeServerView,
  lastDeviceId,
  mergeServerView,
  takeBaseline,
  type FleetBaseline,
} from './serverWatch'

const LEVEL_CLASS: Record<ConsoleLevel, string> = {
  error: 'bad',
  warn: 'warn',
  info: '',
  plain: 'muted',
}

function Checklist({
  reached,
  skipped,
  retracted,
  waitingFor,
  fromServer,
}: {
  reached: Milestone[]
  skipped: Milestone[]
  /** Reached by an earlier boot and lost at an uncommanded restart (R2b-fe-4). */
  retracted: Milestone[]
  waitingFor: Milestone | null
  /** Marked from the server's view, not the console's (R2b-fe-5). */
  fromServer: Milestone[]
}) {
  return (
    <ol className="milestones" data-testid="boot-milestones">
      {MILESTONES.map((milestone) => {
        const done = reached.includes(milestone)
        const wasSkipped = !done && skipped.includes(milestone)
        const lost = !done && !wasSkipped && retracted.includes(milestone)
        // done > skipped > waiting > retracted > pending. A retracted milestone that is
        // also the one being waited on stays 'waiting' and carries the loss as a flag.
        const state = done
          ? 'done'
          : wasSkipped
            ? 'skipped'
            : milestone === waitingFor
              ? 'waiting'
              : lost
                ? 'retracted'
                : 'pending'
        const server = done && fromServer.includes(milestone)
        return (
          <li
            key={milestone}
            className={state}
            data-state={state}
            data-retracted={state === 'waiting' && lost ? 'true' : undefined}
            data-source={server ? 'server' : undefined}
          >
            <span aria-hidden="true">
              {done
                ? '✓'
                : wasSkipped
                  ? '–'
                  : milestone === waitingFor
                    ? '…'
                    : lost
                      ? '↺'
                      : '·'}
            </span>{' '}
            {MILESTONE_LABELS[milestone]}
            {server && <span className="muted"> — from the server</span>}
            {state === 'skipped' && <span className="muted"> — not logged</span>}
            {state === 'waiting' && (
              <span className="muted">{lost ? ' — waiting (lost at the restart)' : ' — waiting'}</span>
            )}
            {state === 'retracted' && <span className="muted"> — lost at the restart</span>}
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
  onReflash,
  reflashBlockedReason = null,
  diagnostics,
  result,
  hideResult = false,
  naming,
}: {
  autoWatch: boolean
  // Injected by the tests only: jsdom has no `navigator.serial` (see `boardConsole.ts`).
  createConsole?: ConsoleFactory
  /**
   * S0-fe-6. Runs a full re-flash: re-acquire the port with esptool, mint a fresh
   * single-use token, write ff_cfg + the agent, erase NVS. Absent when the page cannot
   * flash right now — and then NO button is rendered, because a button that cannot work
   * is the thing this task forbids. Optional: the panel is also used with no flasher
   * behind it at all.
   */
  onReflash?: () => Promise<void>
  /** Why `onReflash` is absent, in plain language. Rendered as muted text, never as a
   *  disabled button — a disabled button explains nothing. */
  reflashBlockedReason?: string | null
  /**
   * S0-fe-7. What the flasher page knows and this panel does not — the chip, the config it
   * would write, the versions, and the secrets that must never reach the bundle. Absent
   * when the panel is used with no flasher behind it, and the bundle is still useful: the
   * log, the fault and what the board says about itself all come off the events.
   */
  diagnostics?: DiagnosticContext
  /**
   * R2b-fe-3. What the flasher page knows for the result card: the fleet rows, the UI/API
   * versions and what it just wrote. Absent in a standalone panel, and the card still
   * renders from the console alone.
   */
  result?: ResultContext
  /** R2b-fe-3. The flasher is showing its own (flash-failed) card; never show two. */
  hideResult?: boolean
  /**
   * R2b-fe-6. The capability to name the board from the success card: re-read the fleet
   * after a save, drop to the login gate on a 401. Absent in a standalone panel (nothing
   * to re-read, no session to lose), and then no form renders: a form that cannot work is
   * what this codebase forbids. It also needs a fleet row for the board, or the PATCH
   * would be a 404.
   */
  naming?: { onSessionExpired: () => void; onSaved: () => void }
}) {
  const state = useBoardConsole({ createConsole, explainError: explainFlashError })
  const { watch: watchConsole } = state

  // R2b-fe-5. The watch anchor: a board flashed elsewhere, watched with no flash in this tab.
  // Taken on the first watch and kept across re-watches (a re-taken baseline after the board
  // re-enrolled would never show the change); Clear drops it. A flash in this tab supplies
  // its own baseline (`result.flashBaseline`, owned by `FlashBoard`), which always wins.
  const [watchBaseline, setWatchBaseline] = useState<FleetBaseline | null>(null)
  const [baselineWanted, setBaselineWanted] = useState(false)
  // When the console last stopped (acquire failed, stream ended, or released); null while a
  // watch runs. Starts the server-wait deadline. Set when `watch()` settles rather than off
  // the `opening` flag: a fast acquire failure sets `opening` true and false in one batch.
  const [consoleStoppedAt, setConsoleStoppedAt] = useState<number | null>(null)
  const flashBaseline = result?.flashBaseline ?? null
  const devices = result?.devices ?? null
  const anchor = useRef({ flashBaseline, devices, watchBaseline })
  anchor.current = { flashBaseline, devices, watchBaseline }
  const watch = useCallback(
    (acquire: ConsoleAcquire) => {
      const held = anchor.current
      if (held.flashBaseline === null && held.watchBaseline === null) {
        if (held.devices !== null) setWatchBaseline(takeBaseline(held.devices))
        else setBaselineWanted(true)
      }
      setConsoleStoppedAt(null)
      return watchConsole(acquire).finally(() => setConsoleStoppedAt(Date.now()))
    },
    [watchConsole],
  )
  // A fleet that had not loaded at the first watch: capture on the first render that has it.
  // Accepted limit: it could already hold the new enrolment.
  useEffect(() => {
    if (!baselineWanted || devices === null) return
    setBaselineWanted(false)
    setWatchBaseline((current) => current ?? takeBaseline(devices))
  }, [baselineWanted, devices])

  const running = state.watching || state.opening
  const [recovering, setRecovering] = useState(false)
  /**
   * The bundle as it was at the moment of the click, and what the clipboard got.
   *
   * A snapshot rather than a derivation: the summary ticks at 1 Hz while watching, so a
   * textarea that re-derived on every render would drift from the clipboard within a
   * second and the operator would be pasting a different artifact from the one they can
   * see.
   */
  const [bundle, setBundle] = useState<string | null>(null)
  const [copied, setCopied] = useState<boolean | null>(null)

  async function copyBundle() {
    const text = buildDiagnosticBundle({
      events: state.events,
      summary: state.summary,
      context: diagnostics ?? null,
      page: { origin: window.location.origin, userAgent: navigator.userAgent },
    })
    // Snapshot FIRST: whatever happens to the clipboard, what was copied is on screen.
    setBundle(text)
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
    } catch {
      // Never an error state. `navigator.clipboard` is undefined in jsdom (a TypeError)
      // and a real browser can simply refuse — in both cases the text is below.
      setCopied(false)
    }
  }

  async function runReflash() {
    if (onReflash === undefined) return
    setRecovering(true)
    try {
      // esptool needs the port and this panel is holding it. Release FIRST: the flasher
      // opens the same physical device and would otherwise get "already open".
      await state.release()
      await onReflash()
    } finally {
      setRecovering(false)
    }
  }

  // Deliberately NOT clearing the log. The old fault stays visible until the re-flashed
  // board prints its first `ff-agent` line — which is a `boot` milestone and therefore
  // clears the fault by the existing S0-fe-4 rule — and if the recovery flash fails, the
  // evidence is still on screen.
  //
  // Called as a plain function rather than rendered as `<RemedyAction/>`: a component
  // declared inside another is a new type on every render, so React would unmount and
  // remount the button mid-interaction.
  function remedyAction(remedy: Remedy) {
    if (remedy === 'reboot') {
      // An EN pulse needs an open port, so this one exists only while we hold it.
      if (!state.watching) return null
      return (
        <button
          type="button"
          className="remedy"
          data-testid="console-remedy"
          onClick={() => void state.reboot()}
        >
          {REMEDY_LABELS.reboot}
        </button>
      )
    }
    if (onReflash === undefined) {
      if (reflashBlockedReason === null) return null
      return (
        <span className="muted" data-testid="console-remedy-blocked">
          {reflashBlockedReason}
        </span>
      )
    }
    return (
      <button
        type="button"
        className="remedy"
        data-testid="console-remedy"
        onClick={() => void runReflash()}
        // Disabled while it runs: a double-click would mint two single-use tokens.
        disabled={recovering}
      >
        {recovering ? 'Re-flashing…' : REMEDY_LABELS.reflash}
      </button>
    )
  }

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

  // R2b-fe-5. The server's view, merged into the console's summary. Null (and the summary
  // untouched) without a fleet, an id or a baseline — the standalone panel is unchanged.
  const deviceId = lastDeviceId(state.events) ?? result?.flashed?.deviceId ?? null
  const server = describeServerView({
    deviceId,
    devices,
    baseline: flashBaseline ?? watchBaseline,
    expectEnroll: flashBaseline !== null,
  })
  const { summary, fromServer } = mergeServerView(state.summary, server, !running)
  // The console has stopped after trying (an error, or lines then a stop): the server is
  // what the operator watches now, so the checklist stays on screen.
  const serverShown =
    server !== null && !running && (state.error !== null || state.events.length > 0)
  const now = result?.now ?? null
  const serverOverdue =
    serverShown &&
    !server.onFleet &&
    consoleStoppedAt !== null &&
    now !== null &&
    now - consoleStoppedAt >= SERVER_WAIT_MS

  const { fault, rebootLoop, overdue, lastReset, restarts } = summary
  const restartSentence = describeRestarts(restarts)
  const busy = state.opening
  // Cheap, and `summary` already ticks at 1 Hz while watching.
  const outcome = describeOnboardingResult({
    events: state.events,
    summary,
    context: result ?? null,
    fromServer,
  })
  // R2b-fe-6. The fleet row the success card's name form edits, matched by id like
  // everywhere else (the device list is lowercase hex). No row, no form.
  const namedRow =
    outcome?.outcome === 'success' && deviceId !== null && devices !== null
      ? devices.find((d) => d.device_id.toLowerCase() === deviceId.toLowerCase())
      : undefined
  // `!hideResult` is part of it: with the card hidden, the toolbar keeps its copy button.
  const failureCard = !hideResult && outcome?.outcome === 'failure'
  const copyButton = (
    // Never shortened to "Copy": Testing Library matches accessible names by
    // substring, and `EnrollBoard` already renders a bare `Copy`. "Copied — secrets
    // redacted" is likewise unique on purpose.
    <button type="button" onClick={() => void copyBundle()}>
      {copied === true ? 'Copied — secrets redacted' : 'Copy diagnostic bundle'}
    </button>
  )

  return (
    <section aria-labelledby="console-heading">
      <h3 id="console-heading">4 · Watch the board</h3>
      <p className="muted">
        Reads the board's own log over the same USB cable at 115200 baud — the boot, the
        Wi-Fi join, the enrolment. When the page has the fleet, it also follows the server&rsquo;s
        view of the board, so a board that drops off USB when it resets can still finish here.
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
          <button
            type="button"
            onClick={() => {
              state.clear()
              setWatchBaseline(null)
              setBaselineWanted(false)
              setBundle(null)
              setCopied(null)
            }}
          >
            Clear
          </button>
        )}{' '}
        {/* R2b-fe-3: a failure card holds the one copy click, so the toolbar's goes. */}
        {(state.watching || state.opening || state.events.length > 0) &&
          !failureCard &&
          copyButton}
      </p>

      {state.opening && <p className="muted">Opening the port…</p>}

      {state.watching && lastReset !== null && (
        <p className="muted" data-testid="console-boot-count">
          Boot {state.summary.boots} · reset: {resetLabel(lastReset)}
          {lastReset.commanded ? ' (by this panel)' : ''}
        </p>
      )}

      {(state.watching || serverShown) && (
        <Checklist
          reached={summary.reached}
          skipped={summary.skipped}
          retracted={summary.retracted}
          waitingFor={summary.waitingFor}
          fromServer={fromServer}
        />
      )}

      {serverShown && (
        <p className={server.onFleet ? 'ok' : undefined} data-testid="console-server-view">
          {server.onFleet
            ? `The server sees ${server.deviceId} on the fleet.`
            : server.enrolled
              ? `The server has enrolled ${server.deviceId}; waiting for it to reach the broker.`
              : `Watching the server for ${server.deviceId}: not enrolled yet.`}
        </p>
      )}

      {/* No unbounded wait on the server either (spec/standards.md). */}
      {serverOverdue && (
        <p className="warn" data-testid="console-server-overdue">
          The server has not seen {server.deviceId} on the fleet in{' '}
          {Math.round(SERVER_WAIT_MS / 1000)} s. Press &ldquo;Watch a board&rdquo; to read its
          log, or re-flash it.
        </p>
      )}

      {fault !== null && (
        <p className="bad" role="status" data-testid="console-fault">
          <strong>{fault.text}</strong>
          <br />
          {fault.hint}
          {/* At most ONE action on screen, and since R2b-fe-3 it lives in the result card
              below, not here: every state that used to put a button in this paragraph
              (or the overdue one) is a failure-card state. */}
        </p>
      )}

      {/* Its own line, not the fault slot: on 2026-09-11 the board was both browning out AND
          restarting, and the operator needed to be told both. The loop is proof on its own
          even when nothing in the log explains it. */}
      {restartSentence !== null && rebootLoop === null && (
        <p className="warn" role="status" data-testid="console-restarts">
          <strong>{restartSentence}.</strong>
        </p>
      )}

      {rebootLoop !== null && (
        <p className="bad" role="status" data-testid="console-reboot-loop">
          <strong>
            This board keeps restarting —{' '}
            {restartSentence === null
              ? `${rebootLoop.boots} times so far`
              : restartSentence.charAt(0).toLowerCase() + restartSentence.slice(1)}
            .
          </strong>
          <br />
          It is not staying up long enough to join the fleet.{' '}
          {/* The guess is only worth printing when nothing better is on screen. Until
              2026-09-13 this line recommended a shorter, thicker cable unconditionally,
              including directly beneath a fault that had already named the exact cause —
              so an operator whose cable was fine spent an evening swapping cables and
              USB ports on the strength of it. When a fault is named, it has the log
              behind it and this line has nothing; defer to it. */}
          {fault !== null ? (
            <>Follow the cause named above — it is the one with evidence behind it.</>
          ) : (
            <>
              Nothing in the log says why, which usually means power: try a shorter, thicker
              USB cable straight into the machine rather than a hub, and if that changes
              nothing, suspect the board&rsquo;s own supply rather than what it is plugged
              into.
            </>
          )}
        </p>
      )}

      {/* "No unbounded wait" (spec/standards.md). A spinner that can spin forever is a
          failed acceptance, so the checklist's `…` grows a deadline. */}
      {overdue !== null && (
        <p className="warn" role="status" data-testid="console-overdue">
          <strong>
            {MILESTONE_LABELS[overdue.milestone]} has not happened in{' '}
            {Math.round(overdue.waitedMs / 1000)} s.
          </strong>
          <br />
          {overdue.hint}
        </p>
      )}

      {/* R2b-fe-3. One card: success (it carries the on-fleet sentence) or failure (it
          carries the one action and the one copy click). Never beside the flasher's own
          flash-failed card. */}
      {!hideResult && outcome !== null && (
        <ResultCard
          result={outcome}
          action={
            outcome.outcome === 'failure' && outcome.remedy !== null
              ? remedyAction(outcome.remedy)
              : null
          }
          copy={failureCard ? copyButton : null}
          naming={
            naming !== undefined && namedRow !== undefined ? (
              <NameBoard
                key={namedRow.device_id}
                deviceId={namedRow.device_id}
                currentName={namedRow.name}
                onSessionExpired={naming.onSessionExpired}
                onSaved={naming.onSaved}
              />
            ) : null
          }
        />
      )}

      {/* The text is unchanged (the bench's Check F fail signatures match on it); once the
          server has enrolled the board it is evidently fine, so it is muted. */}
      {state.error !== null && (
        <p className={serverShown && server.enrolled ? 'muted' : 'warn'}>{state.error}</p>
      )}

      {(state.watching || state.opening || state.events.length > 0) && (
        <>
          <h4 id="board-log-heading">Board log</h4>
          <pre className="log console" ref={tail} data-testid="board-console" aria-labelledby="board-log-heading">
            {state.events.filter((e) => e.source === 'board').length === 0 ? (
              <span className="muted" data-testid="board-console-waiting">
                Waiting for the first line from the board…{'\n'}
              </span>
            ) : null}
            {state.events.map((event) => (
              <span key={event.seq} className={LEVEL_CLASS[event.level]}>
                {event.raw}
                {'\n'}
              </span>
            ))}
          </pre>
        </>
      )}

      {/* S0-fe-7. A readOnly <textarea>, not a <pre>: on 2026-09-11 the log WAS on screen
          and the operator could not get it out, because selecting text in an unlabelled
          <pre> is not an affordance anyone finds. Ctrl-A inside this box selects the
          bundle and nothing else. */}
      {bundle !== null && (
        <>
          <h4 id="bundle-heading">Diagnostic bundle</h4>
          <p className="muted">
            Everything someone helping you needs: the log above, this board&apos;s chip and
            config, the versions, and the fault. The enrolment token, the Wi-Fi passphrase and
            any broker password are removed. Paste it wherever you are asking for help.
          </p>
          {copied === false && (
            <p className="warn">
              The browser would not give this page the clipboard. Select the text below and copy
              it.
            </p>
          )}
          <textarea
            className="log console"
            readOnly
            value={bundle}
            data-testid="diagnostic-bundle"
            aria-labelledby="bundle-heading"
          />
        </>
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
