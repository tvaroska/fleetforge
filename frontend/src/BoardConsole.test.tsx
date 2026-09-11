// What this defends:
//
// 1. **The operator never has to reach for `screen`.** The panel opens the port, shows the
//    boot, and drives a checklist to "On the fleet".
// 2. **A board that stops short is diagnosed by name**, not left as silence.
// 3. **Release really releases.** If this regresses, the next `screen` gets "Resource
//    busy" and the operator blames their cable.
// 4. **One port at a time**, and the port goes back on unmount.

import { act, render, screen, waitFor } from '@testing-library/react'
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
    expect(fault).toHaveTextContent(/browning out/)
    expect(fault).toHaveTextContent(/not a hub/)

    const loop = await screen.findByTestId('console-reboot-loop')
    expect(loop).toHaveTextContent(/keeps restarting — 3 times/)

    // The stale ✓ that sent the diagnosis the wrong way for most of that session.
    const milestones = screen.getByTestId('boot-milestones')
    await waitFor(() => {
      expect(milestones.querySelectorAll('[data-state="done"]')).toHaveLength(1)
    })
    expect(milestones.querySelector('[data-state="waiting"]')).toHaveTextContent('Network up')
    expect(screen.queryByTestId('console-online')).not.toBeInTheDocument()
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
    it('offers NO button for the 2026-09-11 brownout — no cable is fixed in software', async () => {
      const { factory } = fakeConsole(BENCH_2026_09_11)
      const onReflash = vi.fn(async () => {})
      render(<BoardConsolePanel autoWatch createConsole={factory} onReflash={onReflash} />)

      const fault = await screen.findByTestId('console-fault')
      expect(fault).toHaveTextContent(/browning out/)
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
