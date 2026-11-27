// React state hook and session lifecycle controller for the board UART console.
// Protocol parsing and boot-state folding live in `boardConsoleProtocol.ts` and
// `boardConsoleTypes.ts`, both re-exported here for callers.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { classifyConsoleLine, panelNotice, summarizeConsole } from './boardConsoleProtocol'
import {
  CONSOLE_BAUD_RATE,
  MAX_CONSOLE_LINES,
  type BoardConsole,
  type ConsoleAcquire,
  type ConsoleEvent,
  type ConsoleFactory,
  type ConsoleSummary,
} from './boardConsoleTypes'

export * from './boardConsoleTypes'
export * from './boardConsoleProtocol'

export const defaultConsoleFactory: ConsoleFactory = async (options) =>
  (await import('./serialConsole')).createSerialConsole(options)

export type BoardConsoleState = {
  watching: boolean
  opening: boolean
  events: ConsoleEvent[]
  summary: ConsoleSummary
  error: string | null
  watch: (acquire: ConsoleAcquire) => Promise<void>
  release: () => Promise<void>
  reboot: () => Promise<void>
  clear: () => void
}

/**
 * Owns at most one open Web Serial console session at a time, releasing any prior session
 * before opening a new one and always releasing the OS tty on React unmount.
 */
export function useBoardConsole({
  createConsole,
  explainError,
}: {
  createConsole?: ConsoleFactory
  explainError: (error: unknown) => string
}): BoardConsoleState {
  const [events, setEvents] = useState<ConsoleEvent[]>([])
  const [watching, setWatching] = useState(false)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!watching) return
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [watching])
  const summary = useMemo(() => summarizeConsole(events, now), [events, now])
  const [opening, setOpening] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const sessionRef = useRef<BoardConsole | null>(null)
  const runRef = useRef(0)
  const seqRef = useRef(0)
  const commandedResetAtRef = useRef<number | null>(null)

  const factoryRef = useRef(createConsole ?? defaultConsoleFactory)
  factoryRef.current = createConsole ?? defaultConsoleFactory

  const append = useCallback((raw: string) => {
    const event = classifyConsoleLine(raw, seqRef.current++)
    setEvents((current) => {
      const next = current.length >= MAX_CONSOLE_LINES ? current.slice(1) : current.slice()
      next.push(event)
      return next
    })
  }, [])

  const appendNotice = useCallback((raw: string, options: { commandedReset?: boolean } = {}) => {
    const event = panelNotice(raw, seqRef.current++, Date.now(), options)
    setEvents((current) => {
      const next = current.length >= MAX_CONSOLE_LINES ? current.slice(1) : current.slice()
      next.push(event)
      return next
    })
  }, [])

  const requestReboot = useCallback(
    (session: BoardConsole, why: string) => {
      appendNotice(`— ${why}`, { commandedReset: true })
      commandedResetAtRef.current = Date.now()
      void session.reboot().catch((err: unknown) => {
        appendNotice(
          `— could not reset the board: ${explainError(err)}. Press "Reboot the board".`,
        )
      })
    },
    [appendNotice, explainError],
  )

  const stop = useCallback(async () => {
    runRef.current += 1
    const session = sessionRef.current
    sessionRef.current = null
    setWatching(false)
    if (session !== null) await session.close()
  }, [])

  const watch = useCallback(
    async (acquire: ConsoleAcquire) => {
      await stop()
      const run = runRef.current
      setError(null)
      setOpening(true)
      let session: BoardConsole
      try {
        session = await factoryRef.current({
          baudRate: CONSOLE_BAUD_RATE,
          acquire,
          onLog: appendNotice,
        })
      } catch (err) {
        if (run === runRef.current) {
          setError(explainError(err))
          setOpening(false)
        }
        return
      }
      if (run !== runRef.current) {
        await session.close()
        return
      }
      sessionRef.current = session
      setOpening(false)
      setWatching(true)
      requestReboot(session, 'resetting the board so the log starts at its first line')
      try {
        for await (const line of session.lines()) {
          if (run !== runRef.current) break
          append(line)
        }
        if (run === runRef.current) {
          const recentReset =
            commandedResetAtRef.current !== null && Date.now() - commandedResetAtRef.current < 5000
          setError(
            recentReset
              ? 'The board dropped off the USB bus when it was reset — some boards re-enumerate. Press "Watch a board" to pick it up again.'
              : 'The board disconnected. Plug it back in and watch it again.',
          )
        }
      } catch (err) {
        if (run === runRef.current) setError(explainError(err))
      } finally {
        if (run === runRef.current) {
          sessionRef.current = null
          setWatching(false)
          await session.close()
        }
      }
    },
    [append, appendNotice, explainError, requestReboot, stop],
  )

  const release = useCallback(async () => {
    await stop()
    setError(null)
    setOpening(false)
  }, [stop])

  const reboot = useCallback(async () => {
    const session = sessionRef.current
    if (session === null) return
    requestReboot(session, 'reset requested — the board is restarting')
  }, [requestReboot])

  const clear = useCallback(() => setEvents([]), [])

  useEffect(
    () => () => {
      runRef.current += 1
      const session = sessionRef.current
      sessionRef.current = null
      void session?.close()
    },
    [],
  )

  return { watching, opening, events, summary, error, watch, release, reboot, clear }
}
