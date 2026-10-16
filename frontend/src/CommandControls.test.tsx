// What these tests defend (R5-fe-1):
//
// 1. Apply and Cancel update post once, to the exact verb path, with no body, and re-read.
// 2. Reboot never posts without its confirm step; Keep running posts nothing.
// 3. The server's refusal is shown verbatim; a 401 bounces to the login gate.
// 4. A legacy board gets no buttons; the last-command line never announces itself.

import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CommandControls } from './CommandControls'
import { type CommandSummary, type DeploySummary, type DeviceSummary } from './api'

const NOW = new Date('2026-09-10T12:00:00Z').getTime()
const ALL = ['ota', 'apply', 'cancel', 'rollback', 'reboot', 'selftest']

function deploy(overrides: Partial<DeploySummary> = {}): DeploySummary {
  return {
    cmd_id: 'cmd-a',
    state: 'staged',
    at: '2026-09-10T11:59:40Z',
    is_terminal: false,
    artifact_version: '1.5.0',
    from_version: '1.4.2',
    pct: null,
    detail: null,
    ...overrides,
  }
}

function device(overrides: Partial<DeviceSummary> = {}): DeviceSummary {
  return {
    device_id: 'a4cf12b3de90',
    name: 'coop door',
    group_id: null,
    platform_type: 'esp32s3',
    fw_version: '1.4.2',
    agent_version: '0.6.0',
    link_type: 'wifi',
    ssid: null,
    known_networks: null,
    power_class: 'always_on',
    expected_wake_interval_s: null,
    parent_device_id: null,
    partition_layout: 'ab-4m-v1',
    ota_slot_size: 1966080,
    capabilities: ALL,
    last_seen: '2026-09-10T11:59:30Z',
    enrolled_at: '2026-09-10T11:00:00Z',
    broker_provisioned_at: '2026-09-10T11:00:00Z',
    online: true,
    deploy: deploy(),
    ...overrides,
  }
}

function command(overrides: Partial<CommandSummary> = {}): CommandSummary {
  return {
    command_id: 'c0ffee',
    type: 'apply',
    cmd_id: 'cmd-a',
    sent_at: '2026-09-10T11:59:50Z',
    sent_by: null,
    result: null,
    detail: null,
    answered_at: null,
    ...overrides,
  }
}

function respond(body: unknown, status = 202) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(
    async () => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }),
  )
}

const ACCEPTED = { command_id: 'c0ffee', device_id: 'a4cf12b3de90', type: 'apply', cmd_id: 'cmd-a', device_online: true }

function renderControls(d: DeviceSummary = device()) {
  const onSent = vi.fn()
  const onSessionExpired = vi.fn()
  const view = render(<CommandControls device={d} now={NOW} onSent={onSent} onSessionExpired={onSessionExpired} />)
  return { onSent, onSessionExpired, view }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('CommandControls — Apply and Cancel update', () => {
  it('Apply posts once to the verb path with no body, then re-reads', async () => {
    const fetchMock = respond(ACCEPTED)
    const { onSent } = renderControls()

    await userEvent.click(screen.getByRole('button', { name: 'Apply the staged update on coop door' }))

    await waitFor(() => expect(onSent).toHaveBeenCalledTimes(1))
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/v1/devices/a4cf12b3de90/deploys/cmd-a/apply')
    expect(init?.method).toBe('POST')
    expect(init?.body).toBeUndefined()
    expect(screen.queryByTestId('command-queued')).toBeNull()
  })

  it('Cancel update posts once to the cancel path', async () => {
    const fetchMock = respond({ ...ACCEPTED, type: 'cancel' })
    const { onSent } = renderControls()

    await userEvent.click(screen.getByRole('button', { name: 'Cancel the update on coop door' }))

    await waitFor(() => expect(onSent).toHaveBeenCalledTimes(1))
    expect(fetchMock.mock.calls.map((c) => String(c[0]))).toEqual(['/v1/devices/a4cf12b3de90/deploys/cmd-a/cancel'])
    expect(screen.getByTestId('command-cancel')).toHaveTextContent('Cancel update')
  })

  it('disables every command button while a POST is in flight', async () => {
    let release: (r: Response) => void = () => {}
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      () => new Promise<Response>((resolve) => (release = resolve)),
    )
    renderControls()

    await userEvent.click(screen.getByTestId('command-apply'))

    expect(screen.getByTestId('command-apply')).toBeDisabled()
    expect(screen.getByTestId('command-cancel')).toBeDisabled()
    expect(screen.getByTestId('command-reboot')).toBeDisabled()
    await act(async () => release(new Response(JSON.stringify(ACCEPTED), { status: 202 })))
    await waitFor(() => expect(screen.getByTestId('command-apply')).toBeEnabled())
  })

  it('says queued when the board is offline', async () => {
    respond({ ...ACCEPTED, device_online: false })
    renderControls()

    await userEvent.click(screen.getByTestId('command-apply'))

    expect(await screen.findByTestId('command-queued')).toHaveTextContent(
      'queued — the board is offline; the broker holds the command until it next connects',
    )
  })

  it('shows the server sentence verbatim in an alert', async () => {
    const sentence = 'deploy cmd-a was sent with apply: auto; the board applies it by itself'
    respond({ detail: sentence }, 409)
    const { onSent } = renderControls()

    await userEvent.click(screen.getByTestId('command-apply'))

    expect(await screen.findByRole('alert')).toHaveTextContent(sentence)
    expect(onSent).not.toHaveBeenCalled()
  })

  it('bounces to the login gate on a 401, with no alert', async () => {
    respond({ detail: 'not authenticated' }, 401)
    const { onSessionExpired } = renderControls()

    await userEvent.click(screen.getByTestId('command-cancel'))

    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('CommandControls — Reboot', () => {
  it('asks first; Keep running posts nothing; Reboot now posts once', async () => {
    const fetchMock = respond({ ...ACCEPTED, type: 'reboot', cmd_id: null })
    const { onSent } = renderControls(device({ deploy: null }))

    await userEvent.click(screen.getByRole('button', { name: 'Reboot coop door' }))
    expect(fetchMock).not.toHaveBeenCalled()
    expect(screen.getByTestId('command-reboot-confirm')).toHaveTextContent(
      'Reboot coop door now? It restarts on 1.4.2.',
    )

    await userEvent.click(screen.getByRole('button', { name: 'Keep running' }))
    expect(fetchMock).not.toHaveBeenCalled()
    expect(screen.queryByTestId('command-reboot-confirm')).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: 'Reboot coop door' }))
    await userEvent.click(screen.getByRole('button', { name: 'Reboot now' }))

    await waitFor(() => expect(onSent).toHaveBeenCalledTimes(1))
    expect(fetchMock.mock.calls.map((c) => String(c[0]))).toEqual(['/v1/devices/a4cf12b3de90/reboot'])
    expect(fetchMock.mock.calls[0][1]?.body).toBeUndefined()
  })

  it('names the current image when the version is unknown', async () => {
    renderControls(device({ fw_version: null, deploy: null }))
    await userEvent.click(screen.getByTestId('command-reboot'))
    expect(screen.getByTestId('command-reboot-confirm')).toHaveTextContent('It restarts on its current image.')
  })
})

describe('CommandControls — what renders', () => {
  it('a legacy board gets no buttons and no container', () => {
    renderControls(device({ capabilities: ['ota'] }))
    expect(screen.queryByTestId('command-controls')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('a legacy board still shows a REST rollback answer, with no buttons', () => {
    renderControls(device({ capabilities: ['ota'], last_command: command({ type: 'rollback', cmd_id: null }) }))
    expect(screen.getByTestId('last-command')).toHaveTextContent('Rollback · sent, no answer yet (10 s ago)')
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('renders a refusal with its detail and the bad class', () => {
    renderControls(
      device({
        last_command: command({
          type: 'reboot',
          cmd_id: null,
          result: 'refused',
          detail: 'an update is in progress',
          answered_at: '2026-09-10T11:59:55Z',
        }),
      }),
    )
    const line = screen.getByTestId('last-command')
    expect(line).toHaveAttribute('data-result', 'refused')
    expect(line).toHaveTextContent('Reboot · refused: an update is in progress (5 s ago)')
    expect(line.querySelector('.bad')).toHaveTextContent('refused')
  })

  it('renders a missing answer as data-result none, without a spinner or timeout', () => {
    renderControls(device({ last_command: command() }))
    const line = screen.getByTestId('last-command')
    expect(line).toHaveAttribute('data-result', 'none')
    expect(line.textContent).not.toMatch(/stuck|timed out|null|undefined/)
    expect(document.querySelector('[role="progressbar"]')).toBeNull()
  })

  it('announces nothing to assistive tech', () => {
    const { view } = renderControls(device({ last_command: command({ result: 'deferred' }) }))
    expect(view.container.querySelector('[aria-live]')).toBeNull()
    expect(view.container.querySelector('[role]')).toBeNull()
  })

  it('copes with last_command absent (an older API)', () => {
    const d = device()
    delete (d as Partial<DeviceSummary>).last_command
    renderControls(d)
    expect(screen.queryByTestId('last-command')).toBeNull()
    expect(screen.getByTestId('command-apply')).toBeInTheDocument()
  })
})
