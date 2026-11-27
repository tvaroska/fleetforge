import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, type DeviceSummary } from './api'
import { DeviceInspector, renderAsciiSparkline } from './DeviceInspector'

const NOW = Date.parse('2026-11-20T12:00:00Z')

function makeDevice(overrides: Partial<DeviceSummary> = {}): DeviceSummary {
  return {
    device_id: 'a4cf12b3de90',
    name: 'gh-node-01',
    group_id: null,
    platform_type: 'esp32s3',
    fw_version: '1.4.2',
    agent_version: '0.5.0',
    link_type: 'wifi',
    ssid: 'lab-24',
    known_networks: 2,
    power_class: 'always_on',
    expected_wake_interval_s: null,
    parent_device_id: null,
    partition_layout: 'ab-4m-v1',
    ota_slot_size: 1966080,
    capabilities: ['ota', 'reboot', 'rollback'],
    last_seen: '2026-11-20T11:59:50Z',
    enrolled_at: '2026-11-20T10:00:00Z',
    broker_provisioned_at: '2026-11-20T10:00:01Z',
    online: true,
    deploy: null,
    ...overrides,
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('renderAsciiSparkline', () => {
  it('renders an em-dash for empty samples, flat blocks for constant values, and 8-level blocks for ramps', () => {
    expect(renderAsciiSparkline([])).toBe('—')
    expect(renderAsciiSparkline([100, 100, 100])).toBe('▄▄▄')
    const ramp = renderAsciiSparkline([0, 50, 100])
    expect(ramp[0]).toBe('▁')
    expect(ramp[2]).toBe('█')
  })
})

describe('DeviceInspector — Mode A (Triage & Bench)', () => {
  it('shows empty guidance when no device is selected without issuing mount fetches', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    render(<DeviceInspector devices={[]} now={NOW} />)
    expect(screen.getByTestId('inspector-empty')).toBeInTheDocument()
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('renders safe-mode alert, health/heap/reset_reason metrics, cargo sub_versions, and selftest slot', () => {
    const faulted = makeDevice({
      safe_mode: true,
      last_reset_reason: 'panic',
      fault_resets: { window_s: 86400, panic: 3, task_wdt: 0, brownout: 1 },
      health: {
        at: '2026-11-20T11:59:45Z',
        uptime_s: 120,
        rssi: -64,
        free_heap: 131072,
        min_free_heap: 98304,
        boot_ok: false,
      },
      sub_versions: { stm32: '2.1.0', weights: 'v4' },
      deploy: {
        cmd_id: 'cmd-9',
        state: 'rolled_back',
        at: '2026-11-20T11:58:00Z',
        is_terminal: true,
        artifact_version: '1.5.0',
        from_version: '1.4.2',
        pct: 100,
        detail: 'selftest failed',
        steps: [],
        confirm_timeout_s: 180,
        selftest: {
          outcome: 'failed',
          reason: 'i2c_nack_0x44',
          returned_to: 'ota_0',
        },
      },
    })

    render(<DeviceInspector device={faulted} now={NOW} />)
    expect(screen.getByTestId('inspector-safe-mode-alert')).toHaveTextContent('ota_0')
    expect(screen.getByTestId('inspector-vitals')).toHaveTextContent('128.0 KiB')
    expect(screen.getByTestId('inspector-vitals')).toHaveTextContent('96.0 KiB')
    expect(screen.getByTestId('inspector-vitals')).toHaveTextContent('-64 dBm')
    expect(screen.getByTestId('inspector-vitals')).toHaveTextContent('panic')
    expect(screen.getByTestId('inspector-vitals')).toHaveTextContent('panic ×3, brownout ×1 in 24 h')
    expect(screen.getByTestId('inspector-sub-versions')).toHaveTextContent('stm32:2.1.0')
    expect(screen.getByTestId('inspector-last-update')).toHaveTextContent('i2c_nack_0x44')
  })

  it('dispatches flag updates, rollback confirmation, reboot, sparklines, and surfaces API/401 errors', async () => {
    const user = userEvent.setup()
    const onUpdated = vi.fn()
    const onSessionExpired = vi.fn()
    const board = makeDevice()

    const updateSpy = vi.spyOn(api, 'updateDevice').mockResolvedValue({ ...board, is_dev: true })
    const rollbackSpy = vi.spyOn(api, 'rollbackDevice').mockResolvedValue({
      command_id: 'cmd-rb-1',
      device_id: board.device_id,
      type: 'rollback',
      cmd_id: null,
      device_online: true,
    })
    const rebootSpy = vi.spyOn(api, 'rebootDevice').mockRejectedValueOnce(new ApiError(503, 'broker unavailable'))
    const telemetrySpy = vi.spyOn(api, 'deviceTelemetry').mockResolvedValue({
      device_id: board.device_id,
      name: 'free_heap',
      samples: [
        { at: '2026-11-20T11:59:00Z', value: 131072 },
        { at: '2026-11-20T11:58:00Z', value: 98304 },
      ],
    })

    render(
      <DeviceInspector
        device={board}
        now={NOW}
        onUpdated={onUpdated}
        onSessionExpired={onSessionExpired}
      />,
    )

    await user.click(screen.getByTestId('inspector-toggle-dev'))
    expect(updateSpy).toHaveBeenCalledWith(board.device_id, { is_dev: true })
    expect(await screen.findByTestId('inspector-status')).toHaveTextContent(
      'Updated board configuration.',
    )
    expect(onUpdated).toHaveBeenCalledTimes(1)

    await user.click(screen.getByTestId('inspector-rollback-btn'))
    expect(screen.getByTestId('inspector-rollback-confirm')).toBeInTheDocument()
    await user.click(screen.getByTestId('inspector-rollback-confirm-btn'))
    expect(rollbackSpy).toHaveBeenCalledWith(board.device_id)
    expect(await screen.findByTestId('inspector-status')).toHaveTextContent('cmd-rb-1')

    await user.click(screen.getByTestId('inspector-reboot-btn'))
    expect(rebootSpy).toHaveBeenCalledWith(board.device_id)
    expect(await screen.findByTestId('inspector-error')).toHaveTextContent('broker unavailable')

    await user.click(screen.getByTestId('load-telemetry-sparklines'))
    expect(telemetrySpy).toHaveBeenCalledWith(board.device_id, 'free_heap')
    expect(await screen.findByTestId('telemetry-sparkline-box')).toHaveTextContent('2 samples')

    updateSpy.mockRejectedValueOnce(new ApiError(401, 'expired'))
    await user.click(screen.getByTestId('inspector-toggle-stay-awake'))
    expect(onSessionExpired).toHaveBeenCalledTimes(1)
  })
})

describe('DeviceInspector — Modes B (Dock), C (Compare), and D (Bulk / RMA)', () => {
  it('renders RULE E301 dock posture, mesh skew badge, validates JSON, and replays a dock bundle', async () => {
    const user = userEvent.setup()
    const nodeA = makeDevice({ device_id: 'node-a', name: 'rover-a', fw_version: '1.4.2' })
    const nodeB = makeDevice({
      device_id: 'node-b',
      name: 'rover-b',
      link_type: 'dock',
      fw_version: '1.3.0',
    })
    const dockSpy = vi.spyOn(api, 'dockSync').mockResolvedValue({
      device_id: 'node-a',
      link_type: 'dock',
      fw_version: '1.4.2',
      statuses_recorded: 2,
      telemetry_batches_recorded: 3,
      log_batches_recorded: 1,
      device: nodeA,
    })

    render(<DeviceInspector device={nodeA} devices={[nodeA, nodeB]} initialMode="dock" now={NOW} />)
    expect(screen.getByTestId('dock-posture-badges')).toHaveTextContent('802.11 OFF (RULE E301)')
    expect(screen.getByTestId('mesh-skew-badge')).toHaveTextContent('! SKEW')

    const input = screen.getByTestId('inspector-dock-bundle-input')
    await user.type(input, 'not-json')
    await user.click(screen.getByTestId('dock-sync-btn'))
    expect(await screen.findByTestId('inspector-error')).toHaveTextContent('not valid JSON')

    await user.clear(input)
    await user.paste('{"statuses":[{"state":"confirmed"}],"telemetry":[],"logs":[]}')
    await user.click(screen.getByTestId('dock-sync-btn'))
    expect(dockSpy).toHaveBeenCalledWith('node-a', {
      statuses: [{ state: 'confirmed' }],
      telemetry: [],
      logs: [],
    })
    expect(await screen.findByTestId('dock-sync-receipt')).toHaveTextContent('statuses: 2')
  })

  it('filters exception boards in Compare mode and dispatches confirmed bulk actions', async () => {
    const user = userEvent.setup()
    const healthy = makeDevice({ device_id: 'ok-1', name: 'healthy-board' })
    const safeBoard = makeDevice({
      device_id: 'safe-1',
      name: 'safe-board',
      safe_mode: true,
    })
    const selftestBoard = makeDevice({
      device_id: 'st-1',
      name: 'i2c-board',
      deploy: {
        cmd_id: 'c1',
        state: 'rolled_back',
        at: '2026-11-20T11:55:00Z',
        is_terminal: true,
        artifact_version: '1.5.0',
        from_version: '1.4.2',
        pct: 100,
        detail: null,
        steps: [],
        confirm_timeout_s: 180,
        selftest: { outcome: 'failed', reason: 'i2c sensor nack', returned_to: 'ota_0' },
      },
    })
    const rebootSpy = vi.spyOn(api, 'rebootDevice').mockResolvedValue({
      command_id: 'cmd-1',
      device_id: 'st-1',
      type: 'reboot',
      cmd_id: null,
      device_online: true,
    })

    render(
      <DeviceInspector
        devices={[healthy, safeBoard, selftestBoard]}
        initialMode="compare"
        now={NOW}
      />,
    )

    const table = screen.getByTestId('compare-boards-table')
    expect(within(table).queryByText('healthy-board')).toBeNull()
    expect(within(table).getAllByTestId('compare-board-row')).toHaveLength(2)

    await user.click(screen.getByTestId('compare-filter-selftest'))
    expect(within(table).getAllByTestId('compare-board-row')).toHaveLength(1)
    expect(within(table).getByText('i2c-board')).toBeInTheDocument()

    await user.click(screen.getByTestId('compare-select-all'))
    await user.click(screen.getByTestId('compare-bulk-reboot'))
    await user.click(screen.getByTestId('compare-bulk-confirm-btn'))
    expect(rebootSpy).toHaveBeenCalledWith('st-1')
    expect(await screen.findByTestId('inspector-status')).toHaveTextContent('Sent reboot to 1 board(s)')
  })

  it('executes 2-step RMA decommission in Bulk mode', async () => {
    const user = userEvent.setup()
    const board = makeDevice()
    const decomSpy = vi.spyOn(api, 'decommissionDevice').mockResolvedValue(board)

    render(
      <DeviceInspector
        device={board}
        groups={[
          {
            id: 'g-1',
            project_id: 'p-1',
            name: 'canary',
            description: null,
            created_at: '2026-11-20T00:00:00Z',
            updated_at: '2026-11-20T00:00:00Z',
          },
        ]}
        initialMode="bulk"
        now={NOW}
      />,
    )

    expect(screen.getByLabelText('Bulk cohort ring')).toBeInTheDocument()
    await user.click(screen.getByTestId('rma-step1-btn'))
    expect(screen.getByTestId('rma-step2-confirm')).toBeInTheDocument()
    await user.click(screen.getByTestId('rma-confirm-btn'))
    expect(decomSpy).toHaveBeenCalledWith(board.device_id)
    expect(await screen.findByTestId('inspector-status')).toHaveTextContent('Decommissioned gh-node-01')
  })
})
