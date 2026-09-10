// What these tests defend, in order of how much a regression would cost:
//
// 1. **Presence comes from `GET /v1/devices`, never from the SSE payload.** The frame
//    carries an `online` snapshot; patching a row from it looks like a free
//    optimisation and makes the dashboard disagree with the server about a board.
// 2. **The interval re-read exists.** A sleepy board goes offline with NO event at all,
//    so a purely event-driven list shows a dead board as online forever — and passes
//    every test you would naturally write. Test 5 is the one that fails if it is deleted.
// 3. **The EventSource is closed on unmount.** `sse_max_clients` is 20 per API worker
//    and React StrictMode mounts twice; a leaked source turns ~20 reloads into a 503
//    with no visible cause.
// 4. **CLOSED is not CONNECTING.** A 401/503 leaves EventSource permanently closed and
//    needs a manual, backed-off reconnect; a network blip must be left to the browser.
// 5. **A dead API is not a dead session** — that distinction stops an operator re-typing
//    the admin password at a server that is simply down.

import { render, screen } from '@testing-library/react'
import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { FleetView } from './FleetView'
import { COALESCE_MS, POLL_MS, type EventSourceLike } from './fleet'

function device(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    device_id: 'a4cf12b3de90',
    name: 'blinker',
    group_id: null,
    platform_type: 'esp32',
    fw_version: '0.1.0',
    agent_version: '0.1.0',
    link_type: 'wifi',
    power_class: 'always_on',
    expected_wake_interval_s: null,
    parent_device_id: null,
    partition_layout: 'ab-4m-v1',
    ota_slot_size: 1966080,
    capabilities: ['ota'],
    last_seen: '2026-09-10T12:00:00Z',
    enrolled_at: '2026-09-10T11:00:00Z',
    broker_provisioned_at: '2026-09-10T11:00:00Z',
    online: true,
    ...overrides,
  }
}

// A FACTORY, never a shared `Response`: a body can be read once, so handing the same
// object to two `fetch` calls makes the second one throw and reads, in the assertions,
// as the API being down.
function responds(body: unknown, status = 200) {
  return async () =>
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

/** A structural stand-in for `EventSource`: jsdom does not have the global at all. */
class FakeEventSource implements EventSourceLike {
  static created: FakeEventSource[] = []

  readyState = 0
  closes = 0
  onopen: ((ev: Event) => unknown) | null = null
  onmessage: ((ev: MessageEvent<string>) => unknown) | null = null
  onerror: ((ev: Event) => unknown) | null = null

  constructor(readonly url: string) {
    FakeEventSource.created.push(this)
  }

  close() {
    this.closes += 1
    this.readyState = 2
  }

  open() {
    this.readyState = 1
    this.onopen?.(new Event('open'))
  }

  message(payload: unknown) {
    this.raw(JSON.stringify(payload))
  }

  raw(data: string) {
    this.onmessage?.(new MessageEvent('message', { data }))
  }

  /** `readyState` 0 = the browser is retrying; 2 = the server answered with a status. */
  fail(readyState: 0 | 2) {
    this.readyState = readyState
    this.onerror?.(new Event('error'))
  }
}

const HEARTBEAT = { v: 1, type: 'device.heartbeat', device_id: 'a4cf12b3de90', at: '2026-09-10T12:00:00Z', online: true }

const factory = (url: string) => new FakeEventSource(url)

function latest(): FakeEventSource {
  const source = FakeEventSource.created.at(-1)
  if (source === undefined) throw new Error('no EventSource was created')
  return source
}

/** Let queued promise callbacks run without advancing the fake clock. */
async function settle() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

let fetchMock: MockInstance<typeof globalThis.fetch>

beforeEach(() => {
  FakeEventSource.created = []
  vi.useFakeTimers()
  fetchMock = vi.spyOn(globalThis, 'fetch')
})

afterEach(() => {
  vi.useRealTimers()
})

function deviceCalls(): string[] {
  return fetchMock.mock.calls.map(([input]) => String(input)).filter((url) => url === '/v1/devices')
}

describe('useFleet', () => {
  it('renders the fleet from the initial read, before any event arrives', async () => {
    fetchMock.mockImplementation(responds({ devices: [device()] }))

    render(<FleetView onSessionExpired={vi.fn()} createEventSource={factory} />)
    await settle()

    expect(screen.getAllByTestId('device-row')).toHaveLength(1)
    expect(deviceCalls()).toHaveLength(1)
  })

  it('subscribes to the RELATIVE /v1/events, carrying the same-origin cookie', async () => {
    fetchMock.mockImplementation(responds({ devices: [] }))

    render(<FleetView onSessionExpired={vi.fn()} createEventSource={factory} />)
    await settle()

    expect(FakeEventSource.created).toHaveLength(1)
    expect(latest().url).toBe('/v1/events')
  })

  it('coalesces a burst of frames into ONE re-read', async () => {
    fetchMock.mockImplementation(responds({ devices: [device()] }))

    render(<FleetView onSessionExpired={vi.fn()} createEventSource={factory} />)
    await settle()
    const before = deviceCalls().length

    await act(async () => {
      for (let i = 0; i < 5; i += 1) latest().message(HEARTBEAT)
    })
    await advance(COALESCE_MS + 10)

    expect(deviceCalls().length - before).toBe(1)
  })

  it('takes presence from /v1/devices and NEVER from the event payload', async () => {
    // The frame insists the board is up; every read of the record says it is not (a
    // sleepy board whose wake window has expired, say). The row must stay offline both
    // BEFORE the coalesced re-read lands and after it — a hook that patches the row from
    // the payload flips it to online the instant the frame arrives, and this is the test
    // that catches that.
    fetchMock.mockImplementation(responds({ devices: [device({ online: false })] }))

    render(<FleetView onSessionExpired={vi.fn()} createEventSource={factory} />)
    await settle()
    expect(screen.getByLabelText('offline')).toBeInTheDocument()

    await act(async () => {
      latest().message({ ...HEARTBEAT, online: true })
    })
    await settle()
    expect(screen.queryByLabelText('online')).toBeNull()

    await advance(COALESCE_MS + 10)
    expect(screen.getByLabelText('offline')).toBeInTheDocument()
    expect(screen.queryByLabelText('online')).toBeNull()
  })

  it('re-reads on a plain interval, so a sleepy board goes offline with NO event', async () => {
    const sleepy = { power_class: 'sleepy', expected_wake_interval_s: 10 }
    fetchMock
      .mockImplementationOnce(responds({ devices: [device({ ...sleepy, online: true })] }))
      .mockImplementation(responds({ devices: [device({ ...sleepy, online: false })] }))

    render(<FleetView onSessionExpired={vi.fn()} createEventSource={factory} />)
    await settle()
    expect(screen.getByLabelText('online')).toBeInTheDocument()

    // Not one frame is delivered. Presence expiry publishes nothing at all.
    await advance(POLL_MS + 50)

    expect(screen.getByLabelText('offline')).toBeInTheDocument()
  })

  it('re-reads on onopen — the resync path after the 15 min cap or close_all', async () => {
    fetchMock.mockImplementation(responds({ devices: [device()] }))

    render(<FleetView onSessionExpired={vi.fn()} createEventSource={factory} />)
    await settle()
    const before = deviceCalls().length

    await act(async () => {
      latest().open()
    })
    await settle()

    expect(deviceCalls().length).toBe(before + 1)
    expect(screen.getByTestId('stream-status')).toHaveTextContent('Live')
  })

  it('leaves a CONNECTING error to the browser: no second source, no logout', async () => {
    const expired = vi.fn()
    fetchMock.mockImplementation(responds({ devices: [device()] }))

    render(<FleetView onSessionExpired={expired} createEventSource={factory} />)
    await settle()

    await act(async () => {
      latest().fail(0)
    })
    await advance(60_000)

    expect(FakeEventSource.created).toHaveLength(1)
    expect(expired).not.toHaveBeenCalled()
    expect(screen.getByTestId('stream-status')).toHaveTextContent('Reconnecting')
  })

  it('bounces to login when a CLOSED stream is confirmed by a 401 probe', async () => {
    const expired = vi.fn()
    fetchMock
      .mockImplementationOnce(responds({ devices: [device()] }))
      .mockImplementation(responds({ detail: 'not authenticated' }, 401))

    render(<FleetView onSessionExpired={expired} createEventSource={factory} />)
    await settle()

    await act(async () => {
      latest().fail(2)
    })
    await advance(60_000)

    expect(expired).toHaveBeenCalledTimes(1)
    expect(FakeEventSource.created).toHaveLength(1)
  })

  it('reconnects a CLOSED stream with a doubling backoff when the probe is not a 401', async () => {
    fetchMock.mockImplementation(responds({ devices: [device()] }))

    render(<FleetView onSessionExpired={vi.fn()} createEventSource={factory} />)
    await settle()

    // 503 too many event stream clients: EventSource stays CLOSED, so we retry.
    await act(async () => {
      latest().fail(2)
    })
    await settle()
    expect(FakeEventSource.created).toHaveLength(1)
    await advance(1000)
    expect(FakeEventSource.created).toHaveLength(2)

    await act(async () => {
      latest().fail(2)
    })
    await settle()
    await advance(1000)
    expect(FakeEventSource.created).toHaveLength(2) // 1 s is no longer enough
    await advance(1000)
    expect(FakeEventSource.created).toHaveLength(3)
  })

  it('closes the EventSource on unmount and stops fetching (the sse_max_clients guard)', async () => {
    fetchMock.mockImplementation(responds({ devices: [device()] }))

    const view = render(<FleetView onSessionExpired={vi.fn()} createEventSource={factory} />)
    await settle()
    const source = latest()

    view.unmount()
    expect(source.closes).toBe(1)

    const after = deviceCalls().length
    await advance(POLL_MS * 3)
    expect(deviceCalls().length).toBe(after)
  })

  it('keeps the last list and shows a banner when the API is unreachable', async () => {
    const expired = vi.fn()
    fetchMock
      .mockImplementationOnce(responds({ devices: [device()] }))
      .mockRejectedValue(new TypeError('Failed to fetch'))

    render(<FleetView onSessionExpired={expired} createEventSource={factory} />)
    await settle()
    expect(screen.getAllByTestId('device-row')).toHaveLength(1)

    await advance(POLL_MS + 50)

    // Still on screen, with an explanation over it — and still logged in.
    expect(screen.getAllByTestId('device-row')).toHaveLength(1)
    expect(screen.getByRole('alert')).toHaveTextContent(/unreachable/i)
    expect(expired).not.toHaveBeenCalled()
  })

  it('rebuilds a stream the API outlived, even though onerror never fired', async () => {
    // Verified against the real dev stack: stop the api container and the dev proxy holds
    // the `/v1/events` socket open, so `EventSource` reports no error at all. Without this
    // guard the page says "Live" at a dead stream and never recovers when the api returns.
    let apiDown = false
    const ok = responds({ devices: [device()] })
    fetchMock.mockImplementation(async () => {
      if (apiDown) throw new TypeError('Failed to fetch')
      return ok()
    })

    render(<FleetView onSessionExpired={vi.fn()} createEventSource={factory} />)
    await settle()
    await act(async () => {
      latest().open()
    })
    await settle()
    expect(screen.getByTestId('stream-status')).toHaveTextContent('Live')
    expect(FakeEventSource.created).toHaveLength(1)

    // The api goes away. No frame, no `onerror` — only the poll notices.
    apiDown = true
    await advance(POLL_MS + 50)
    expect(screen.getByTestId('stream-status')).toHaveTextContent('Reconnecting')

    // It comes back. The next good read rebuilds the stream rather than trusting it.
    apiDown = false
    await advance(POLL_MS + 50)
    expect(FakeEventSource.created).toHaveLength(2)
    expect(latest().url).toBe('/v1/events')
    await act(async () => {
      latest().open()
    })
    await settle()
    expect(screen.getByTestId('stream-status')).toHaveTextContent('Live')
  })

  it('ignores an unparseable frame and an unknown event type without re-reading', async () => {
    fetchMock.mockImplementation(responds({ devices: [device()] }))

    render(<FleetView onSessionExpired={vi.fn()} createEventSource={factory} />)
    await settle()
    const before = deviceCalls().length

    await act(async () => {
      latest().raw('not json at all')
      latest().raw('[]')
    })
    await advance(COALESCE_MS + 10)
    expect(deviceCalls().length).toBe(before)

    // An unknown but well-formed type IS a hint — `EventType` is additive, and a reader
    // that refused to re-read on a type it does not know would go blind after a server
    // upgrade. Ignoring it means not rendering it, not ignoring the trigger.
    await act(async () => {
      latest().message({ ...HEARTBEAT, type: 'device.something.new' })
    })
    await advance(COALESCE_MS + 10)
    expect(deviceCalls().length).toBe(before + 1)
  })
})
