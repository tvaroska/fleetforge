// Contextual Device Inspector Drawer (P1-1, P1-2, P1-3, P2-1).
// Unifies Triage & Bench, RULE E301 Dock sync, side-by-side Compare/Diff, and Bulk/RMA
// workflows in a sticky right-hand drawer with zero automatic mount `fetch` calls.

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
import {
  DeviceInspectorBulkPanel,
  DeviceInspectorComparePanel,
  DeviceInspectorDockPanel,
  type ComparePattern,
} from './DeviceInspectorCommands'
import { DeviceInspectorTriagePanel, renderAsciiSparkline } from './DeviceInspectorHealth'
import { formatBytes } from './format'

export { renderAsciiSparkline }
export type InspectorMode = 'triage' | 'dock' | 'compare' | 'bulk'
export type DockSyncBundle = DockSyncRequest
export type DockSyncReceipt = DockSyncResponse
export type TelemetrySample = TelemetryPoint
export type TelemetryResponse = TelemetrySeries

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

  const [sparklineText, setSparklineText] = useState<string | null>(null)
  const [sparklineSummary, setSparklineSummary] = useState<string | null>(null)

  const [dockBundleJson, setDockBundleJson] = useState<string>('')
  const [dockReceipt, setDockReceipt] = useState<DockSyncReceipt | null>(null)

  const [comparePattern, setComparePattern] = useState<ComparePattern>('all')
  const [selectedCompareIds, setSelectedCompareIds] = useState<string[]>([])
  const [confirmingBulkAction, setConfirmingBulkAction] = useState<'reboot' | 'rollback' | null>(
    null,
  )

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

  const compareBase = devices.filter(
    (d) =>
      d.safe_mode === true ||
      d.deploy?.state === 'rolled_back' ||
      d.deploy?.state === 'failed' ||
      d.health?.boot_ok === false,
  )
  const filteredCompareBoards = compareBase.filter((d) => {
    const safeMode = Boolean(d.safe_mode)
    const reasonText =
      `${d.deploy?.selftest?.reason ?? ''} ${d.deploy?.detail ?? ''} ${d.last_reset_reason ?? ''}`.toLowerCase()
    if (comparePattern === 'safe_mode') return safeMode
    if (comparePattern === 'selftest') {
      return d.deploy?.selftest?.outcome === 'failed' || /i2c|selftest|nack|sensor/.test(reasonText)
    }
    if (comparePattern === 'timeout') {
      return (
        d.deploy?.selftest?.outcome === 'timed_out' || /timeout|timed_out|wdt/.test(reasonText)
      )
    }
    return true
  })
  const selectedCompareBoards = filteredCompareBoards.filter((d) =>
    selectedCompareIds.includes(d.device_id),
  )

  function toggleCompareSelection(deviceId: string) {
    setConfirmingBulkAction(null)
    setSelectedCompareIds((prev) =>
      prev.includes(deviceId) ? prev.filter((id) => id !== deviceId) : [...prev, deviceId],
    )
  }

  function toggleSelectAllCompare() {
    setConfirmingBulkAction(null)
    if (
      filteredCompareBoards.length > 0 &&
      selectedCompareBoards.length === filteredCompareBoards.length
    ) {
      setSelectedCompareIds([])
    } else {
      setSelectedCompareIds(filteredCompareBoards.map((d) => d.device_id))
    }
  }

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
        setSparklineSummary(`${series.name}: 0 samples recorded`)
      }
    } catch (err) {
      handleError(err, 'Could not load telemetry sparklines')
    } finally {
      setBusy(false)
    }
  }

  async function handleDockReplay() {
    if (!effectiveDevice) {
      setErrorMsg('Select a board before replaying a dock bundle.')
      return
    }
    const raw = dockBundleJson.trim()
    if (!raw) {
      setErrorMsg('Paste a drained USB-C dock JSON bundle before replaying.')
      return
    }
    let parsed: DockSyncRequest
    try {
      parsed = JSON.parse(raw) as DockSyncRequest
    } catch {
      setErrorMsg('Dock bundle is not valid JSON.')
      return
    }
    setBusy(true)
    setErrorMsg(null)
    setStatusMsg(null)
    try {
      const receipt = await callDockSync(effectiveDevice.device_id, parsed)
      setDockReceipt(receipt)
      setStatusMsg(`Replayed dock bundle for ${effectiveDevice.name ?? effectiveDevice.device_id}.`)
      notifyUpdated()
    } catch (err) {
      handleError(err, 'Dock bundle replay failed')
    } finally {
      setBusy(false)
    }
  }

  async function handleBulkAction(action: 'reboot' | 'rollback') {
    if (selectedCompareBoards.length === 0) return
    setBusy(true)
    setErrorMsg(null)
    setStatusMsg(null)
    try {
      for (const b of selectedCompareBoards) {
        if (action === 'reboot') {
          await api.rebootDevice(b.device_id)
        } else {
          await callRollbackDevice(b.device_id)
        }
      }
      setConfirmingBulkAction(null)
      setStatusMsg(
        `Sent ${action} to ${selectedCompareBoards.length} board(s): ${selectedCompareBoards
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

      {mode === 'triage' && (
        <DeviceInspectorTriagePanel
          effectiveDevice={effectiveDevice}
          now={now}
          busy={busy}
          confirmingRollback={confirmingRollback}
          sparklineText={sparklineText}
          sparklineSummary={sparklineSummary}
          onSetConfirmingRollback={setConfirmingRollback}
          onToggleDeviceFlag={(patch) => void toggleDeviceFlag(patch)}
          onConfirmRollback={() => void handleConfirmRollback()}
          onRebootSingle={() => void handleRebootSingle()}
          onLoadSparklines={() => void handleLoadSparklines()}
        >
          {children}
        </DeviceInspectorTriagePanel>
      )}

      {mode === 'dock' && (
        <DeviceInspectorDockPanel
          devices={devices}
          effectiveDevice={effectiveDevice}
          busy={busy}
          dockBundleJson={dockBundleJson}
          dockReceipt={dockReceipt}
          onChangeDockBundleJson={setDockBundleJson}
          onDockReplay={() => void handleDockReplay()}
        />
      )}

      {mode === 'compare' && (
        <DeviceInspectorComparePanel
          compareBaseCount={compareBase.length}
          comparePattern={comparePattern}
          filteredCompareBoards={filteredCompareBoards}
          selectedCompareIds={selectedCompareIds}
          selectedCompareBoards={selectedCompareBoards}
          confirmingBulkAction={confirmingBulkAction}
          busy={busy}
          onSetComparePattern={setComparePattern}
          onToggleSelectAll={toggleSelectAllCompare}
          onToggleCompareSelection={toggleCompareSelection}
          onSetConfirmingBulkAction={setConfirmingBulkAction}
          onBulkAction={(action) => void handleBulkAction(action)}
        />
      )}

      {mode === 'bulk' && (
        <DeviceInspectorBulkPanel
          project={project}
          groups={groups}
          effectiveDevice={effectiveDevice}
          busy={busy}
          holdAtStaged={holdAtStaged}
          leapfrogParent={leapfrogParent}
          targetGroupId={targetGroupId}
          confirmingRma={confirmingRma}
          onSetHoldAtStaged={setHoldAtStaged}
          onSetLeapfrogParent={setLeapfrogParent}
          onSetTargetGroupId={setTargetGroupId}
          onSetConfirmingRma={setConfirmingRma}
          onConfirmRmaDecommission={() => void handleConfirmRmaDecommission()}
        />
      )}
    </aside>
  )
}
