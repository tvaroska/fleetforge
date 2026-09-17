// What these tests defend, in order of how much a regression would cost:
//
// 1. The cell cannot offer a version this board cannot run. The server refuses a
//    target mismatch with a 409, so an unfiltered picker is a picker full of errors.
// 2. The server's refusals reach the operator VERBATIM. `deploys.py` writes those
//    sentences — the missing `ota` capability, the image that does not fit the slot —
//    with the numbers the operator's next action depends on. A friendlier rewrite here
//    would throw them away.
// 3. `awaiting_safe_window` is a legitimate, unbounded wait, and nothing in this
//    dashboard expires it, styles it as a failure, or spins at it. The device owns the
//    reboot; a "stuck?" badge would be this client inventing a policy the system does
//    not have.
// 4. `pct` never becomes a progress bar. It is a transition log — one number for a
//    whole download — so a bar would sit still and read as the hang this feature exists
//    to rule out.
// 5. A state this dashboard has never heard of renders as itself. `deploy_events.state`
//    is TEXT with no CHECK and a newer agent is allowed to say something new.

import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { DeployCell } from './DeployCell'
import { FleetView } from './FleetView'
import { type DeploySummary, type DeviceSummary } from './api'
import { type EventSourceLike } from './fleet'

const NOW = new Date('2026-09-10T12:00:00Z').getTime()

function device(overrides: Partial<DeviceSummary> = {}): DeviceSummary {
  return {
    device_id: 'a4cf12b3de90',
    name: null,
    group_id: null,
    platform_type: 'esp32c6',
    fw_version: '1.4.2',
    agent_version: '0.1.0',
    link_type: 'wifi',
    power_class: 'always_on',
    expected_wake_interval_s: null,
    parent_device_id: null,
    partition_layout: 'ab-4m-v1',
    ota_slot_size: 1966080,
    capabilities: ['ota'],
    last_seen: '2026-09-10T11:59:30Z',
    enrolled_at: '2026-09-10T11:00:00Z',
    broker_provisioned_at: '2026-09-10T11:00:00Z',
    online: true,
    deploy: null,
    ...overrides,
  }
}

function deploy(overrides: Partial<DeploySummary> = {}): DeploySummary {
  return {
    cmd_id: 'cmd-a',
    state: 'downloading',
    at: '2026-09-10T11:59:40Z',
    is_terminal: false,
    artifact_version: '1.5.0',
    from_version: '1.4.2',
    pct: null,
    detail: null,
    ...overrides,
  }
}

function artifact(version: string, target = 'esp32c6') {
  return {
    target,
    version,
    sha256: 'a'.repeat(64),
    size_bytes: 230_000,
    partition_layout: 'ab-4m-v1',
    kind: 'user_firmware',
    created_at: '2026-09-10T11:00:00Z',
  }
}

// A FACTORY, never a shared `Response`: a body can be read once, so handing the same
// object to two `fetch` calls makes the second one throw and reads as the API being down.
function responds(body: unknown, status = 200) {
  return async () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    })
}

// A `<td>` needs a row and a table around it or jsdom drops it, and `render` would
// then find nothing.
function renderCell(props: Partial<Parameters<typeof DeployCell>[0]> = {}) {
  const onDeployed = vi.fn()
  const onSessionExpired = vi.fn()
  render(
    <table>
      <tbody>
        <tr>
          <DeployCell
            device={props.device ?? device()}
            artifacts={props.artifacts ?? [artifact('1.5.0')]}
            artifactsLoaded={props.artifactsLoaded ?? true}
            onDeployed={props.onDeployed ?? onDeployed}
            onSessionExpired={props.onSessionExpired ?? onSessionExpired}
            now={props.now ?? NOW}
          />
        </tr>
      </tbody>
    </table>,
  )
  return { onDeployed, onSessionExpired }
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('DeployCell — choosing a version', () => {
  // The filter itself lives in `FleetView`, so this is the one test here that renders
  // the whole table: asserting it against a hand-filtered prop would only test the test.
  it('offers only the versions built for this board s chip', async () => {
    const inertSource = (): EventSourceLike => ({
      readyState: 0,
      close: () => {},
      onopen: null,
      onmessage: null,
      onerror: null,
    })
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input?: unknown) => {
      if (String(input) === '/v1/artifact') {
        return new Response(
          JSON.stringify({
            artifacts: [artifact('1.6.0'), artifact('1.5.0'), artifact('0.9.0', 'esp32')],
          }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        )
      }
      return new Response(
        JSON.stringify({ devices: [device({ platform_type: 'esp32c6' })], arrivals: [] }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      )
    })

    render(<FleetView onSessionExpired={vi.fn()} createEventSource={inertSource} />)

    const cell = await screen.findByTestId('deploy-cell')
    await waitFor(() => expect(within(cell).getAllByRole('option')).toHaveLength(2))
    // '0.9.0' is an esp32 image; this board is an esp32c6 and the server would 409.
    expect(within(cell).getAllByRole('option').map((o) => o.textContent)).toEqual([
      '1.6.0',
      '1.5.0',
    ])
  })

  it('preselects the newest version, which is the one the server listed first', () => {
    renderCell({ artifacts: [artifact('1.6.0'), artifact('1.5.0')] })
    expect(screen.getByRole('combobox')).toHaveValue('1.6.0')
  })

  it('names the chip when nothing has been uploaded for it, rather than a dead button', () => {
    renderCell({ device: device({ platform_type: 'esp32c6' }), artifacts: [] })

    expect(screen.getByText('No esp32c6 image has been uploaded yet.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Deploy' })).toBeNull()
  })

  it('says it is still loading before the artifact list lands', () => {
    renderCell({ artifacts: [], artifactsLoaded: false })

    expect(screen.getByText('Loading…')).toBeInTheDocument()
    expect(screen.queryByText(/has been uploaded yet/)).toBeNull()
  })
})

describe('DeployCell — sending a deploy', () => {
  it('posts the chosen version with apply auto, then re-reads the fleet', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(
      responds({
        cmd_id: 'cmd-a',
        device_id: 'a4cf12b3de90',
        version: '1.5.0',
        sha256: 'a'.repeat(64),
        size_bytes: 230_000,
        apply: 'auto',
        reused: false,
        device_online: true,
      }),
    )
    const { onDeployed } = renderCell({ artifacts: [artifact('1.5.0')] })

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/v1/devices/a4cf12b3de90/deploy')
    expect(init.method).toBe('POST')
    // No `target` and no `sha256`: the server resolves the artifact from the board's own
    // `platform_type`, which is what makes a cross-chip flash unrepresentable here.
    expect(JSON.parse(init.body as string)).toEqual({ version: '1.5.0', apply: 'auto' })
    expect(init.credentials).toBe('same-origin')

    // The `requested` row is already committed, so the table re-reads instead of
    // waiting out the poll interval.
    await waitFor(() => expect(onDeployed).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('sends the version the operator picked, not the default', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockImplementation(responds({ cmd_id: 'c', device_id: 'a4cf12b3de90', version: '1.5.0', sha256: 'a', size_bytes: 1, apply: 'auto', reused: false, device_online: true }))
    renderCell({ artifacts: [artifact('1.6.0'), artifact('1.5.0')] })

    await userEvent.selectOptions(screen.getByRole('combobox'), '1.5.0')
    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(JSON.parse(init.body as string)).toEqual({ version: '1.5.0', apply: 'auto' })
  })

  it('renders the server s refusal verbatim, numbers and all', async () => {
    const detail =
      'this device did not announce the `ota` capability, so it has no agent that can ' +
      'stage an update. It announced: nothing.'
    vi.spyOn(globalThis, 'fetch').mockImplementation(responds({ detail }, 409))
    const { onDeployed } = renderCell()

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(detail)
    expect(onDeployed).not.toHaveBeenCalled()
    // The button comes back: the refusal is about this board, and the operator may have
    // a different version to try.
    expect(screen.getByRole('button', { name: 'Deploy' })).toBeEnabled()
  })

  it('says a duplicate deploy will not download twice', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      responds({
        cmd_id: 'cmd-a',
        device_id: 'a4cf12b3de90',
        version: '1.5.0',
        sha256: 'a',
        size_bytes: 1,
        apply: 'auto',
        reused: true,
        device_online: true,
      }),
    )
    renderCell()

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))

    expect(await screen.findByTestId('deploy-accepted')).toHaveTextContent(/already in flight/i)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('calls an offline board s deploy queued, which is what the broker does with it', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      responds({
        cmd_id: 'cmd-a',
        device_id: 'a4cf12b3de90',
        version: '1.5.0',
        sha256: 'a',
        size_bytes: 1,
        apply: 'auto',
        reused: false,
        device_online: false,
      }),
    )
    renderCell({ device: device({ online: false }) })

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))

    const accepted = await screen.findByTestId('deploy-accepted')
    expect(accepted).toHaveTextContent(/queued/i)
    // Queued is not a failure: the QoS-1 command waits in the persistent session.
    expect(accepted).not.toHaveClass('bad')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('bounces a dead session to the login gate instead of showing an error', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(responds({ detail: 'not authenticated' }, 401))
    const { onSessionExpired, onDeployed } = renderCell()

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))

    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(onDeployed).not.toHaveBeenCalled()
  })
})

describe('DeployCell — what the board says back', () => {
  it('shows nothing for a board that has never been deployed to', () => {
    renderCell({ device: device({ deploy: null }) })
    expect(screen.queryByTestId('deploy-state')).toBeNull()
  })

  it('reads the live state from the server s row, with the version it is delivering', () => {
    renderCell({ device: device({ deploy: deploy({ state: 'downloading' }) }) })

    expect(screen.getByTestId('deploy-state')).toHaveTextContent('downloading the image')
    expect(screen.getByTestId('deploy-cell')).toHaveTextContent('→ 1.5.0')
    expect(screen.getByTestId('deploy-cell')).toHaveTextContent('20 s ago')
  })

  it('renders a state this dashboard has never heard of as itself', () => {
    renderCell({ device: device({ deploy: deploy({ state: 'defragmenting' }) }) })

    const state = screen.getByTestId('deploy-state')
    expect(state).toHaveTextContent('defragmenting')
    expect(state).not.toHaveClass('bad')
  })

  it('marks a failure with a word, not only a colour', () => {
    renderCell({
      device: device({ deploy: deploy({ state: 'failed', is_terminal: true, detail: 'sha256 mismatch' }) }),
    })

    expect(screen.getByTestId('deploy-state')).toHaveTextContent('failed')
    expect(screen.getByTestId('deploy-state')).toHaveClass('bad')
    expect(screen.getByTestId('deploy-cell')).toHaveTextContent('sha256 mismatch')
  })

  it('renders device-controlled detail as text', () => {
    renderCell({
      device: device({ deploy: deploy({ state: 'failed', detail: '<script>alert(1)</script>' }) }),
    })

    expect(screen.getByTestId('deploy-cell')).toHaveTextContent('<script>alert(1)</script>')
    expect(document.querySelector('script')).toBeNull()
  })

  // The whole point of the feature: a board waiting for its own safe moment is not a
  // hang, and this dashboard must not imply that it is or invent a deadline for it.
  it('lets awaiting_safe_window wait indefinitely, unstyled and unexpired', async () => {
    vi.useFakeTimers()
    renderCell({ device: device({ deploy: deploy({ state: 'awaiting_safe_window' }) }) })

    const state = screen.getByTestId('deploy-state')
    expect(state).toHaveTextContent('waiting for a safe moment')
    expect(state).toHaveTextContent('may wait indefinitely')
    expect(state).not.toHaveClass('bad')
    expect(screen.getByTestId('deploy-cell')).not.toHaveAttribute('aria-busy')

    await vi.advanceTimersByTimeAsync(10 * 60_000)

    // Ten minutes later: the same sentence, still not a failure, still no "stuck?".
    expect(screen.getByTestId('deploy-state')).toHaveTextContent('waiting for a safe moment')
    expect(screen.getByTestId('deploy-state')).not.toHaveClass('bad')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('reports pct as text and never as a progress bar', () => {
    renderCell({ device: device({ deploy: deploy({ state: 'downloading', pct: 42 }) }) })

    expect(screen.getByTestId('deploy-cell')).toHaveTextContent('42%')
    // `pct` is the first value seen per state, not a feed: a bar driven by it would sit
    // at one number for a whole download and read as a hang.
    expect(screen.queryByRole('progressbar')).toBeNull()
  })

  it('shows no percentage at all when the board has not reported one', () => {
    renderCell({ device: device({ deploy: deploy({ state: 'staged', pct: null }) }) })

    expect(screen.getByTestId('deploy-cell')).not.toHaveTextContent('%')
    expect(screen.queryByRole('progressbar')).toBeNull()
  })

  it('keeps showing the live state while a second deploy is in flight', () => {
    renderCell({ device: device({ deploy: deploy({ state: 'confirmed', is_terminal: true }) }) })

    expect(screen.getByTestId('deploy-state')).toHaveTextContent('done — running the new version')
    expect(screen.getByTestId('deploy-state')).toHaveClass('ok')
    expect(screen.getByRole('button', { name: 'Deploy' })).toBeEnabled()
  })
})
