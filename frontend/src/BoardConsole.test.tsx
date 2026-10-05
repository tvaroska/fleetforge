// What this defends:
//
// 1. **The operator never has to reach for `screen`.** The panel opens the port, shows the
//    boot, and drives a checklist to "On the fleet".
// 2. **A board that stops short is diagnosed by name**, not left as silence.
// 3. **Release really releases.** If this regresses, the next `screen` gets "Resource
//    busy" and the operator blames their cable.
// 4. **One port at a time**, and the port goes back on unmount.

import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { BoardConsolePanel } from './BoardConsole'
import {
  CONSOLE_BAUD_RATE,
  MILESTONE_DEADLINE_MS,
  type BoardConsole,
  type ConsoleFactory,
} from './boardConsole'
import { BENCH_2026_09_11 } from './fixtures/bench-2026-09-11'
import { BENCH_2026_10_04 } from './fixtures/bench-2026-10-04'
import { REBOOT_DURING_WATCH } from './fixtures/reboot-during-watch'
import { CAUSE_NEXT, type ResultContext } from './onboardingResult'
import type { DeviceSummary } from './api'
import { SERVER_WAIT_MS, takeBaseline, type FleetBaseline } from './serverWatch'

const HAPPY = [
  'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
  'I (120) ff-id: device_id a4cf12b3de90',
  'I (800) ff-wifi: associated; waiting for DHCP',
  'I (900) ff-net: wifi link up, ip 192.168.1.40 gw 192.168.1.1 mask 255.255.255.0',
  'I (1500) ff-time: sntp: 1970-01-01T00:00:02Z -> 2026-09-10T21:00:00Z (via pool.ntp.org)',
  'I (2600) ff-enroll: enroll 200 https://bingo.tvaroska.sk/v1/enroll',
  'I (3100) ff-mqtt: mqtt connected as a4cf12b3de90 (mqtts://bingo.tvaroska.sk:8883)',
]

/**
 * S0-fe-6's flagship fault: the board came up, joined the network, set its clock — and the
 * token baked into it had already been spent. `enroll_until_credentialed()` parks forever
 * on a 409, so nothing but a re-flash will ever move this board.
 */
const SPENT_TOKEN = [
  'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'I (100) ff-agent: fleetforge agent 0.3.0 (idf v5.5.5), built Sep 11 2026 08:14:02',
  'I (900) ff-net: wifi link up, ip 192.168.1.40 gw 192.168.1.1 mask 255.255.255.0',
  'I (1500) ff-time: sntp: 1970-01-01T00:00:02Z -> 2026-09-11T08:14:05Z (via pool.ntp.org)',
  'E (2600) ff-enroll: enroll 409: this token is already used, revoked or expired.',
  'E (2900) ff-agent: halted: this board\'s enrollment token was refused for good — re-flash ' +
    'ff_cfg with a fresh ffe_ token (POST /v1/enrollment-tokens)',
]

/**
 * Restarting, and the log says nothing about why — no BOD line, no halt, no disconnect
 * reason. This is the only case where the reboot-loop banner is allowed to guess, so it
 * is the counterpart to the assertion in the 2026-09-11 bench test that it must NOT.
 */
const UNEXPLAINED_LOOP = [
  'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
  'I (120) ff-id: device_id a4cf12b3de90',
  'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
  'I (120) ff-id: device_id a4cf12b3de90',
  'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
  'I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
  'I (120) ff-id: device_id a4cf12b3de90',
]

const SILENT_BOARD = [
  'I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
  'I (120) ff-id: device_id a4cf12b3de90',
  'I (300) ff-wifi: wifi sta starting, ssid home-5g',
  'W (5300) ff-wifi: disconnected (reason 201); reconnecting in 1000 ms',
  'W (6300) ff-agent: no network yet; waiting for the link',
]

/**
 * A board on a bench, minus the bench.
 *
 * `lines()` yields the script and then BLOCKS — it does not return. That is the real
 * behaviour and it is the point: `agent_main.c:166` retries forever, so a console that
 * ended its stream after the last line would have to invent an inactivity timeout, which
 * is exactly what this task forbids. The block is released by `close()`.
 */
function fakeConsole(script: string[], options: { silentUntilReset?: boolean; rebootRejects?: boolean } = {}) {
  const state = {
    opened: 0,
    closed: 0,
    reboots: 0,
    acquires: [] as string[],
    baudRates: [] as number[],
  }
  let release: (() => void) | null = null

  const factory: ConsoleFactory = async ({ baudRate, acquire }) => {
    state.opened += 1
    state.acquires.push(acquire)
    state.baudRates.push(baudRate)
    let closed = false
    let rebootRelease: (() => void) | null = null
    const rebootPromise = options.silentUntilReset
      ? new Promise<void>((resolve) => {
          rebootRelease = resolve
        })
      : Promise.resolve()

    const board: BoardConsole = {
      async *lines() {
        // S0-fe-5: a board that is silent until the panel pulses EN.
        await rebootPromise
        for (const line of script) {
          yield line
        }
        if (closed) return
        await new Promise<void>((resolve) => {
          release = resolve
        })
      },
      async reboot() {
        state.reboots += 1
        if (options.rebootRejects) {
          throw new Error('setSignals is unsupported')
        }
        rebootRelease?.()
      },
      async close() {
        if (closed) return
        closed = true
        state.closed += 1
        release?.()
      },
    }
    return board
  }

  return { factory, state }
}

describe('BoardConsolePanel', () => {
  it('opens the port by itself after a flash and drives the boot to the fleet', async () => {
    const { factory, state } = fakeConsole(HAPPY)
    render(<BoardConsolePanel autoWatch createConsole={factory} />)

    // No click: `autoWatch` means the operator's hands never leave the flash page.
    await waitFor(() => expect(state.opened).toBe(1))
    // 'granted', because the flasher never called `port.forget()`. A 'prompt' here would
    // need a user gesture the effect does not have, and would fail in a real browser.
    expect(state.acquires).toEqual(['granted'])
    expect(state.baudRates).toEqual([CONSOLE_BAUD_RATE])

    const console_ = await screen.findByTestId('board-console')
    await waitFor(() => {
      expect(console_).toHaveTextContent('enroll 200')
    })
    // Boot-ROM chatter is kept verbatim; it is how you tell a brownout from a bad image.
    expect(console_).toHaveTextContent('POWERON_RESET')

    const milestones = screen.getByTestId('boot-milestones')
    await waitFor(() => {
      expect(milestones.querySelectorAll('[data-state="done"]')).toHaveLength(5)
    })
    expect(await screen.findByTestId('console-online')).toBeInTheDocument()
    expect(screen.queryByTestId('console-fault')).not.toBeInTheDocument()
    // R2b-fe-4: one clean boot, which this panel itself asked for. Nothing restarted.
    expect(screen.getByTestId('console-boot-count')).toHaveTextContent(
      'Boot 1 · reset: power-on (by this panel)',
    )
    expect(screen.queryByTestId('console-restarts')).not.toBeInTheDocument()
    expect(screen.queryByTestId('console-reboot-loop')).not.toBeInTheDocument()
  })

  it('shows a board on the fleet when SNTP timed out but enrolment worked (S0-bug-1)', async () => {
    const { factory } = fakeConsole(BENCH_2026_10_04)
    render(<BoardConsolePanel autoWatch createConsole={factory} />)

    expect(await screen.findByTestId('console-online')).toBeInTheDocument()
    const milestones = screen.getByTestId('boot-milestones')
    const clock = milestones.querySelector('li[data-state="skipped"]')
    expect(clock).toHaveTextContent('Clock set')
    expect(milestones.querySelector('[data-state="waiting"]')).toBeNull()
  })

  it('names the cause when the board goes silent', async () => {
    const { factory } = fakeConsole(SILENT_BOARD)
    render(<BoardConsolePanel autoWatch createConsole={factory} />)

    const fault = await screen.findByTestId('console-fault')
    // The whole reason this task exists: "reason 201" is not a diagnosis, this is.
    expect(fault).toHaveTextContent(/2\.4 GHz/)
    expect(fault).toHaveTextContent('disconnected (reason 201)')

    // And the checklist says how far it got, so the fault has somewhere to attach.
    const milestones = screen.getByTestId('boot-milestones')
    expect(milestones.querySelectorAll('[data-state="done"]')).toHaveLength(1)
    expect(milestones.querySelector('[data-state="waiting"]')).toHaveTextContent('Network up')
    expect(screen.queryByTestId('console-online')).not.toBeInTheDocument()
  })

  // S0-fe-4, and the criterion the task is judged on: the same board, the same log, the
  // panel the operator was actually looking at on 2026-09-11.
  it('diagnoses the brownout loop of 2026-09-11 without anyone reading the log', async () => {
    const { factory } = fakeConsole(BENCH_2026_09_11)
    render(<BoardConsolePanel autoWatch createConsole={factory} />)

    const fault = await screen.findByTestId('console-fault')
    expect(fault).toHaveTextContent(/rail collapsed during radio calibration/)
    // Names the stage, not a guess at the cause: this board dies in `phy_init` every
    // boot and never reaches association, so "before it can join" was the wrong story.
    expect(fault).toHaveTextContent(/before this board ever tried to join/)
    expect(fault).toHaveTextContent(/bulk capacitor/)

    const loop = await screen.findByTestId('console-reboot-loop')
    expect(loop).toHaveTextContent(/keeps restarting — rebooted 2×/)
    expect(loop).toHaveTextContent(/brownout/)
    // And it does NOT second-guess the fault above it. This banner used to recommend a
    // shorter, thicker cable unconditionally, one line under a fault that already named
    // the cause off the log — which is how an operator with a perfectly good cable came
    // to spend an evening swapping cables and USB ports. When a cause is named, the
    // guess stays off the screen.
    expect(loop).not.toHaveTextContent(/cable/)
    expect(loop).not.toHaveTextContent(/hub/)
    expect(loop).toHaveTextContent(/cause named above/)

    // The stale ✓ that sent the diagnosis the wrong way for most of that session.
    const milestones = screen.getByTestId('boot-milestones')
    await waitFor(() => {
      expect(milestones.querySelectorAll('[data-state="done"]')).toHaveLength(1)
    })
    expect(milestones.querySelector('[data-state="waiting"]')).toHaveTextContent('Network up')
    expect(screen.queryByTestId('console-online')).not.toBeInTheDocument()
  })

  // R2b-fe-4: the count, the reason, and what the board had reached being taken away.
  it('a board that reboots during watch shows the count, the reason and retracts what it had reached', async () => {
    const { factory } = fakeConsole(REBOOT_DURING_WATCH)
    render(<BoardConsolePanel autoWatch createConsole={factory} />)

    // The panel's own S0-fe-5 notice makes boot 1 commanded: boots is still 4, restarts 3.
    const loop = await screen.findByTestId('console-reboot-loop')
    await waitFor(() => {
      expect(loop).toHaveTextContent('This board keeps restarting — rebooted 3×: brownout.')
    })
    expect(screen.queryByTestId('console-restarts')).not.toBeInTheDocument()
    expect(screen.getByTestId('console-boot-count')).toHaveTextContent('Boot 4 · reset: brownout')

    const milestones = screen.getByTestId('boot-milestones')
    const retracted = milestones.querySelectorAll('li[data-state="retracted"]')
    expect(retracted).toHaveLength(1)
    expect(retracted[0]).toHaveTextContent('Clock set')
    expect(retracted[0]).toHaveTextContent('lost at the restart')
    const waiting = milestones.querySelector('li[data-state="waiting"]')
    expect(waiting).toHaveTextContent('Network up')
    expect(waiting).toHaveAttribute('data-retracted', 'true')
    expect(milestones.querySelectorAll('[data-state="done"]')).toHaveLength(1)
  })

  it('a board that reaches the fleet, panics and comes back says so, without a loop banner', async () => {
    const { factory } = fakeConsole([
      ...HAPPY,
      "E (9000) task_wdt: Guru Meditation Error: Core  0 panic'ed (LoadProhibited).",
      'rst:0xc (SW_CPU_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
      'I (100) ff-agent: fleetforge agent 0.1.0 (idf v5.5.5), built Sep 10 2026 00:00:00',
    ])
    render(<BoardConsolePanel autoWatch createConsole={factory} />)

    const restarts = await screen.findByTestId('console-restarts')
    expect(restarts).toHaveTextContent('Rebooted 1× after reaching the fleet: panic.')
    expect(screen.queryByTestId('console-reboot-loop')).not.toBeInTheDocument()
  })

  it('guesses at power only when the log names no cause at all', async () => {
    const { factory } = fakeConsole(UNEXPLAINED_LOOP)
    render(<BoardConsolePanel autoWatch createConsole={factory} />)

    const loop = await screen.findByTestId('console-reboot-loop')
    expect(loop).toHaveTextContent(/keeps restarting — rebooted 2×/)
    expect(loop).toHaveTextContent(/power-on/)
    expect(screen.queryByTestId('console-fault')).not.toBeInTheDocument()
    // Nothing better is on screen, so the guess earns its place — but it stops short of
    // blaming the cable outright, because that is the advice that wasted the evening.
    expect(loop).toHaveTextContent(/shorter, thicker/)
    expect(loop).toHaveTextContent(/suspect the board’s own supply/)
  })

  it('never spins forever: a milestone past its deadline says so on its own', async () => {
    vi.useFakeTimers()
    try {
      const { factory } = fakeConsole(SILENT_BOARD)
      render(<BoardConsolePanel autoWatch createConsole={factory} />)
      // No further lines arrive — that IS the stalled case. Only the clock moves.
      await act(() => vi.advanceTimersByTimeAsync(MILESTONE_DEADLINE_MS.link + 2_000))

      const overdue = screen.getByTestId('console-overdue')
      expect(overdue).toHaveTextContent(/Network up has not happened in \d+ s/)
      expect(overdue).toHaveTextContent(/5 GHz/)
    } finally {
      vi.useRealTimers()
    }
  })

  it('hands the port back when released, and says so', async () => {
    const user = userEvent.setup()
    const { factory, state } = fakeConsole(HAPPY)
    render(<BoardConsolePanel autoWatch createConsole={factory} />)
    await screen.findByTestId('board-console')

    await user.click(screen.getByRole('button', { name: /release the port/i }))

    await waitFor(() => expect(state.closed).toBe(1))
    expect(await screen.findByRole('button', { name: /watch a board/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /release the port/i })).not.toBeInTheDocument()
  })

  it('never holds two ports: watching again releases the first', async () => {
    const user = userEvent.setup()
    const { factory, state } = fakeConsole(HAPPY)
    render(<BoardConsolePanel autoWatch createConsole={factory} />)
    await waitFor(() => expect(state.opened).toBe(1))

    await user.click(screen.getByRole('button', { name: /release the port/i }))
    await user.click(screen.getByRole('button', { name: /watch a board/i }))

    await waitFor(() => expect(state.opened).toBe(2))
    // Manual watch shows the chooser; the automatic one after a flash must not.
    expect(state.acquires).toEqual(['granted', 'prompt'])
    expect(state.closed).toBe(1)
  })

  it('gives the port back on unmount', async () => {
    const { factory, state } = fakeConsole(HAPPY)
    const view = render(<BoardConsolePanel autoWatch createConsole={factory} />)
    await waitFor(() => expect(state.opened).toBe(1))

    view.unmount()

    // Otherwise the device stays ours until the tab closes, and the operator's next
    // `screen` fails with "Resource busy" for no visible reason.
    await waitFor(() => expect(state.closed).toBe(1))
  })

  it('pulses EN on demand, so a boot log can be read from its first line', async () => {
    const user = userEvent.setup()
    const { factory, state } = fakeConsole(HAPPY)
    render(<BoardConsolePanel autoWatch createConsole={factory} />)
    await screen.findByTestId('board-console')

    // S0-fe-5: autoWatch now auto-pulses once, so the click makes it 2.
    const rebootsBefore = state.reboots
    await user.click(screen.getByRole('button', { name: /reboot the board/i }))
    expect(state.reboots).toBe(rebootsBefore + 1)
  })

  it('does not touch the port until asked, when there was no flash', async () => {
    const factory = vi.fn<ConsoleFactory>()
    render(<BoardConsolePanel autoWatch={false} createConsole={factory} />)

    expect(factory).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /watch a board/i })).toBeInTheDocument()
    expect(screen.getByText(/the port is free/i)).toBeInTheDocument()
  })

  it('explains a refused port instead of showing an empty panel', async () => {
    const factory: ConsoleFactory = async () => {
      throw Object.assign(new Error('No port selected by the user.'), { name: 'NotFoundError' })
    }
    render(<BoardConsolePanel autoWatch createConsole={factory} />)

    // `explainFlashError` turns Chromium's NotFoundError into something readable.
    expect(await screen.findByText('No board selected.')).toBeInTheDocument()
  })

  // ── S0-fe-5: the boot happens with no operator action ───────────────────────────────
  describe('automatic reset so the log starts at the top', () => {
    it('a silent board becomes a boot log with zero clicks', async () => {
      // The board emits NOTHING until EN is pulsed. The log exists only because the panel
      // reset it. This is the acceptance: S0-fe-5's task line in one test.
      const { factory, state } = fakeConsole(HAPPY, { silentUntilReset: true })
      render(<BoardConsolePanel autoWatch createConsole={factory} />)

      const console_ = await screen.findByTestId('board-console')
      await waitFor(() => {
        expect(console_).toHaveTextContent('POWERON_RESET')
        expect(console_).toHaveTextContent('enroll 200')
      })

      const milestones = screen.getByTestId('boot-milestones')
      await waitFor(() => {
        expect(milestones.querySelectorAll('[data-state="done"]')).toHaveLength(5)
      })
      expect(state.reboots).toBe(1)
    })

    it('an empty log is visibly an empty log, not an absent one', async () => {
      const { factory } = fakeConsole([], { silentUntilReset: true })
      render(<BoardConsolePanel autoWatch createConsole={factory} />)

      const console_ = await screen.findByTestId('board-console')
      expect(console_).toBeInTheDocument()
      expect(await screen.findByTestId('board-console-waiting')).toHaveTextContent(
        /waiting for the first line from the board/i,
      )
      expect(screen.queryByText(/the port is free/i)).not.toBeInTheDocument()
    })

    it('does not cry wolf on the happy path', async () => {
      // The panel just reset the board, which is a second boot — but it is commanded, so
      // the loop must not fire.
      const { factory } = fakeConsole(HAPPY, { silentUntilReset: true })
      render(<BoardConsolePanel autoWatch createConsole={factory} />)

      await waitFor(() => {
        expect(screen.getByTestId('board-console')).toHaveTextContent('enroll 200')
      })
      expect(screen.queryByTestId('console-reboot-loop')).not.toBeInTheDocument()
    })

    it('a failed pulse does not block the panel, and says so in the log', async () => {
      const { factory, state } = fakeConsole(HAPPY, { rebootRejects: true })
      render(<BoardConsolePanel autoWatch createConsole={factory} />)

      const console_ = await screen.findByTestId('board-console')
      await waitFor(() => {
        expect(console_).toHaveTextContent('could not reset the board')
      })
      expect(state.reboots).toBe(1)
      expect(screen.queryByTestId('console-fault')).not.toBeInTheDocument()
    })

    it('the manual watch path pulses too', async () => {
      const user = userEvent.setup()
      const { factory, state } = fakeConsole(HAPPY)
      render(<BoardConsolePanel autoWatch createConsole={factory} />)
      await screen.findByTestId('board-console')

      await user.click(screen.getByRole('button', { name: /release the port/i }))
      await user.click(screen.getByRole('button', { name: /watch a board/i }))

      await waitFor(() => expect(state.reboots).toBe(2))
    })
  })

  // ── S0-fe-6: where the remedy is software, the panel offers it ──────────────────────
  //
  // The task line: the operator presses one button instead of performing three manual
  // steps. The negative half matters just as much — a fault with no software remedy must
  // render NO button rather than one that cannot work.
  describe('the fix is a button, not an instruction', () => {
    it('offers a re-flash for a spent token, and releases the port before running it', async () => {
      const user = userEvent.setup()
      const { factory, state } = fakeConsole(SPENT_TOKEN)
      const onReflash = vi.fn(async () => {})
      render(<BoardConsolePanel autoWatch createConsole={factory} onReflash={onReflash} />)

      const fault = await screen.findByTestId('console-fault')
      expect(fault).toHaveTextContent(/single-use/)

      const button = await screen.findByRole('button', { name: /re-flash the board/i })
      await user.click(button)

      // esptool opens the same physical device, so the console must let go FIRST or the
      // recovery flash dies with "the port is already open".
      await waitFor(() => expect(state.closed).toBe(1))
      expect(onReflash).toHaveBeenCalledTimes(1)
    })

    it('explains why it cannot re-flash rather than showing a dead button', async () => {
      const { factory } = fakeConsole(SPENT_TOKEN)
      render(
        <BoardConsolePanel
          autoWatch
          createConsole={factory}
          reflashBlockedReason="Fill in the network details in step 2 to re-flash from here."
        />,
      )

      await screen.findByTestId('console-fault')
      expect(await screen.findByTestId('console-remedy-blocked')).toHaveTextContent(
        /network details in step 2/i,
      )
      expect(screen.queryByTestId('console-remedy')).not.toBeInTheDocument()
    })

    // The acceptance's second half, on the log that started this whole feature.
    it('offers NO button for the 2026-09-11 brownout — no power rail is fixed in software', async () => {
      const { factory } = fakeConsole(BENCH_2026_09_11)
      const onReflash = vi.fn(async () => {})
      render(<BoardConsolePanel autoWatch createConsole={factory} onReflash={onReflash} />)

      const fault = await screen.findByTestId('console-fault')
      expect(fault).toHaveTextContent(/rail collapsed during radio calibration/)
      expect(screen.queryByTestId('console-remedy')).not.toBeInTheDocument()
      expect(onReflash).not.toHaveBeenCalled()
    })

    it('offers a reboot — not a re-flash — for a board stuck in its ROM loader', async () => {
      const user = userEvent.setup()
      const { factory, state } = fakeConsole([
        'rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)',
        'waiting for download',
      ])
      const onReflash = vi.fn(async () => {})
      render(<BoardConsolePanel autoWatch createConsole={factory} onReflash={onReflash} />)

      await screen.findByTestId('console-fault')
      const rebootsBefore = state.reboots
      await user.click(await screen.findByRole('button', { name: /reboot and retry/i }))

      expect(state.reboots).toBe(rebootsBefore + 1)
      expect(onReflash).not.toHaveBeenCalled()
    })

    it('renders exactly one action when the board is both faulted and overdue', async () => {
      vi.useFakeTimers()
      try {
        const { factory } = fakeConsole(SPENT_TOKEN)
        const onReflash = vi.fn(async () => {})
        render(<BoardConsolePanel autoWatch createConsole={factory} onReflash={onReflash} />)
        await act(() => vi.advanceTimersByTimeAsync(MILESTONE_DEADLINE_MS.enroll + 2_000))

        // Both the fault and the stalled `enroll` milestone want a re-flash. Two identical
        // buttons would be confusing and an ambiguous `getByRole` in every future test.
        expect(screen.getByTestId('console-overdue')).toBeInTheDocument()
        expect(screen.getAllByTestId('console-remedy')).toHaveLength(1)
      } finally {
        vi.useRealTimers()
      }
    })
  })
})

/**
 * S0-fe-7 — the escalation path.
 *
 * On 2026-09-11 the log WAS on screen and the operator still could not get it out: the
 * only affordance was selecting text inside an unlabelled `<pre>`. These two cases are
 * the whole fix — one click copies, and a clipboard the browser refuses still leaves a
 * labelled, selectable box holding exactly what would have been copied.
 */
describe('BoardConsolePanel — the diagnostic bundle', () => {
  it('copies a diagnostic bundle in one click', async () => {
    const user = userEvent.setup()
    const writeText = vi.fn().mockResolvedValue(undefined)
    // The repo's clipboard idiom (`EnrollBoard.test.tsx`): jsdom has no clipboard, and
    // there is deliberately no injected seam for one. `defineProperty` rather than
    // `Object.assign` because `userEvent.setup()` has already installed its own stub
    // behind a getter.
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })

    const { factory } = fakeConsole(BENCH_2026_09_11)
    render(<BoardConsolePanel autoWatch createConsole={factory} />)
    await screen.findByTestId('console-fault')

    await user.click(screen.getByRole('button', { name: /copy diagnostic bundle/i }))

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    const copied = writeText.mock.calls[0][0] as string
    expect(copied).toContain('E BOD:')
    expect(copied).toContain('rail collapsed during radio calibration')
    // What was copied is what is on screen — not a re-derivation that drifts with the
    // 1 Hz summary tick while the operator is still reading it.
    expect(screen.getByTestId('diagnostic-bundle')).toHaveValue(copied)
    expect(await screen.findByRole('button', { name: /copied/i })).toBeInTheDocument()
  })

  it('leaves the bundle on screen when the browser refuses the clipboard', async () => {
    const user = userEvent.setup()
    const writeText = vi.fn().mockRejectedValue(new Error('write permission denied'))
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })

    const { factory } = fakeConsole(BENCH_2026_09_11)
    render(<BoardConsolePanel autoWatch createConsole={factory} />)
    await screen.findByTestId('console-fault')

    await user.click(screen.getByRole('button', { name: /copy diagnostic bundle/i }))

    // The 2026-09-11 dead end, and the reason this must never be an error state.
    const box = await screen.findByTestId('diagnostic-bundle')
    expect((box as HTMLTextAreaElement).value).toContain('E BOD:')
    expect(screen.getByText(/would not give this page the clipboard/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /copied/i })).not.toBeInTheDocument()
  })
})

// R2b-fe-3 — one card, success or failure. A failure names ONE cause and holds the ONE
// action and the ONE copy click; the watch paragraphs above it keep the long story.
describe('BoardConsolePanel — the result card (R2b-fe-3)', () => {
  it('shows a success card with the facts the console gave', async () => {
    const { factory } = fakeConsole(HAPPY)
    render(<BoardConsolePanel autoWatch createConsole={factory} />)

    const card = await screen.findByTestId('result-card')
    await waitFor(() => expect(card).toHaveAttribute('data-outcome', 'success'))
    expect(within(card).getByTestId('console-online')).toBeInTheDocument()
    expect(card).toHaveTextContent('a4cf12b3de90')
    expect(card).toHaveTextContent('NTP (pool.ntp.org)')
  })

  it('a spent token: one cause, one remedy and one copy click, all inside the card', async () => {
    const { factory } = fakeConsole(SPENT_TOKEN)
    const onReflash = vi.fn(async () => {})
    render(<BoardConsolePanel autoWatch createConsole={factory} onReflash={onReflash} />)

    const card = await screen.findByTestId('result-card')
    await waitFor(() => expect(card).toHaveAttribute('data-outcome', 'failure'))
    expect(within(card).getByTestId('result-headline')).toHaveTextContent(/^Enrolment refused/)
    expect(screen.getAllByTestId('console-remedy')).toHaveLength(1)
    expect(within(card).getByRole('button', { name: /re-flash the board/i })).toBe(
      screen.getByTestId('console-remedy'),
    )
    const copies = screen.getAllByRole('button', { name: /copy diagnostic bundle/i })
    expect(copies).toHaveLength(1)
    expect(card).toContainElement(copies[0])
    // The long story stays in the watch paragraph, with no button of its own.
    expect(screen.getByTestId('console-fault')).toHaveTextContent(/single-use/)
    expect(within(screen.getByTestId('console-fault')).queryByRole('button')).toBeNull()
  })

  it('the 2026-09-11 brownout: a power headline and a text next action, no button', async () => {
    const { factory } = fakeConsole(BENCH_2026_09_11)
    const onReflash = vi.fn(async () => {})
    render(<BoardConsolePanel autoWatch createConsole={factory} onReflash={onReflash} />)

    const card = await screen.findByTestId('result-card')
    await waitFor(() => expect(card).toHaveAttribute('data-outcome', 'failure'))
    expect(within(card).getByTestId('result-headline')).toHaveTextContent(/^Power/)
    expect(within(card).getByTestId('result-next')).toHaveTextContent(CAUSE_NEXT.power)
    expect(screen.queryByTestId('console-remedy')).not.toBeInTheDocument()
  })

  it('a silent Wi-Fi board past the link deadline gets a Wi-Fi headline', async () => {
    vi.useFakeTimers()
    try {
      const { factory } = fakeConsole(SILENT_BOARD)
      render(<BoardConsolePanel autoWatch createConsole={factory} />)
      // Before the deadline: still progressing, so the checklist is the view.
      await act(() => vi.advanceTimersByTimeAsync(1_000))
      expect(screen.queryByTestId('result-card')).not.toBeInTheDocument()

      await act(() => vi.advanceTimersByTimeAsync(MILESTONE_DEADLINE_MS.link + 2_000))
      const card = screen.getByTestId('result-card')
      expect(card).toHaveAttribute('data-outcome', 'failure')
      expect(within(card).getByTestId('result-headline')).toHaveTextContent(/^Wi-Fi/)
      expect(within(card).getByTestId('result-next')).toHaveTextContent(CAUSE_NEXT.wifi)
      expect(screen.queryByTestId('console-remedy')).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('renders no card when the flasher is showing its own, and keeps the toolbar copy', async () => {
    const { factory } = fakeConsole(SPENT_TOKEN)
    render(<BoardConsolePanel autoWatch createConsole={factory} hideResult />)

    await screen.findByTestId('console-fault')
    expect(screen.queryByTestId('result-card')).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /copy diagnostic bundle/i })).toHaveLength(1)
  })
})

// ── R2b-fe-5 ────────────────────────────────────────────────────────────────────────────
//
// A native-USB board re-enumerates when it is reset, so the console can lose the port while
// the board enrols perfectly well. The server's device list is the truth either way.
describe('BoardConsolePanel — the console and the server together (R2b-fe-5)', () => {
  const ID = 'a4cf12b3de90'
  const E0 = '2026-10-04T10:00:00Z'
  const E1 = '2026-10-04T12:00:00Z'
  /** The exact `serialConsole.ts` error after its 8 s `getPorts()` window. */
  const NO_BOARD =
    'No board is available to watch. Plug it back in, then use \u201cWatch a board\u201d to pick the port.'

  const row = (over: Partial<DeviceSummary> = {}): DeviceSummary => ({
    device_id: ID,
    name: null,
    group_id: null,
    platform_type: 'esp32',
    fw_version: '0.4.0',
    agent_version: '0.4.0',
    link_type: 'wifi',
    ssid: null,
    known_networks: null,
    power_class: 'always_on',
    expected_wake_interval_s: null,
    parent_device_id: null,
    partition_layout: 'ab-4m-v1',
    ota_slot_size: 1966080,
    capabilities: ['ota'],
    last_seen: null,
    enrolled_at: E1,
    broker_provisioned_at: null,
    online: false,
    deploy: null,
    ...over,
  })
  const enrolledRow = row({ broker_provisioned_at: '2026-10-04T12:00:01Z' })
  const onlineRow = { ...enrolledRow, online: true, last_seen: '2026-10-04T12:00:09Z' }

  const context = (
    devices: DeviceSummary[] | null,
    flashBaseline: FleetBaseline | null = { rows: {} },
    now?: number,
  ): ResultContext => ({
    devices,
    versions: null,
    flashed: { deviceId: ID, agentVersion: '0.4.0', layout: 'ab-4m-v1', link: 'wifi', ssid: null },
    flashBaseline,
    now,
  })

  const failingAcquire: ConsoleFactory = async () => {
    throw new Error(NO_BOARD)
  }

  /** Opens, yields the first lines, then the stream ENDS: the board dropped off the bus. */
  function droppingConsole(script: string[]): ConsoleFactory {
    return async () => ({
      async *lines() {
        for (const line of script) yield line
      },
      async reboot() {},
      async close() {},
    })
  }

  const item = (label: string) =>
    within(screen.getByTestId('boot-milestones')).getByText(label).closest('li') as HTMLElement

  it('acquire fails after flashing a new board: the server drives it to the success card', async () => {
    const { rerender } = render(
      <BoardConsolePanel autoWatch createConsole={failingAcquire} result={context([])} />,
    )
    const view = await screen.findByTestId('console-server-view')
    expect(view).toHaveTextContent(`Watching the server for ${ID}`)
    expect(screen.queryByTestId('result-card')).not.toBeInTheDocument()
    expect(screen.getByText(NO_BOARD)).toHaveClass('warn')

    rerender(
      <BoardConsolePanel autoWatch createConsole={failingAcquire} result={context([enrolledRow])} />,
    )
    expect(item('Enrolled')).toHaveAttribute('data-state', 'done')
    expect(item('Enrolled')).toHaveAttribute('data-source', 'server')
    expect(item('Enrolled')).toHaveTextContent('from the server')
    expect(item('On the fleet')).toHaveAttribute('data-state', 'waiting')
    expect(screen.getByTestId('console-server-view')).toHaveTextContent('has enrolled')
    expect(screen.queryByTestId('result-card')).not.toBeInTheDocument()

    rerender(
      <BoardConsolePanel autoWatch createConsole={failingAcquire} result={context([onlineRow])} />,
    )
    const card = screen.getByTestId('result-card')
    expect(card).toHaveAttribute('data-outcome', 'success')
    expect(within(card).getByTestId('console-online')).toBeInTheDocument()
    const fleetRow = within(card).getByText('On the fleet').nextElementSibling
    expect(fleetRow).toHaveTextContent('the console did not see it')
    expect(item('On the fleet')).toHaveAttribute('data-source', 'server')
    // The text is unchanged (Check F matches on it), muted now the board is evidently fine.
    expect(screen.getByText(NO_BOARD)).toHaveClass('muted')
  })

  it('the stream ends after the EN pulse: the drop text stays, the server finishes the job', async () => {
    const factory = droppingConsole(HAPPY.slice(0, 3))
    const { rerender } = render(
      <BoardConsolePanel autoWatch createConsole={factory} result={context([])} />,
    )
    const drop = await screen.findByText(/dropped off the USB bus/)
    expect(drop).toHaveClass('warn')
    expect(screen.getByTestId('console-server-view')).toBeInTheDocument()
    expect(screen.getByTestId('boot-milestones')).toBeInTheDocument()

    rerender(
      <BoardConsolePanel autoWatch createConsole={factory} result={context([enrolledRow])} />,
    )
    expect(screen.getByText(/dropped off the USB bus/)).toHaveClass('muted')

    rerender(<BoardConsolePanel autoWatch createConsole={factory} result={context([onlineRow])} />)
    expect(screen.getByTestId('result-card')).toHaveAttribute('data-outcome', 'success')
    expect(screen.getByText(/dropped off the USB bus/)).toHaveClass('muted')
    expect(item('Agent running')).not.toHaveAttribute('data-source')
    expect(item('Enrolled')).toHaveAttribute('data-source', 'server')
  })

  it('re-flashing a known online board: its stale row marks nothing until it re-enrols', async () => {
    const stale = row({
      enrolled_at: E0,
      broker_provisioned_at: '2026-10-04T10:00:01Z',
      online: true,
      last_seen: '2026-10-04T11:59:00Z',
    })
    const baseline = takeBaseline([stale])
    const { rerender } = render(
      <BoardConsolePanel
        autoWatch
        createConsole={failingAcquire}
        result={context([stale], baseline)}
      />,
    )
    await screen.findByTestId('console-server-view')
    expect(document.querySelector('[data-source="server"]')).toBeNull()
    expect(screen.queryByTestId('result-card')).not.toBeInTheDocument()
    expect(screen.getByTestId('console-server-view')).toHaveTextContent('not enrolled yet')

    // Re-enrolled, but last_seen still predates the new enrolment: Enrolled, not On the fleet.
    const reEnrolled = { ...stale, enrolled_at: E1, broker_provisioned_at: '2026-10-04T12:00:01Z' }
    rerender(
      <BoardConsolePanel
        autoWatch
        createConsole={failingAcquire}
        result={context([reEnrolled], baseline)}
      />,
    )
    expect(item('Enrolled')).toHaveAttribute('data-source', 'server')
    expect(item('On the fleet')).not.toHaveAttribute('data-source')
    expect(screen.queryByTestId('result-card')).not.toBeInTheDocument()

    rerender(
      <BoardConsolePanel
        autoWatch
        createConsole={failingAcquire}
        result={context([{ ...reEnrolled, last_seen: '2026-10-04T12:00:09Z' }], baseline)}
      />,
    )
    expect(screen.getByTestId('result-card')).toHaveAttribute('data-outcome', 'success')
  })

  it('says so after the server wait, and never before (no unbounded wait)', async () => {
    const { rerender } = render(
      <BoardConsolePanel
        autoWatch
        createConsole={failingAcquire}
        result={context([], { rows: {} }, Date.now())}
      />,
    )
    await screen.findByTestId('console-server-view')
    const stoppedBy = Date.now()
    rerender(
      <BoardConsolePanel
        autoWatch
        createConsole={failingAcquire}
        result={context([], { rows: {} }, stoppedBy + SERVER_WAIT_MS - 5_000)}
      />,
    )
    expect(screen.queryByTestId('console-server-overdue')).not.toBeInTheDocument()

    rerender(
      <BoardConsolePanel
        autoWatch
        createConsole={failingAcquire}
        result={context([], { rows: {} }, stoppedBy + SERVER_WAIT_MS + 1_000)}
      />,
    )
    expect(screen.getByTestId('console-server-overdue')).toHaveTextContent(
      `The server has not seen ${ID} on the fleet in 90 s.`,
    )
    expect(screen.queryByTestId('result-card')).not.toBeInTheDocument()
  })

  it('a manual watch with no flash: On the fleet from the server once last_seen moves', async () => {
    const user = userEvent.setup()
    const held = row({
      enrolled_at: E0,
      broker_provisioned_at: '2026-10-04T10:00:01Z',
      online: true,
      last_seen: '2026-10-04T11:00:00Z',
    })
    const factory = droppingConsole(HAPPY.slice(0, 3))
    const manual = (devices: DeviceSummary[]): ResultContext => ({
      devices,
      versions: null,
      flashed: null,
    })
    const { rerender } = render(
      <BoardConsolePanel autoWatch={false} createConsole={factory} result={manual([held])} />,
    )
    await user.click(screen.getByRole('button', { name: 'Watch a board' }))
    await screen.findByText(/dropped off the USB bus/)
    // A held credential is enrolled; the unchanged last_seen is not proof of this session.
    expect(item('Enrolled')).toHaveAttribute('data-source', 'server')
    expect(item('On the fleet')).toHaveAttribute('data-state', 'waiting')

    // Watching again keeps the first baseline.
    rerender(
      <BoardConsolePanel
        autoWatch={false}
        createConsole={factory}
        result={manual([{ ...held, last_seen: '2026-10-04T11:00:20Z' }])}
      />,
    )
    expect(item('On the fleet')).toHaveAttribute('data-source', 'server')
    expect(screen.getByTestId('result-card')).toHaveAttribute('data-outcome', 'success')
    await user.click(screen.getByRole('button', { name: 'Watch a board' }))
    await waitFor(() =>
      expect(screen.getByTestId('result-card')).toHaveAttribute('data-outcome', 'success'),
    )
  })

  it('with no fleet the panel is the console alone, as before', async () => {
    render(<BoardConsolePanel autoWatch createConsole={failingAcquire} />)
    await screen.findByText(NO_BOARD)
    expect(screen.queryByTestId('console-server-view')).not.toBeInTheDocument()
    expect(screen.queryByTestId('boot-milestones')).not.toBeInTheDocument()
  })
})

// ── R2b-fe-6 ────────────────────────────────────────────────────────────────────────────
//
// The last line of Flow 1: name the board from the success card. Only with the `naming`
// capability and a fleet row for the board; the body is `{"name": ...}` and never a group.
describe('BoardConsolePanel — name the board (R2b-fe-6)', () => {
  const ID = 'a4cf12b3de90'

  const row = (over: Partial<DeviceSummary> = {}): DeviceSummary => ({
    device_id: ID,
    name: null,
    group_id: null,
    platform_type: 'esp32',
    fw_version: '0.1.0',
    agent_version: '0.1.0',
    link_type: 'wifi',
    ssid: null,
    known_networks: null,
    power_class: 'always_on',
    expected_wake_interval_s: null,
    parent_device_id: null,
    partition_layout: 'ab-4m-v1',
    ota_slot_size: 1966080,
    capabilities: ['ota'],
    last_seen: '2026-10-04T12:00:09Z',
    enrolled_at: '2026-10-04T12:00:00Z',
    broker_provisioned_at: '2026-10-04T12:00:01Z',
    online: true,
    deploy: null,
    ...over,
  })

  const ctx = (devices: DeviceSummary[] | null): ResultContext => ({
    devices,
    versions: null,
    flashed: null,
    flashBaseline: null,
  })

  const json = (status: number, body: unknown) =>
    new Response(JSON.stringify(body), { status })

  function setup(
    options: {
      script?: string[]
      devices?: DeviceSummary[] | null
      naming?: boolean
    } = {},
  ) {
    const { factory } = fakeConsole(options.script ?? HAPPY)
    const onSessionExpired = vi.fn()
    const onSaved = vi.fn()
    const naming = options.naming === false ? undefined : { onSessionExpired, onSaved }
    const devices = options.devices === undefined ? [row()] : options.devices
    const view = (d: DeviceSummary[] | null) => (
      <BoardConsolePanel autoWatch createConsole={factory} result={ctx(d)} naming={naming} />
    )
    const utils = render(view(devices))
    return { ...utils, view, onSessionExpired, onSaved }
  }

  const form = () => screen.findByTestId('name-board')
  const input = () => screen.getByLabelText('Board name') as HTMLInputElement
  const save = () => screen.getByRole('button', { name: 'Save name' })

  it('shows the form on the success card, last, empty for an unnamed board', async () => {
    setup()
    const card = await screen.findByTestId('result-card')
    await waitFor(() => expect(card).toHaveAttribute('data-outcome', 'success'))
    const named = await form()
    expect(card).toContainElement(named)
    expect(card.lastElementChild).toBe(named)
    expect(input().value).toBe('')
  })

  it('pre-fills the name of a board that already has one', async () => {
    setup({ devices: [row({ name: 'coop door' })] })
    await form()
    expect(input().value).toBe('coop door')
  })

  it('saves the trimmed name: one PATCH, only {"name"}, then the fleet is re-read', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(json(200, row({ name: 'coop door' })))
    const { onSaved } = setup()
    await form()

    await userEvent.type(input(), '  coop door  ')
    await userEvent.click(save())

    const message = await screen.findByTestId('name-board-message')
    expect(message).toHaveTextContent('Saved. The fleet table shows “coop door”')
    expect(message).toHaveAttribute('role', 'status')
    expect(fetchSpy).toHaveBeenCalledTimes(1)
    const [url, init] = fetchSpy.mock.calls[0]
    expect(url).toBe(`/v1/devices/${ID}`)
    expect(init?.method).toBe('PATCH')
    expect(init?.body).toBe('{"name":"coop door"}')
    expect(onSaved).toHaveBeenCalledTimes(1)
    expect(input().value).toBe('coop door')
  })

  it('clearing the field sends {"name":null}', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(200, row()))
    setup({ devices: [row({ name: 'coop door' })] })
    await form()

    await userEvent.clear(input())
    await userEvent.click(save())

    expect(await screen.findByTestId('name-board-message')).toHaveTextContent('Name cleared')
    expect(fetchSpy.mock.calls[0][1]?.body).toBe('{"name":null}')
    expect(input().value).toBe('')
  })

  it('shows a 409 verbatim, keeps the draft and does not re-read the fleet', async () => {
    const detail = 'name already used by device a4cf12b3de91: hen house'
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(409, { detail }))
    const { onSaved } = setup()
    await form()

    await userEvent.type(input(), 'hen house')
    await userEvent.click(save())

    expect(await screen.findByTestId('name-board-message')).toHaveTextContent(detail)
    expect(screen.getByTestId('name-board-message')).toHaveClass('bad')
    expect(onSaved).not.toHaveBeenCalled()
    expect(input().value).toBe('hen house')
  })

  it('a 401 drops to the login gate and shows nothing', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(401, { detail: 'not authenticated' }))
    const { onSessionExpired, onSaved } = setup()
    await form()

    await userEvent.type(input(), 'coop door')
    await userEvent.click(save())

    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1))
    expect(screen.queryByTestId('name-board-message')).not.toBeInTheDocument()
    expect(onSaved).not.toHaveBeenCalled()
  })

  it('a MAC-shaped or over-long name is refused on the client with no request', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    setup()
    await form()

    await userEvent.type(input(), 'A4CF12B3DE91')
    await userEvent.click(save())
    expect(await screen.findByTestId('name-board-message')).toHaveTextContent(
      'cannot look like a device id',
    )

    await userEvent.clear(input())
    await userEvent.paste('a'.repeat(65))
    await userEvent.click(save())
    expect(screen.getByTestId('name-board-message')).toHaveTextContent(
      'a name is at most 64 characters',
    )
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('a double click while the request is pending sends one PATCH', async () => {
    let resolve: (r: Response) => void = () => {}
    const pending = new Promise<Response>((r) => {
      resolve = r
    })
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockReturnValue(pending)
    setup()
    await form()

    await userEvent.type(input(), 'coop door')
    await userEvent.dblClick(save())

    const busy = screen.getByRole('button', { name: 'Saving…' })
    expect(busy).toBeDisabled()
    expect(fetchSpy).toHaveBeenCalledTimes(1)

    resolve(json(200, row({ name: 'coop door' })))
    expect(await screen.findByRole('button', { name: 'Save name' })).toBeEnabled()
  })

  it('renders no form without the naming capability', async () => {
    setup({ naming: false })
    const card = await screen.findByTestId('result-card')
    await waitFor(() => expect(card).toHaveAttribute('data-outcome', 'success'))
    expect(screen.queryByTestId('name-board')).not.toBeInTheDocument()
  })

  it('renders no form when the fleet has no row for the board', async () => {
    setup({ devices: [row({ device_id: '0000000fe699' })] })
    const card = await screen.findByTestId('result-card')
    await waitFor(() => expect(card).toHaveAttribute('data-outcome', 'success'))
    expect(screen.queryByTestId('name-board')).not.toBeInTheDocument()
  })

  it('renders no form on a failure card, even with a row', async () => {
    setup({ script: SPENT_TOKEN })
    const card = await screen.findByTestId('result-card')
    await waitFor(() => expect(card).toHaveAttribute('data-outcome', 'failure'))
    expect(screen.queryByTestId('name-board')).not.toBeInTheDocument()
  })

  it('a re-read of the fleet never clobbers what is being typed', async () => {
    const { rerender, view } = setup()
    await form()
    await userEvent.type(input(), 'half typed')

    rerender(view([row({ name: 'someone else renamed it' })]))

    expect(input().value).toBe('half typed')
  })
})
