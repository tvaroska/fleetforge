// What these tests defend (R2b-fe-9):
//
// 1. The six milestones come from the recorded steps, a later one implies the earlier
//    ones ("not reported", never pending), and offsets are server-time deltas.
// 2. Deadline and stall sentences appear only for the states and past the thresholds
//    the board's own rules define — exact text, because the wording is the feature.
// 3. `awaiting_safe_window` never gets a deadline or a stall, however long it waits.
// 4. Nothing renders `%`, `null`, `undefined` or `NaN`, and no elapsed time is negative.

import { describe, expect, it } from 'vitest'
import { type DeployStep, type DeploySummary } from './api'
import { deployTimeline, type DeployTimeline } from './deployTimeline'

const NOW = Date.parse('2026-09-10T12:00:00Z')
const at = (secondsAgo: number) => new Date(NOW - secondsAgo * 1000).toISOString()
const step = (state: string, secondsAgo: number): DeployStep => ({ state, at: at(secondsAgo) })

function deploy(steps: DeployStep[], overrides: Partial<DeploySummary> = {}): DeploySummary {
  const last = steps[steps.length - 1]
  return {
    cmd_id: 'cmd-a',
    state: last.state,
    at: last.at,
    is_terminal: false,
    artifact_version: '1.5.0',
    from_version: '1.4.2',
    pct: null,
    detail: null,
    steps,
    confirm_timeout_s: 300,
    ...overrides,
  }
}

const ONLINE = { online: true, fw_version: '1.4.2' }
const OFFLINE = { online: false, fw_version: '1.4.2' }

const seen: DeployTimeline[] = []
function timeline(d: DeploySummary, device = ONLINE, now = NOW): DeployTimeline {
  const result = deployTimeline(d, device, now)
  seen.push(result)
  return result
}
const statuses = (t: DeployTimeline) => t.items.map((i) => `${i.label}:${i.status}`)

const DOWNLOAD_DEADLINE =
  'The board says nothing more until the image is written. If data stops arriving, it gives up on its own 60 to 80 s after the last byte and reports “download stalled”.'

describe('deployTimeline — milestones', () => {
  it('shows a download in flight: sent done, downloading current, the rest pending', () => {
    const t = timeline(deploy([step('requested', 50), step('staging', 48), step('downloading', 42)]))

    expect(statuses(t)).toEqual([
      'sent:done',
      'downloading:current',
      'staged:pending',
      'rebooting:pending',
      'confirming:pending',
      'confirmed:pending',
    ])
    expect(t.items[0].offset).toBe('+0 s')
    // The milestone's time is its FIRST step (`staging`), not the latest one.
    expect(t.items[1].offset).toBe('+2 s')
    expect(t.elapsed).toBe('elapsed 50 s')
    expect(t.current).toBe('downloading the image for 42 s')
    expect(t.terminal).toBe(false)
  })

  it('marks a milestone with no step of its own as implied, never pending', () => {
    const confirming = timeline(
      deploy([step('requested', 60), step('downloading', 50), step('staged', 40), step('confirming', 5)]),
    )
    expect(statuses(confirming).slice(0, 5)).toEqual([
      'sent:done',
      'downloading:done',
      'staged:done',
      'rebooting:implied',
      'confirming:current',
    ])
    expect(confirming.items[3].offset).toBeNull()

    const confirmed = timeline(
      deploy([step('requested', 60), step('rebooting', 30), step('confirmed', 10)], { is_terminal: true }),
    )
    expect(statuses(confirmed)).toEqual([
      'sent:done',
      'downloading:implied',
      'staged:implied',
      'rebooting:done',
      'confirming:implied',
      'confirmed:done',
    ])
  })

  it('ends a confirmed deploy with all six done, "took", and nothing live', () => {
    const t = timeline(
      deploy(
        [
          step('requested', 100),
          step('staging', 100),
          step('downloading', 99),
          step('verifying', 80),
          step('staged', 79),
          step('applying', 79),
          step('rebooting', 79),
          step('confirming', 60),
          step('confirmed', 58),
        ],
        { is_terminal: true },
      ),
    )
    expect(t.items.map((i) => i.status)).toEqual(['done', 'done', 'done', 'done', 'done', 'done'])
    expect(t.items.map((i) => i.offset)).toEqual(['+0 s', '+0 s', '+21 s', '+21 s', '+40 s', '+42 s'])
    expect(t.elapsed).toBe('took 42 s')
    expect(t.current).toBeNull()
    expect(t.deadline).toBeNull()
    expect(t.stall).toBeNull()
  })

  it('ends a failed download with one ✗ item and nothing pending after it', () => {
    const t = timeline(
      deploy([step('requested', 200), step('downloading', 190), step('failed', 100)], {
        is_terminal: true,
        detail: 'download stalled',
      }),
    )
    expect(statuses(t)).toEqual(['sent:done', 'downloading:done', 'failed:failed'])
    expect(t.items[2].offset).toBe('+1 min 40 s')
    expect(t.deadline).toBeNull()
    expect(t.stall).toBeNull()
  })

  it('ends a rollback with the reached milestones and the rolled-back item', () => {
    const t = timeline(
      deploy(
        [step('requested', 400), step('staged', 390), step('rebooting', 389), step('rolled_back', 40)],
        { is_terminal: true },
      ),
    )
    expect(statuses(t)).toEqual([
      'sent:done',
      'downloading:implied',
      'staged:done',
      'rebooting:done',
      'rolled back to the previous version:failed',
    ])
    expect(t.items[4].offset).toBe('+6 min 0 s')
  })

  it('renders a state it has never heard of as itself, with no deadline', () => {
    const t = timeline(deploy([step('requested', 20), step('defragmenting', 10)]))
    expect(statuses(t)).toEqual(['sent:done', 'defragmenting:current'])
    expect(t.current).toBe('defragmenting for 10 s')
    expect(t.deadline).toBeNull()
    expect(t.stall).toBeNull()
  })

  it('falls back to the summary row when the API sends no steps', () => {
    const d = deploy([step('downloading', 12)])
    delete d.steps
    const t = timeline(d)
    expect(t.items[1]).toEqual({ key: 'downloading', label: 'downloading', status: 'current', offset: '+0 s' })
    expect(t.items[0].status).toBe('implied')
    expect(t.current).toBe('downloading the image for 12 s')
  })

  it('clamps a step stamped after the browser’s now at 0 s', () => {
    const t = timeline(deploy([step('requested', -5), step('staging', -6)]))
    expect(t.elapsed).toBe('elapsed 0 s')
    expect(t.current).toBe('starting for 0 s')
  })

  it('prints no offset for an unparseable time, never NaN', () => {
    const t = timeline(deploy([{ state: 'requested', at: 'garbage' }, step('staging', 3)]))
    expect(t.items[0].offset).toBeNull()
    expect(t.elapsed).toBe('elapsed 0 s')
  })
})

describe('deployTimeline — deadlines and stalls', () => {
  it('states the download rule, and calls a stall only past 80 s', () => {
    const at80 = timeline(deploy([step('requested', 85), step('downloading', 80)]))
    expect(at80.deadline).toBe(DOWNLOAD_DEADLINE)
    expect(at80.stall).toBeNull()

    const at81 = timeline(deploy([step('requested', 86), step('downloading', 81)]))
    expect(at81.deadline).toBe(DOWNLOAD_DEADLINE)
    expect(at81.stall).toBe(
      'No word from the board for 1 min 21 s. It may still be downloading: it gives up only when no data has arrived for 60 to 80 s, and then reports “download stalled”.',
    )

    const offline = timeline(deploy([step('requested', 200), step('downloading', 125)]), OFFLINE)
    expect(offline.stall).toBe(
      'No word from the board for 2 min 5 s. It may still be downloading: it gives up only when no data has arrived for 60 to 80 s, and then reports “download stalled”. The board is offline now; it reports what happened when it reconnects.',
    )
  })

  it('queues for an offline board forever, and nudges an online one after 30 s', () => {
    const queued = timeline(deploy([step('requested', 86_400)]), OFFLINE)
    expect(queued.deadline).toBe(
      'Queued: the board is offline, and the broker holds the command until it next connects.',
    )
    expect(queued.stall).toBeNull()

    const at29 = timeline(deploy([step('requested', 29)]))
    expect(at29.deadline).toBeNull()
    expect(at29.stall).toBeNull()

    const at30 = timeline(deploy([step('requested', 30)]))
    expect(at30.stall).toBe(
      'No answer from the board after 30 s. An online board normally takes a command within seconds; the broker holds it until the board does.',
    )
  })

  describe('the confirm window', () => {
    const rebootingAgo = (s: number, overrides: Partial<DeploySummary> = {}) =>
      deploy([step('requested', s + 30), step('applying', s), step('rebooting', s)], overrides)

    it('counts down the board’s rollback timer from the reboot', () => {
      expect(timeline(rebootingAgo(10)).deadline).toBe(
        'If 1.5.0 never connects, the board rolls back on its own about 300 s after the reboot (4 min 50 s left).',
      )
      expect(timeline(rebootingAgo(300)).deadline).toBe(
        'If 1.5.0 never connects, the board rolls back on its own about 300 s after the reboot (due now).',
      )
      expect(timeline(rebootingAgo(359)).stall).toBeNull()
    })

    it('says the window has passed once its slack is over, with what the board reports now', () => {
      const base =
        'The 300 s rollback window has passed with no result. A board that rolled back reports “rolled back” once the previous image reconnects.'
      const passed = timeline(rebootingAgo(360))
      expect(passed.deadline).toBeNull()
      expect(passed.stall).toBe(`${base} It now reports running 1.4.2 again.`)

      expect(timeline(rebootingAgo(400, { state: 'confirming' })).stall).toBe(
        `${base} It now reports running 1.4.2 again.`,
      )
      expect(timeline(rebootingAgo(400), { online: true, fw_version: '1.5.0' }).stall).toBe(
        `${base} It now reports running 1.5.0, but sent no result for this update.`,
      )
      expect(timeline(rebootingAgo(400), OFFLINE).stall).toBe(`${base} The board is offline now.`)
      expect(timeline(rebootingAgo(400), { online: true, fw_version: '9.9.9' }).stall).toBe(base)
    })

    it('anchors on applying when there is no rebooting step', () => {
      const t = timeline(deploy([step('requested', 40), step('applying', 20), step('confirming', 5)]))
      expect(t.deadline).toContain('(4 min 40 s left)')
    })

    it('never invents a window: no confirm_timeout_s, or no reboot to anchor on', () => {
      const noWindow = timeline(rebootingAgo(10, { confirm_timeout_s: undefined }))
      expect(noWindow.deadline).toBeNull()
      expect(noWindow.stall).toBeNull()

      const noAnchor = timeline(deploy([step('requested', 900), step('confirming', 800)]))
      expect(noAnchor.deadline).toBeNull()
      expect(noAnchor.stall).toBeNull()
    })

    it('words an unknown version without printing null', () => {
      const t = timeline(rebootingAgo(10, { artifact_version: null }))
      expect(t.deadline).toContain('If the new image never connects')
    })
  })

  it('says where a rollback is going', () => {
    const t = timeline(deploy([step('requested', 400), step('rebooting', 380), step('rolling_back', 20)]))
    expect(t.deadline).toBe(
      'The board is going back to 1.4.2; it reports “rolled back” once that image reconnects.',
    )
    expect(t.items[t.items.length - 1]).toMatchObject({ label: 'rolling back', status: 'current' })

    const unknown = timeline(
      deploy([step('rolling_back', 20)], { from_version: null }),
    )
    expect(unknown.deadline).toContain('going back to the previous version')
  })

  it('never puts a deadline or a stall on awaiting_safe_window, even after a day', () => {
    for (const device of [ONLINE, OFFLINE]) {
      const t = timeline(
        deploy([step('requested', 86_500), step('staged', 86_450), step('awaiting_safe_window', 86_400)]),
        device,
      )
      expect(t.deadline).toBeNull()
      expect(t.stall).toBeNull()
      expect(t.current).toBe(
        'waiting for a safe moment — the board decides when, and may wait indefinitely for 24 h 0 min',
      )
      expect(t.items.find((i) => i.status === 'current')?.label).toBe('staged')
    }
  })

  it('says nothing live once the server calls the transaction terminal', () => {
    const t = timeline(deploy([step('requested', 900), step('downloading', 800)], { is_terminal: true }))
    expect(t.deadline).toBeNull()
    expect(t.stall).toBeNull()
    expect(t.current).toBeNull()
  })
})

describe('deployTimeline — never prints a hole', () => {
  it('contains no %, null, undefined or NaN across every fixture above', () => {
    expect(seen.length).toBeGreaterThan(20)
    for (const t of seen) {
      const text = [
        t.elapsed,
        t.current ?? '',
        t.deadline ?? '',
        t.stall ?? '',
        ...t.items.map((i) => `${i.label} ${i.offset ?? ''}`),
      ].join(' ')
      expect(text).not.toMatch(/%|null|undefined|NaN|-\d/)
    }
  })
})
