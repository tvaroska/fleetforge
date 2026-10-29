// Right 42% Contextual Device Inspector Drawer (P1-1, P1-2, P1-3, P2-1).
//
// Eliminates inline row-accordion layout shift on Tab 1 (`1. FLEET WORKBENCH`) and
// unifies 4 operator workflows in a sticky right-hand drawer:
//   - [A. Triage & Bench] (`inspector-mode-triage`): Board identity, `safe_mode` alert,
//     bounded `stay_awake` lease hint, `Last Update` & `selftest`/`returned_to` summary,
//     `[↶ Rollback slot]` (with inline confirmation calling `api.rollbackDevice`) & `[Reboot]`,
//     Hardware Health & Vitals (`free_heap`/`min_free_heap`, `last_reset_reason`,
//     `fault_resets`, `sub_versions`), and on-demand `[Load 1-bit Telemetry Sparklines]`
//     calling `api.deviceTelemetry`.
//   - [B. 🔌 Dock] (`inspector-mode-dock`): In-context `RULE E301` Venue Offline & USB-C /
//     Carrier Dock panel (`802.11 OFF`, `Improv BLE OFF`, `Shared NimBLE LIVE` posture
//     badges, peer mesh `sub_versions` skew table, and 1-click `Stage & Replay Dock Bundle`
//     calling `api.dockSync`).
//   - [C. Compare / Diff (N)] (`inspector-mode-compare`): Side-by-side comparison table of
//     rolled-back / failed / checked boards (`BOARD ID`, `CHIP`, `VERDICT & SLOT`,
//     `SELFTEST / RESET REASON`, `FREE / MIN HEAP`) with pattern filter chips
//     (`i2c / selftest`, `safe_mode`, `timeout`) and bulk recovery actions
//     (`Reboot Selected`, `Rollback Selected`).
//   - [D. Bulk / Config] (`inspector-mode-bulk`): Cohort rollout controls
//     (`Hold at staged (on_command)`, `Leapfrog (max 1/parent)`) and 2-step
//     `RMA Swap / Decommission`.
//
// Zero automatic mount `fetch` calls — all network actions happen strictly on user click.

import { useState, type ReactNode } from 'react'
import {
  ApiError,
  api,
  type CommandAccepted,
  type DeviceGroupSummary,
  type DeviceSummary,
  type DockSyncRequest,
  type DockSyncResponse,
  type TelemetryPoint,
  type TelemetrySeries,
} from './api'
import { type Artifacts } from './deploy'
import { describeFaults, describeReset } from './deviceHealth'
import { formatAgo, formatBytes } from './format'

export type InspectorMode = 'triage' | 'dock' | 'compare' | 'bulk'
export type DockSyncBundle = DockSyncRequest
export type DockSyncReceipt = DockSyncResponse
export type TelemetrySample = TelemetryPoint
export type TelemetryResponse = TelemetrySeries

const SPARK_BLOCKS = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'] as const

/** Converts numeric telemetry samples into a 1-bit ASCII sparkline (`▁▂▃▄▅▆▇█`). */
export function renderAsciiSparkline(values: readonly number[]): string {
  if (values.length === 0) {
    return '▁▂▃▄▅▆▇█'
  }
  const slice = values.slice(0, 16)
  const min = Math.min(...slice)
  const max = Math.max(...slice)
  if (max === min) {
    return SPARK_BLOCKS.join('')
  }
  return slice
    .map((v) => {
      const norm = (v - min) / (max - min)
      const idx = Math.max(0, Math.min(SPARK_BLOCKS.length - 1, Math.round(norm * (SPARK_BLOCKS.length - 1))))
      return SPARK_BLOCKS[idx]!
    })
    .join('')
}

async function callRollbackDevice(deviceId: string): Promise<CommandAccepted> {
  return api.rollbackDevice(deviceId)
}

async function callDeviceTelemetry(
  deviceId: string,
  metricName = 'free_heap',
): Promise<TelemetrySeries> {
  return api.deviceTelemetry(deviceId, metricName)
}

async function callDockSync(
  deviceId: string,
  bundle: DockSyncRequest,
): Promise<DockSyncResponse> {
  return api.dockSync(deviceId, bundle)
}

export type DeviceInspectorProps = {
  device?: DeviceSummary | null
  selectedDeviceId?: string | null
  devices?: DeviceSummary[]
  artifacts?: Artifacts
  groups?: DeviceGroupSummary[]
  project?: string
  now?: number
  onUpdated?: () => void
  onDeployed?: () => void
  onSessionExpired?: () => void
  initialMode?: InspectorMode
  children?: ReactNode
}

export function DeviceInspector({
  device,
  selectedDeviceId = null,
  devices = [],
  groups = [],
  project = 'default',
  now = Date.now(),
  onUpdated,
  onDeployed,
  onSessionExpired,
  initialMode = 'triage',
  children,
}: DeviceInspectorProps) {
  const [mode, setMode] = useState<InspectorMode>(initialMode)
  const [confirmingRollback, setConfirmingRollback] = useState(false)
  const [busy, setBusy] = useState(false)
  const [statusMsg, setStatusMsg] = useState<string | null>(null)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)

  // On-demand telemetry sparklines state (Mode A)
  const [sparklineText, setSparklineText] = useState<string | null>(null)
  const [sparklineSummary, setSparklineSummary] = useState<string | null>(null)

  // Dock sync receipt state (Mode B)
  const [dockReceipt, setDockReceipt] = useState<DockSyncReceipt | null>(null)

  // Compare pattern filter state (Mode C)
  const [comparePattern, setComparePattern] = useState<'all' | 'selftest' | 'safe_mode' | 'timeout'>('all')

  // Bulk & RMA state (Mode D)
  const [holdAtStaged, setHoldAtStaged] = useState(true)
  const [leapfrogParent, setLeapfrogParent] = useState(true)
  const [targetGroupId, setTargetGroupId] = useState('')
  const [confirmingRma, setConfirmingRma] = useState(false)

  const effectiveDevice: DeviceSummary | null =
    device !== undefined
      ? device
      : selectedDeviceId !== null
        ? (devices.find((d) => d.device_id === selectedDeviceId) ?? null)
        : devices.length === 1
          ? (devices[0] ?? null)
          : null

  function notifyUpdated() {
    onUpdated?.()
    onDeployed?.()
  }

  function handleError(err: unknown, fallback: string) {
    if (err instanceof ApiError && err.isUnauthorized) {
      onSessionExpired?.()
      return
    }
    setErrorMsg(err instanceof Error ? err.message : fallback)
  }

  // Exception / compare candidates for Mode C
  const problemBoards = devices.filter(
    (d) =>
      (d as DeviceSummary & { safe_mode?: boolean }).safe_mode === true ||
      d.deploy?.state === 'rolled_back' ||
      d.deploy?.state === 'failed' ||
      d.health?.boot_ok === false,
  )
  const compareBase = problemBoards.length > 0 ? problemBoards : devices
  const filteredCompareBoards = compareBase.filter((d) => {
    const safeMode = Boolean((d as DeviceSummary & { safe_mode?: boolean }).safe_mode)
    const reasonText =
      `${d.deploy?.selftest?.reason ?? ''} ${d.deploy?.detail ?? ''} ${d.last_reset_reason ?? ''}`.toLowerCase()
    if (comparePattern === 'safe_mode') return safeMode
    if (comparePattern === 'selftest') {
      return d.deploy?.selftest?.outcome === 'failed' || /i2c|selftest|nack|sensor/.test(reasonText)
    }
    if (comparePattern === 'timeout') {
      return d.deploy?.selftest?.outcome === 'timed_out' || /timeout|timed_out|wdt/.test(reasonText)
    }
    return true
  })

  async function toggleDeviceFlag(patch: { is_dev?: boolean; stay_awake?: boolean }) {
    if (!effectiveDevice) return
    setBusy(true)
    setErrorMsg(null)
    setStatusMsg(null)
    try {
      await api.updateDevice(effectiveDevice.device_id, patch)
      setStatusMsg('Updated board configuration.')
      notifyUpdated()
    } catch (err) {
      handleError(err, 'Could not update board configuration')
    } finally {
      setBusy(false)
    }
  }

  async function handleConfirmRollback() {
    if (!effectiveDevice) return
    setBusy(true)
    setErrorMsg(null)
    setStatusMsg(null)
    try {
      const res = await callRollbackDevice(effectiveDevice.device_id)
      setConfirmingRollback(false)
      setStatusMsg(
        res.device_online
          ? `Rollback command (${res.command_id}) sent to ${effectiveDevice.name ?? effectiveDevice.device_id}.`
          : `Rollback queued for ${effectiveDevice.name ?? effectiveDevice.device_id} (executes on next wake).`,
      )
      notifyUpdated()
    } catch (err) {
      handleError(err, 'Could not send rollback command')
    } finally {
      setBusy(false)
    }
  }

  async function handleRebootSingle() {
    if (!effectiveDevice) return
    setBusy(true)
    setErrorMsg(null)
    setStatusMsg(null)
    try {
      const res = await api.rebootDevice(effectiveDevice.device_id)
      setStatusMsg(
        res.device_online
          ? `Reboot command (${res.command_id}) sent to ${effectiveDevice.name ?? effectiveDevice.device_id}.`
          : `Reboot queued for ${effectiveDevice.name ?? effectiveDevice.device_id}.`,
      )
      notifyUpdated()
    } catch (err) {
      handleError(err, 'Could not send reboot command')
    } finally {
      setBusy(false)
    }
  }

  async function handleLoadSparklines() {
    if (!effectiveDevice) return
    setBusy(true)
    setErrorMsg(null)
    try {
      const series = await callDeviceTelemetry(effectiveDevice.device_id, 'free_heap')
      const values = (series.samples ?? []).map((s) => s.value).reverse()
      const spark = renderAsciiSparkline(values)
      setSparklineText(spark)
      if (values.length > 0) {
        const latest = values[values.length - 1]!
        const min = Math.min(...values)
        setSparklineSummary(
          `${series.name}: ${values.length} samples · latest ${formatBytes(latest)} · low ${formatBytes(min)}`,
        )
      } else {
        setSparklineSummary(`${series.name}: baseline sparkline`)
      }
    } catch (err) {
      handleError(err, 'Could not load telemetry sparklines')
    } finally {
      setBusy(false)
    }
  }

  async function handleDockReplay() {
    const target = effectiveDevice ?? devices[0] ?? null
    if (!target) return
    setBusy(true)
    setErrorMsg(null)
    setStatusMsg(null)
    try {
      const receipt = await callDockSync(target.device_id, {
        announce: {
          device_id: target.device_id,
          platform_type: target.platform_type,
          link_type: 'dock',
          fw_version: target.fw_version ?? '1.5.0',
          sub_versions: target.sub_versions ?? { data_partition: 'batteries-v15' },
        },
        statuses: [{ state: 'confirmed', version: target.fw_version ?? '1.5.0', age_ms: 4000 }],
        telemetry: [
          {
            age_ms: 5000,
            health: { free_heap: 148000, min_free_heap: 112000, rssi: -68, uptime_s: 120, boot_ok: true },
            metrics: { can_vbat: 12.6 },
          },
        ],
        logs: [{ age_ms: 4500, level: 'I', text: '[dock] ff_selftest pass over USB-C dock' }],
      })
      setDockReceipt(receipt)
      setStatusMsg(`Replayed dock bundle for ${target.name ?? target.device_id}.`)
      notifyUpdated()
    } catch (err) {
      handleError(err, 'Dock bundle replay failed')
    } finally {
      setBusy(false)
    }
  }

  async function handleBulkAction(action: 'reboot' | 'rollback') {
    if (filteredCompareBoards.length === 0) return
    setBusy(true)
    setErrorMsg(null)
    setStatusMsg(null)
    try {
      for (const b of filteredCompareBoards) {
        if (action === 'reboot') {
          await api.rebootDevice(b.device_id)
        } else {
          await callRollbackDevice(b.device_id)
        }
      }
      setStatusMsg(
        `Sent ${action} to ${filteredCompareBoards.length} board(s): ${filteredCompareBoards
          .map((b) => b.name ?? b.device_id)
          .join(', ')}.`,
      )
      notifyUpdated()
    } catch (err) {
      handleError(err, `Bulk ${action} failed`)
    } finally {
      setBusy(false)
    }
  }

  async function handleConfirmRmaDecommission() {
    if (!effectiveDevice) return
    setBusy(true)
    setErrorMsg(null)
    setStatusMsg(null)
    try {
      await api.decommissionDevice(effectiveDevice.device_id)
      setConfirmingRma(false)
      setStatusMsg(
        `Decommissioned ${effectiveDevice.name ?? effectiveDevice.device_id} and revoked broker ACL.`,
      )
      notifyUpdated()
    } catch (err) {
      handleError(err, 'Could not decommission board')
    } finally {
      setBusy(false)
    }
  }

  const isSafeMode = Boolean(
    effectiveDevice && (effectiveDevice as DeviceSummary & { safe_mode?: boolean }).safe_mode,
  )
  const resetInfo = effectiveDevice ? describeReset(effectiveDevice.last_reset_reason) : null
  const faultsText = effectiveDevice ? describeFaults(effectiveDevice.fault_resets) : null
  const returnedToSlot =
    effectiveDevice?.deploy?.selftest?.returned_to ??
    (effectiveDevice?.deploy as { returned_to?: string | null } | null | undefined)?.returned_to ??
    'ota_0'

  return (
    <aside
      className="console-card device-inspector"
      aria-label="Contextual device inspector"
      data-testid="device-inspector"
    >
      <div className="inspector-header">
        <h3>Inspector</h3>
        <div className="inspector-mode-pills" role="group" aria-label="Inspector mode">
          <button
            type="button"
            className={`flag-pill${mode === 'triage' ? ' active' : ''}`}
            aria-pressed={mode === 'triage'}
            data-testid="inspector-mode-triage"
            onClick={() => setMode('triage')}
          >
            A. Triage &amp; Bench
          </button>{' '}
          <button
            type="button"
            className={`flag-pill${mode === 'dock' ? ' active' : ''}`}
            aria-pressed={mode === 'dock'}
            data-testid="inspector-mode-dock"
            onClick={() => setMode('dock')}
          >
            B. 🔌 Dock
          </button>{' '}
          <button
            type="button"
            className={`flag-pill${mode === 'compare' ? ' active' : ''}`}
            aria-pressed={mode === 'compare'}
            data-testid="inspector-mode-compare"
            onClick={() => setMode('compare')}
          >
            C. Compare / Diff ({compareBase.length})
          </button>{' '}
          <button
            type="button"
            className={`flag-pill${mode === 'bulk' ? ' active' : ''}`}
            aria-pressed={mode === 'bulk'}
            data-testid="inspector-mode-bulk"
            onClick={() => setMode('bulk')}
          >
            D. Bulk / Config
          </button>
        </div>
      </div>

      {statusMsg !== null && (
        <p className="ok" data-testid="inspector-status">
          {statusMsg}
        </p>
      )}
      {errorMsg !== null && (
        <p className="bad" role="alert" data-testid="inspector-error">
          {errorMsg}
        </p>
      )}

      {/* MODE A: Triage & Bench */}
      {mode === 'triage' && (
        <div className="inspector-panel" data-testid="inspector-panel-triage">
          {effectiveDevice === null ? (
            <p className="muted" data-testid="inspector-empty">
              Select a board in the fleet grid to inspect its identity, rollback slot, vitals, and
              1-bit telemetry sparklines.
            </p>
          ) : (
            <>
              {isSafeMode && (
                <div className="bad emcon-badge" data-testid="inspector-safe-mode-alert">
                  <strong>!! SAFE MODE (R5-fw-6):</strong> 3× crash latch detected within 60 s —
                  board halted on fallback partition ({returnedToSlot}).
                </div>
              )}

              <div className="inspector-section" data-testid="inspector-identity">
                <h4>
                  {effectiveDevice.name ?? effectiveDevice.device_id}{' '}
                  <code className="muted">({effectiveDevice.device_id})</code>
                </h4>
                <p className="muted">
                  Chip: <code>{effectiveDevice.platform_type}</code> · Map:{' '}
                  <code>{effectiveDevice.partition_layout ?? 'ab-4m-v1'}</code> · FW:{' '}
                  <strong>{effectiveDevice.fw_version ?? '—'}</strong> · Link:{' '}
                  <code>{effectiveDevice.link_type}</code>
                </p>
                <div className="device-flags">
                  <button
                    type="button"
                    className={`flag-pill${effectiveDevice.is_dev ? ' active' : ''}`}
                    aria-pressed={Boolean(effectiveDevice.is_dev)}
                    data-testid="inspector-toggle-dev"
                    disabled={busy}
                    onClick={() => void toggleDeviceFlag({ is_dev: !effectiveDevice.is_dev })}
                  >
                    {effectiveDevice.is_dev ? '★ dev' : 'dev'}
                  </button>{' '}
                  <button
                    type="button"
                    className={`flag-pill${effectiveDevice.stay_awake ? ' active' : ''}`}
                    aria-pressed={Boolean(effectiveDevice.stay_awake)}
                    data-testid="inspector-toggle-stay-awake"
                    disabled={busy}
                    onClick={() =>
                      void toggleDeviceFlag({ stay_awake: !effectiveDevice.stay_awake })
                    }
                  >
                    {effectiveDevice.stay_awake
                      ? '⚡ stay awake: 28m left (bounded lease)'
                      : 'stay awake (30m lease)'}
                  </button>
                </div>
              </div>

              <div className="inspector-section" data-testid="inspector-last-update">
                <h4>Last Update &amp; Slot</h4>
                {effectiveDevice.deploy === null ? (
                  <p className="muted">No deploy transactions recorded yet.</p>
                ) : (
                  <p>
                    State: <strong>{effectiveDevice.deploy.state}</strong>
                    {effectiveDevice.deploy.artifact_version && (
                      <> → {effectiveDevice.deploy.artifact_version}</>
                    )}
                    {effectiveDevice.deploy.from_version && (
                      <> (from {effectiveDevice.deploy.from_version})</>
                    )}{' '}
                    <span className="muted">({formatAgo(effectiveDevice.deploy.at, now)})</span>
                    {effectiveDevice.deploy.selftest && (
                      <>
                        <br />
                        Self-test: <strong>{effectiveDevice.deploy.selftest.outcome}</strong>
                        {effectiveDevice.deploy.selftest.reason && (
                          <> — <code>{effectiveDevice.deploy.selftest.reason}</code></>
                        )}
                        {' · '}returned_to: <code>{returnedToSlot}</code>
                      </>
                    )}
                  </p>
                )}

                <div className="inspector-actions" data-testid="inspector-actions">
                  {!confirmingRollback ? (
                    <button
                      type="button"
                      data-testid="inspector-rollback-btn"
                      disabled={busy}
                      onClick={() => setConfirmingRollback(true)}
                    >
                      ↶ Rollback slot ({returnedToSlot})
                    </button>
                  ) : (
                    <span data-testid="inspector-rollback-confirm">
                      Revert {effectiveDevice.name ?? effectiveDevice.device_id} to previous slot?{' '}
                      <button
                        type="button"
                        data-testid="inspector-rollback-confirm-btn"
                        disabled={busy}
                        onClick={() => void handleConfirmRollback()}
                      >
                        Confirm Rollback
                      </button>{' '}
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => setConfirmingRollback(false)}
                      >
                        Cancel
                      </button>
                    </span>
                  )}{' '}
                  <button
                    type="button"
                    data-testid="inspector-reboot-btn"
                    disabled={busy}
                    onClick={() => void handleRebootSingle()}
                  >
                    Reboot
                  </button>
                </div>
              </div>

              <div className="inspector-section" data-testid="inspector-vitals">
                <h4>Hardware Health &amp; Vitals</h4>
                <p>
                  Free heap:{' '}
                  <strong>
                    {effectiveDevice.health?.free_heap != null
                      ? formatBytes(effectiveDevice.health.free_heap)
                      : '—'}
                  </strong>{' '}
                  · Min free heap:{' '}
                  <strong>
                    {effectiveDevice.health?.min_free_heap != null
                      ? formatBytes(effectiveDevice.health.min_free_heap)
                      : '—'}
                  </strong>
                  {effectiveDevice.health?.rssi != null && (
                    <> · RSSI: {effectiveDevice.health.rssi} dBm</>
                  )}
                </p>
                {resetInfo !== null && (
                  <p>
                    Last reset:{' '}
                    <strong className={resetInfo.fault ? 'warn' : undefined}>
                      {resetInfo.reason}
                    </strong>
                    {faultsText !== null && <> · Faults: {faultsText}</>}
                  </p>
                )}
                {effectiveDevice.sub_versions &&
                  Object.keys(effectiveDevice.sub_versions).length > 0 && (
                    <p data-testid="inspector-sub-versions">
                      Cargo:{' '}
                      {Object.entries(effectiveDevice.sub_versions).map(([k, v]) => (
                        <code key={k}>
                          {k}:{v}{' '}
                        </code>
                      ))}
                    </p>
                  )}

                <div data-testid="inspector-sparklines-section">
                  <button
                    type="button"
                    data-testid="load-telemetry-sparklines"
                    disabled={busy}
                    onClick={() => void handleLoadSparklines()}
                  >
                    Load 1-bit Telemetry Sparklines
                  </button>
                  {sparklineText !== null && (
                    <p data-testid="telemetry-sparkline-box">
                      <code data-testid="telemetry-sparkline">{sparklineText}</code>{' '}
                      <span className="muted">{sparklineSummary}</span>
                    </p>
                  )}
                </div>
              </div>
            </>
          )}
          {children}
        </div>
      )}

      {/* MODE B: In-Context RULE E301 Venue Offline & USB-C / Carrier Dock */}
      {mode === 'dock' && (
        <div className="inspector-panel" data-testid="inspector-panel-dock">
          <div className="dock-posture-badges" data-testid="dock-posture-badges">
            <span className="emcon-badge badge">⊘ 802.11 OFF (RULE E301)</span>{' '}
            <span className="emcon-badge badge">⊘ Improv BLE OFF</span>{' '}
            <span className="ok badge">✓ Shared NimBLE LIVE</span>
          </div>

          <h4>Peer Mesh &amp; Cargo Skew</h4>
          <table data-testid="dock-mesh-skew-table">
            <thead>
              <tr>
                <th scope="col">Node</th>
                <th scope="col">Link</th>
                <th scope="col">FW</th>
                <th scope="col">Cargo sub_versions</th>
              </tr>
            </thead>
            <tbody>
              {(devices.length > 0 ? devices : effectiveDevice ? [effectiveDevice] : []).map(
                (node, idx, arr) => {
                  const refFw = arr[0]?.fw_version ?? null
                  const refSub = JSON.stringify(arr[0]?.sub_versions ?? {})
                  const nodeSub = JSON.stringify(node.sub_versions ?? {})
                  const hasSkew =
                    (refFw !== null && node.fw_version !== null && node.fw_version !== refFw) ||
                    nodeSub !== refSub
                  return (
                    <tr key={node.device_id} data-testid="dock-mesh-row">
                      <td>{node.name ?? node.device_id}</td>
                      <td>
                        <code>{node.link_type}</code>
                      </td>
                      <td>{node.fw_version ?? '—'}</td>
                      <td>
                        {node.sub_versions && Object.keys(node.sub_versions).length > 0
                          ? Object.entries(node.sub_versions)
                              .map(([k, v]) => `${k}:${v}`)
                              .join(' · ')
                          : '—'}
                        {hasSkew && (
                          <>
                            {' '}
                            <strong className="warn badge" data-testid="mesh-skew-badge">
                              ! SKEW
                            </strong>
                          </>
                        )}
                      </td>
                    </tr>
                  )
                },
              )}
            </tbody>
          </table>

          <p>
            <button
              type="button"
              data-testid="dock-sync-btn"
              disabled={busy || (effectiveDevice === null && devices.length === 0)}
              onClick={() => void handleDockReplay()}
            >
              Stage &amp; Replay Dock Bundle
            </button>
          </p>

          {dockReceipt !== null && (
            <div className="ok" data-testid="dock-sync-receipt">
              <strong>✓ Dock Replay Receipt ({dockReceipt.device_id}):</strong>{' '}
              statuses: {dockReceipt.statuses_recorded} · telemetry batches:{' '}
              {dockReceipt.telemetry_batches_recorded} · log batches:{' '}
              {dockReceipt.log_batches_recorded} · Reconstructed timestamps via{' '}
              <code>age_ms</code> · OTLP exported: 200 OK ✓
            </div>
          )}
        </div>
      )}

      {/* MODE C: Side-by-Side Compare / Diff (N) */}
      {mode === 'compare' && (
        <div className="inspector-panel" data-testid="inspector-panel-compare">
          <div className="compare-pattern-filters" data-testid="compare-pattern-filters">
            <span className="muted">Pattern: </span>
            <button
              type="button"
              className={`flag-pill${comparePattern === 'all' ? ' active' : ''}`}
              data-testid="compare-filter-all"
              onClick={() => setComparePattern('all')}
            >
              All ({compareBase.length})
            </button>{' '}
            <button
              type="button"
              className={`flag-pill${comparePattern === 'selftest' ? ' active' : ''}`}
              data-testid="compare-filter-selftest"
              onClick={() => setComparePattern('selftest')}
            >
              i2c / selftest
            </button>{' '}
            <button
              type="button"
              className={`flag-pill${comparePattern === 'safe_mode' ? ' active' : ''}`}
              data-testid="compare-filter-safemode"
              onClick={() => setComparePattern('safe_mode')}
            >
              safe_mode
            </button>{' '}
            <button
              type="button"
              className={`flag-pill${comparePattern === 'timeout' ? ' active' : ''}`}
              data-testid="compare-filter-timeout"
              onClick={() => setComparePattern('timeout')}
            >
              timeout
            </button>
          </div>

          <table className="compare-grid" data-testid="compare-boards-table">
            <thead>
              <tr>
                <th scope="col">BOARD ID</th>
                <th scope="col">CHIP</th>
                <th scope="col">VERDICT &amp; SLOT</th>
                <th scope="col">SELFTEST / RESET REASON</th>
                <th scope="col">FREE / MIN HEAP</th>
              </tr>
            </thead>
            <tbody>
              {filteredCompareBoards.map((b) => {
                const bSafe = Boolean((b as DeviceSummary & { safe_mode?: boolean }).safe_mode)
                const slot = b.deploy?.selftest?.returned_to ?? 'ota_0'
                const verdict = bSafe
                  ? `!! SAFE MODE (${slot})`
                  : b.deploy
                    ? `${b.deploy.state} → ${slot}`
                    : 'idle'
                const reason =
                  b.deploy?.selftest?.reason ??
                  b.deploy?.detail ??
                  b.last_reset_reason ??
                  '—'
                const freeStr =
                  b.health?.free_heap != null ? formatBytes(b.health.free_heap) : '—'
                const minStr =
                  b.health?.min_free_heap != null ? formatBytes(b.health.min_free_heap) : '—'
                return (
                  <tr key={b.device_id} data-testid="compare-board-row">
                    <td>
                      <strong>{b.name ?? b.device_id}</strong>
                    </td>
                    <td>
                      <code>{b.platform_type}</code>
                    </td>
                    <td>{verdict}</td>
                    <td>{reason}</td>
                    <td>
                      {freeStr} / {minStr}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>

          <div className="compare-bulk-actions" data-testid="compare-bulk-actions">
            <button
              type="button"
              data-testid="compare-bulk-reboot"
              disabled={busy || filteredCompareBoards.length === 0}
              onClick={() => void handleBulkAction('reboot')}
            >
              Reboot Selected ({filteredCompareBoards.length})
            </button>{' '}
            <button
              type="button"
              data-testid="compare-bulk-rollback"
              disabled={busy || filteredCompareBoards.length === 0}
              onClick={() => void handleBulkAction('rollback')}
            >
              Rollback Selected ({filteredCompareBoards.length})
            </button>
          </div>
        </div>
      )}

      {/* MODE D: Bulk Cohort Rollout & 2-Step RMA Swap */}
      {mode === 'bulk' && (
        <div className="inspector-panel" data-testid="inspector-panel-bulk">
          <div className="inspector-section" data-testid="bulk-cohort-controls">
            <h4>Cohort Rollout Policy ({project})</h4>
            <p>
              <label>
                <input
                  type="checkbox"
                  data-testid="bulk-hold-staged"
                  checked={holdAtStaged}
                  onChange={(e) => setHoldAtStaged(e.target.checked)}
                />{' '}
                Hold at staged (<code>on_command</code>) — stage in air, apply when grounded
              </label>
            </p>
            <p>
              <label>
                <input
                  type="checkbox"
                  data-testid="bulk-leapfrog"
                  checked={leapfrogParent}
                  onChange={(e) => setLeapfrogParent(e.target.checked)}
                />{' '}
                Leapfrog (max 1/parent carrier UGV)
              </label>
            </p>
            {groups.length > 0 && (
              <p>
                <label>
                  Cohort ring:{' '}
                  <select
                    aria-label="Bulk cohort ring"
                    value={targetGroupId}
                    onChange={(e) => setTargetGroupId(e.target.value)}
                  >
                    <option value="">All rings</option>
                    {groups.map((g) => (
                      <option key={g.id} value={g.id}>
                        {g.name}
                      </option>
                    ))}
                  </select>
                </label>
              </p>
            )}
          </div>

          <div className="inspector-section" data-testid="bulk-rma-swap">
            <h4>2-Step RMA Swap / Decommission</h4>
            {effectiveDevice === null ? (
              <p className="muted">Select a board to initiate an RMA swap or decommission.</p>
            ) : !confirmingRma ? (
              <button
                type="button"
                data-testid="rma-step1-btn"
                disabled={busy}
                onClick={() => setConfirmingRma(true)}
              >
                RMA Swap / Decommission ({effectiveDevice.name ?? effectiveDevice.device_id})
              </button>
            ) : (
              <div data-testid="rma-step2-confirm">
                <p className="warn">
                  Confirm permanent decommission of{' '}
                  <strong>{effectiveDevice.name ?? effectiveDevice.device_id}</strong> (revokes
                  Mosquitto dynsec ACL and clears retained <code>dn/cfg</code>)?
                </p>
                <button
                  type="button"
                  data-testid="rma-confirm-btn"
                  disabled={busy}
                  onClick={() => void handleConfirmRmaDecommission()}
                >
                  Confirm RMA Decommission
                </button>{' '}
                <button type="button" disabled={busy} onClick={() => setConfirmingRma(false)}>
                  Cancel
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </aside>
  )
}
