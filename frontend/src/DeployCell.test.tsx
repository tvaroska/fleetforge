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
// 6. A finished deploy gets a verdict word, and the word is withheld when the board's
//    announce contradicts it.
// 7. Nothing reaches `/deploy` without the pre-check card: Deploy only checks, Send sends.
// 8. A refusal offers no Send at all, and its sentence is the server's, verbatim.
// 9. `override` is in the deploy body only for a gating warning the operator ticked;
//    otherwise the body is exactly `{version, apply}` (today's server 422s on any extra key).
// 10. A pre-check answer that arrives after the version changed, Cancel or a newer Deploy
//    click is ignored.

import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { DeployCell } from './DeployCell'
import { FleetView } from './FleetView'
import { type DeployAccepted, type DeploySummary, type DeployPrecheck, type DeviceSummary } from './api'
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

function precheck(overrides: Partial<DeployPrecheck> = {}): DeployPrecheck {
  return {
    device_id: 'a4cf12b3de90',
    target: 'esp32c6',
    version: '1.5.0',
    from_version: '1.4.2',
    sha256: 'a'.repeat(64),
    size_bytes: 230_000,
    artifact_partition_layout: 'ab-4m-v1',
    device_partition_layout: 'ab-4m-v1',
    ota_slot_size: 1_966_080,
    power_class: 'always_on',
    expected_wake_interval_s: null,
    device_online: true,
    confirm_timeout_s: 300,
    deployable: true,
    refusals: [],
    warnings: [],
    ...overrides,
  }
}

function accepted(overrides: Partial<DeployAccepted> = {}): DeployAccepted {
  return {
    cmd_id: 'cmd-a',
    device_id: 'a4cf12b3de90',
    version: '1.5.0',
    sha256: 'a'.repeat(64),
    size_bytes: 230_000,
    apply: 'auto',
    reused: false,
    device_online: true,
    ...overrides,
  }
}

// Routes the two POSTs by URL. Each entry is `[body, status]` or just a body (200); the
// factory returns a FRESH `Response` per call (see `responds`). `/deploy/precheck` is
// matched first because `/deploy` is its prefix.
function routes(
  config: { precheck?: [unknown, number?] | [DeployPrecheck]; deploy?: [unknown, number?] } = {},
) {
  const [precheckBody, precheckStatus] = config.precheck ?? [precheck()]
  const [deployBody, deployStatus] = config.deploy ?? [accepted()]
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input?: unknown) => {
    const url = String(input)
    if (url.endsWith('/deploy/precheck')) return responds(precheckBody, precheckStatus)()
    if (url.endsWith('/deploy')) return responds(deployBody, deployStatus)()
    return new Response('not routed: ' + url, { status: 500 })
  })
  return { fetchMock }
}

function urls(fetchMock: { mock: { calls: unknown[][] } }): string[] {
  return fetchMock.mock.calls.map((call) => String(call[0]))
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

    expect(screen.getByText(
        'No esp32c6 image has been uploaded yet. Upload one under “Upload a build”.',
      )).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Deploy' })).toBeNull()
  })

  it('says it is still loading before the artifact list lands', () => {
    renderCell({ artifacts: [], artifactsLoaded: false })

    expect(screen.getByText('Loading…')).toBeInTheDocument()
    expect(screen.queryByText(/has been uploaded yet/)).toBeNull()
  })
})

describe('DeployCell — sending a deploy', () => {
  it('checks first: Deploy posts only to the pre-check and shows the card', async () => {
    const { fetchMock } = routes()
    renderCell({ artifacts: [artifact('1.5.0')] })

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))

    const card = await screen.findByTestId('precheck-card')
    expect(urls(fetchMock)).toEqual(['/v1/devices/a4cf12b3de90/deploy/precheck'])
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({ version: '1.5.0', apply: 'auto' })
    expect(card).toHaveTextContent('1.4.2 → 1.5.0')
    expect(screen.getByTestId('precheck-fit')).toHaveTextContent('230,000 of 1,966,080 bytes')
    expect(screen.getByTestId('precheck-rollback')).toHaveTextContent(
      'If 1.5.0 never reconnects within 300 s of its reboot, the board rolls back on its own to 1.4.2.',
    )
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('sends on the second click with apply auto and no override, then re-reads the fleet', async () => {
    const { fetchMock } = routes()
    const { onDeployed } = renderCell({ artifacts: [artifact('1.5.0')] })

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Send' }))

    await waitFor(() => expect(onDeployed).toHaveBeenCalledTimes(1))
    expect(urls(fetchMock)).toEqual([
      '/v1/devices/a4cf12b3de90/deploy/precheck',
      '/v1/devices/a4cf12b3de90/deploy',
    ])
    const [, init] = fetchMock.mock.calls[1] as [string, RequestInit]
    expect(init.method).toBe('POST')
    // No `target`, no `sha256` and no `override` key: today's server forbids extra keys.
    expect(JSON.parse(init.body as string)).toEqual({ version: '1.5.0', apply: 'auto' })
    expect(init.credentials).toBe('same-origin')
    expect(screen.queryByTestId('precheck-card')).toBeNull()
    expect(screen.getByTestId('deploy-accepted')).toHaveTextContent('sent')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('sends the version the operator picked, to both the pre-check and the deploy', async () => {
    const { fetchMock } = routes({ precheck: [precheck({ version: '1.5.0' })] })
    renderCell({ artifacts: [artifact('1.6.0'), artifact('1.5.0')] })

    await userEvent.selectOptions(screen.getByRole('combobox'), '1.5.0')
    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Send' }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    for (const call of fetchMock.mock.calls) {
      const [, init] = call as [string, RequestInit]
      expect(JSON.parse(init.body as string)).toEqual({ version: '1.5.0', apply: 'auto' })
    }
  })

  it('offers no Send for a refusal, shows its sentence verbatim and never reaches /deploy', async () => {
    const message =
      'this device runs partition layout ab-4m-v1 and 1.6.0 was built for ab-4m-arduino-v1, ' +
      'so the image cannot boot on it.'
    const { fetchMock } = routes({
      precheck: [
        precheck({
          deployable: false,
          refusals: [{ code: 'layout_mismatch', message }],
          artifact_partition_layout: 'ab-4m-arduino-v1',
        }),
      ],
    })
    renderCell()

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))

    const refusals = await screen.findByTestId('precheck-refusals')
    expect(refusals).toHaveTextContent('Refused:')
    expect(refusals).toHaveTextContent(message)
    expect(screen.getByTestId('precheck-card')).toHaveTextContent('Refusals cannot be overridden.')
    expect(screen.queryByRole('button', { name: /^Send/ })).toBeNull()
    expect(screen.queryByTestId('precheck-rollback')).toBeNull()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled()
    expect(urls(fetchMock)).toEqual(['/v1/devices/a4cf12b3de90/deploy/precheck'])
  })

  it('overrides a non-gating warning by the Send click, with no override key', async () => {
    const message = 'this board is offline: the update waits in its queue until it reconnects.'
    const { fetchMock } = routes({
      precheck: [precheck({ device_online: false, warnings: [{ code: 'offline', message }] })],
    })
    renderCell({ device: device({ online: false }) })

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))

    expect(await screen.findByTestId('precheck-warnings')).toHaveTextContent(`Warning: ${message}`)
    expect(screen.queryByRole('checkbox')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Send' })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Send anyway' }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    const [, init] = fetchMock.mock.calls[1] as [string, RequestInit]
    expect(JSON.parse(init.body as string)).toEqual({ version: '1.5.0', apply: 'auto' })
  })

  it('gates a gating warning on its own tick and then sends override for that code only', async () => {
    const { fetchMock } = routes({
      precheck: [
        precheck({
          warnings: [
            { code: 'offline', message: 'this board is offline' },
            { code: 'rollback_incapable', message: 'no rollback here', needs_override: true },
          ],
        }),
      ],
    })
    renderCell()

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))
    const send = await screen.findByRole('button', { name: 'Send anyway' })
    expect(send).toBeDisabled()
    // Only the gating warning gets a box.
    expect(screen.getAllByRole('checkbox')).toHaveLength(1)

    await userEvent.click(screen.getByRole('checkbox', { name: 'Send anyway: rollback_incapable' }))
    expect(send).toBeEnabled()
    await userEvent.click(send)

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    const [, init] = fetchMock.mock.calls[1] as [string, RequestInit]
    expect(JSON.parse(init.body as string)).toEqual({
      version: '1.5.0',
      apply: 'auto',
      override: ['rollback_incapable'],
    })
  })

  it('discards the card and its ticks when the version changes', async () => {
    routes({
      precheck: [
        precheck({
          warnings: [{ code: 'rollback_incapable', message: 'no rollback', needs_override: true }],
        }),
      ],
    })
    renderCell({ artifacts: [artifact('1.6.0'), artifact('1.5.0')] })

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))
    await userEvent.click(await screen.findByRole('checkbox'))
    await userEvent.selectOptions(screen.getByRole('combobox'), '1.5.0')
    expect(screen.queryByTestId('precheck-card')).toBeNull()

    // A fresh check starts unticked.
    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))
    expect(await screen.findByRole('checkbox')).not.toBeChecked()
  })

  it('ignores a pre-check answer that arrives after the version changed', async () => {
    let resolve: (response: Response) => void = () => {}
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      () => new Promise<Response>((r) => (resolve = r)),
    )
    renderCell({ artifacts: [artifact('1.6.0'), artifact('1.5.0')] })

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))
    expect(screen.getByRole('button', { name: 'Checking…' })).toBeDisabled()
    await userEvent.selectOptions(screen.getByRole('combobox'), '1.5.0')
    expect(screen.getByRole('button', { name: 'Deploy' })).toBeEnabled()

    await act(async () => {
      resolve(
        new Response(JSON.stringify(precheck({ version: '1.6.0' })), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
      )
    })

    expect(screen.queryByTestId('precheck-card')).toBeNull()
    expect(screen.getByRole('button', { name: 'Deploy' })).toBeEnabled()
  })

  it('ignores the answer to an older check once a newer Deploy click is pending', async () => {
    const pending: Array<(response: Response) => void> = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      () => new Promise<Response>((r) => pending.push(r)),
    )
    const answer = (index: number, version: string) =>
      act(async () => {
        pending[index](
          new Response(JSON.stringify(precheck({ version })), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          }),
        )
      })
    renderCell({ artifacts: [artifact('1.6.0'), artifact('1.5.0')] })

    // Check 1.6.0, change the pick (the Deploy button comes back), check 1.5.0.
    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))
    await userEvent.selectOptions(screen.getByRole('combobox'), '1.5.0')
    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))
    expect(pending).toHaveLength(2)

    // The older answer lands first and must not paint a card, nor free the button.
    await answer(0, '1.6.0')
    expect(screen.queryByTestId('precheck-card')).toBeNull()
    expect(screen.getByRole('button', { name: 'Checking…' })).toBeDisabled()

    await answer(1, '1.5.0')
    expect(await screen.findByTestId('precheck-card')).toHaveTextContent('1.4.2 → 1.5.0')
    expect(screen.getAllByTestId('precheck-card')).toHaveLength(1)
  })

  it('closes the card on Cancel without sending anything', async () => {
    const { fetchMock } = routes()
    renderCell()

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel' }))

    expect(screen.queryByTestId('precheck-card')).toBeNull()
    expect(urls(fetchMock)).toEqual(['/v1/devices/a4cf12b3de90/deploy/precheck'])
  })

  it('bounces a dead session on the pre-check to the login gate, with no alert and no card', async () => {
    routes({ precheck: [{ detail: 'not authenticated' }, 401] })
    const { onSessionExpired } = renderCell()

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))

    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByTestId('precheck-card')).toBeNull()
  })

  it('renders a pre-check error verbatim and opens no card', async () => {
    const detail = 'no device with id a4cf12b3de90'
    routes({ precheck: [{ detail }, 404] })
    renderCell()

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(detail)
    expect(screen.queryByTestId('precheck-card')).toBeNull()
    expect(screen.getByRole('button', { name: 'Deploy' })).toBeEnabled()
  })

  it('renders the server s refusal of the deploy verbatim, numbers and all, and keeps the card', async () => {
    const detail =
      'this device did not announce the `ota` capability, so it has no agent that can ' +
      'stage an update. It announced: nothing.'
    routes({ deploy: [{ detail }, 409] })
    const { onDeployed } = renderCell()

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Send' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(detail)
    expect(onDeployed).not.toHaveBeenCalled()
    // The card stays and Send comes back: the refusal is about this board, and the
    // operator may Cancel and try a different version.
    expect(screen.getByTestId('precheck-card')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
  })

  it('says a duplicate deploy will not download twice', async () => {
    routes({ deploy: [accepted({ reused: true })] })
    renderCell()

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Send' }))

    expect(await screen.findByTestId('deploy-accepted')).toHaveTextContent(/already in flight/i)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('calls an offline board s deploy queued, which is what the broker does with it', async () => {
    routes({ deploy: [accepted({ device_online: false })] })
    renderCell({ device: device({ online: false }) })

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Send' }))

    const queued = await screen.findByTestId('deploy-accepted')
    expect(queued).toHaveTextContent(/queued/i)
    // Queued is not a failure: the QoS-1 command waits in the persistent session.
    expect(queued).not.toHaveClass('bad')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('bounces a dead session on the send to the login gate instead of showing an error', async () => {
    routes({ deploy: [{ detail: 'not authenticated' }, 401] })
    const { onSessionExpired, onDeployed } = renderCell()

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Send' }))

    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(onDeployed).not.toHaveBeenCalled()
  })

  it('never prints null or undefined when every nullable in the pre-check is null', async () => {
    routes({
      precheck: [
        precheck({
          from_version: null,
          sha256: null,
          size_bytes: null,
          artifact_partition_layout: null,
          device_partition_layout: null,
          ota_slot_size: null,
          expected_wake_interval_s: null,
        }),
      ],
    })
    renderCell()

    await userEvent.click(screen.getByRole('button', { name: 'Deploy' }))

    await screen.findByTestId('precheck-card')
    const cell = screen.getByTestId('deploy-cell')
    expect(cell).toHaveTextContent('— → 1.5.0')
    expect(cell).toHaveTextContent('the version it runs now')
    expect(cell).not.toHaveTextContent('null')
    expect(cell).not.toHaveTextContent('undefined')
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
    renderCell({
      device: device({
        fw_version: '1.5.0',
        deploy: deploy({ state: 'confirmed', is_terminal: true }),
      }),
    })

    expect(screen.getByTestId('deploy-state').textContent).toBe('good')
    expect(screen.getByTestId('deploy-state')).toHaveClass('ok')
    expect(screen.getByRole('button', { name: 'Deploy' })).toBeEnabled()
  })

  it('says good, without an arrow or a percentage, when the board runs the confirmed image', () => {
    renderCell({
      device: device({
        fw_version: '1.5.0',
        deploy: deploy({ state: 'confirmed', is_terminal: true, pct: 100 }),
      }),
    })

    const cell = screen.getByTestId('deploy-cell')
    expect(screen.getByTestId('deploy-state').textContent).toBe('good')
    expect(screen.getByTestId('deploy-state')).toHaveClass('ok')
    expect(cell).toHaveTextContent('running 1.5.0')
    expect(cell).not.toHaveTextContent('100%')
    expect(cell).not.toHaveTextContent('→')
  })

  it('says rolled back, names the failed version and where the board is back', () => {
    const detail = 'returned to 1.4.2; the new image did not confirm'
    renderCell({
      device: device({
        fw_version: '1.4.2',
        deploy: deploy({ state: 'rolled_back', is_terminal: true, detail }),
      }),
    })

    const cell = screen.getByTestId('deploy-cell')
    expect(screen.getByTestId('deploy-state').textContent).toBe('rolled back')
    expect(screen.getByTestId('deploy-state')).toHaveClass('bad')
    expect(cell).toHaveTextContent('1.5.0 did not confirm')
    expect(cell).toHaveTextContent('back on 1.4.2')
    expect(cell).toHaveTextContent(detail)
    expect(cell).not.toHaveTextContent('→ 1.5.0')
  })

  it('withholds the verdict when the board has since reported another version', () => {
    renderCell({
      device: device({
        fw_version: '1.4.2',
        deploy: deploy({ state: 'confirmed', is_terminal: true, pct: 100 }),
      }),
    })

    const state = screen.getByTestId('deploy-state')
    expect(state.textContent).not.toBe('good')
    expect(state).not.toHaveClass('ok')
    expect(screen.getByTestId('deploy-drift')).toHaveTextContent('has since reported 1.4.2')
    expect(screen.getByTestId('deploy-cell')).not.toHaveTextContent('running')
  })

  it('says the board has not reported when drift has no announce at all', () => {
    renderCell({
      device: device({
        fw_version: null,
        deploy: deploy({ state: 'confirmed', is_terminal: true }),
      }),
    })

    expect(screen.getByTestId('deploy-drift')).toHaveTextContent(
      'the board has not reported a version since',
    )
  })

  it('never prints null or undefined for a rollback with every nullable empty', () => {
    renderCell({
      device: device({
        fw_version: null,
        deploy: deploy({
          state: 'rolled_back',
          is_terminal: true,
          artifact_version: null,
          from_version: null,
          pct: null,
          detail: null,
        }),
      }),
    })

    const cell = screen.getByTestId('deploy-cell')
    expect(cell).toHaveTextContent('the new image did not confirm')
    expect(cell).not.toHaveTextContent('null')
    expect(cell).not.toHaveTextContent('undefined')
  })

  it('renders a non-terminal confirming state exactly as before', () => {
    renderCell({ device: device({ deploy: deploy({ state: 'confirming' }) }) })

    expect(screen.getByTestId('deploy-cell')).toHaveTextContent('confirming the new image → 1.5.0')
  })
})
