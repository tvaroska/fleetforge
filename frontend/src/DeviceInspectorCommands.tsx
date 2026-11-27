// Modes B (Dock), C (Compare / Diff), and D (Bulk / RMA) sub-panels for `DeviceInspector`.

import type { DeviceGroupSummary, DeviceSummary, DockSyncResponse } from './api'
import { formatBytes } from './format'

export type ComparePattern = 'all' | 'selftest' | 'safe_mode' | 'timeout'

export function DeviceInspectorDockPanel({
  devices,
  effectiveDevice,
  busy,
  dockBundleJson,
  dockReceipt,
  onChangeDockBundleJson,
  onDockReplay,
}: {
  devices: DeviceSummary[]
  effectiveDevice: DeviceSummary | null
  busy: boolean
  dockBundleJson: string
  dockReceipt: DockSyncResponse | null
  onChangeDockBundleJson: (value: string) => void
  onDockReplay: () => void
}) {
  const meshNodes = devices.length > 0 ? devices : effectiveDevice ? [effectiveDevice] : []

  return (
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
          {meshNodes.map((node, _idx, arr) => {
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
          })}
        </tbody>
      </table>

      <p>
        <label>
          Drained USB-C Dock Bundle (JSON)
          <textarea
            className="log console"
            rows={4}
            aria-label="Drained USB-C Dock Bundle (JSON)"
            data-testid="inspector-dock-bundle-input"
            placeholder='{"statuses": [], "telemetry": [], "logs": []}'
            value={dockBundleJson}
            onChange={(e) => onChangeDockBundleJson(e.target.value)}
            disabled={busy}
          />
        </label>
      </p>

      <p>
        <button
          type="button"
          data-testid="dock-sync-btn"
          disabled={busy || effectiveDevice === null || dockBundleJson.trim().length === 0}
          onClick={onDockReplay}
        >
          Replay Drained Dock Bundle
        </button>
      </p>

      {dockReceipt !== null && (
        <div className="ok" data-testid="dock-sync-receipt">
          <strong>✓ Dock Replay Receipt ({dockReceipt.device_id}):</strong> statuses:{' '}
          {dockReceipt.statuses_recorded} · telemetry batches:{' '}
          {dockReceipt.telemetry_batches_recorded} · log batches: {dockReceipt.log_batches_recorded}
        </div>
      )}
    </div>
  )
}

export function DeviceInspectorComparePanel({
  compareBaseCount,
  comparePattern,
  filteredCompareBoards,
  selectedCompareIds,
  selectedCompareBoards,
  confirmingBulkAction,
  busy,
  onSetComparePattern,
  onToggleSelectAll,
  onToggleCompareSelection,
  onSetConfirmingBulkAction,
  onBulkAction,
}: {
  compareBaseCount: number
  comparePattern: ComparePattern
  filteredCompareBoards: DeviceSummary[]
  selectedCompareIds: string[]
  selectedCompareBoards: DeviceSummary[]
  confirmingBulkAction: 'reboot' | 'rollback' | null
  busy: boolean
  onSetComparePattern: (pattern: ComparePattern) => void
  onToggleSelectAll: () => void
  onToggleCompareSelection: (deviceId: string) => void
  onSetConfirmingBulkAction: (action: 'reboot' | 'rollback' | null) => void
  onBulkAction: (action: 'reboot' | 'rollback') => void
}) {
  return (
    <div className="inspector-panel" data-testid="inspector-panel-compare">
      <div className="compare-pattern-filters" data-testid="compare-pattern-filters">
        <span className="muted">Pattern: </span>
        <button
          type="button"
          className={`flag-pill${comparePattern === 'all' ? ' active' : ''}`}
          data-testid="compare-filter-all"
          onClick={() => onSetComparePattern('all')}
        >
          All ({compareBaseCount})
        </button>{' '}
        <button
          type="button"
          className={`flag-pill${comparePattern === 'selftest' ? ' active' : ''}`}
          data-testid="compare-filter-selftest"
          onClick={() => onSetComparePattern('selftest')}
        >
          i2c / selftest
        </button>{' '}
        <button
          type="button"
          className={`flag-pill${comparePattern === 'safe_mode' ? ' active' : ''}`}
          data-testid="compare-filter-safemode"
          onClick={() => onSetComparePattern('safe_mode')}
        >
          safe_mode
        </button>{' '}
        <button
          type="button"
          className={`flag-pill${comparePattern === 'timeout' ? ' active' : ''}`}
          data-testid="compare-filter-timeout"
          onClick={() => onSetComparePattern('timeout')}
        >
          timeout
        </button>
      </div>

      <table className="compare-grid" data-testid="compare-boards-table">
        <thead>
          <tr>
            <th scope="col">
              <input
                type="checkbox"
                aria-label="Select all exception boards"
                data-testid="compare-select-all"
                checked={
                  filteredCompareBoards.length > 0 &&
                  selectedCompareBoards.length === filteredCompareBoards.length
                }
                disabled={filteredCompareBoards.length === 0}
                onChange={onToggleSelectAll}
              />
            </th>
            <th scope="col">BOARD ID</th>
            <th scope="col">CHIP</th>
            <th scope="col">VERDICT &amp; SLOT</th>
            <th scope="col">SELFTEST / RESET REASON</th>
            <th scope="col">FREE / MIN HEAP</th>
          </tr>
        </thead>
        <tbody>
          {filteredCompareBoards.length === 0 ? (
            <tr>
              <td colSpan={6} className="muted">
                No boards with active faults, rollbacks, or safe-mode alerts.
              </td>
            </tr>
          ) : (
            filteredCompareBoards.map((b) => {
              const bSafe = Boolean(b.safe_mode)
              const slot = b.deploy?.selftest?.returned_to ?? null
              const verdict = bSafe
                ? slot
                  ? `!! SAFE MODE (${slot})`
                  : '!! SAFE MODE'
                : b.deploy
                  ? slot
                    ? `${b.deploy.state} > ${slot}`
                    : b.deploy.state
                  : 'idle'
              const reason =
                b.deploy?.selftest?.reason ?? b.deploy?.detail ?? b.last_reset_reason ?? '—'
              const freeStr =
                b.health?.free_heap != null ? formatBytes(b.health.free_heap) : '—'
              const minStr =
                b.health?.min_free_heap != null ? formatBytes(b.health.min_free_heap) : '—'
              const isChecked = selectedCompareIds.includes(b.device_id)
              return (
                <tr key={b.device_id} data-testid="compare-board-row">
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Select ${b.name ?? b.device_id}`}
                      data-testid={`compare-select-${b.device_id}`}
                      checked={isChecked}
                      onChange={() => onToggleCompareSelection(b.device_id)}
                    />
                  </td>
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
            })
          )}
        </tbody>
      </table>

      <div className="compare-bulk-actions" data-testid="compare-bulk-actions">
        {confirmingBulkAction === null ? (
          <>
            <button
              type="button"
              data-testid="compare-bulk-reboot"
              disabled={busy || selectedCompareBoards.length === 0}
              onClick={() => onSetConfirmingBulkAction('reboot')}
            >
              Reboot Selected ({selectedCompareBoards.length})
            </button>{' '}
            <button
              type="button"
              data-testid="compare-bulk-rollback"
              disabled={busy || selectedCompareBoards.length === 0}
              onClick={() => onSetConfirmingBulkAction('rollback')}
            >
              Rollback Selected ({selectedCompareBoards.length})
            </button>
          </>
        ) : (
          <span data-testid="compare-bulk-confirm">
            Confirm bulk {confirmingBulkAction} on {selectedCompareBoards.length} selected board(s)?{' '}
            <button
              type="button"
              data-testid="compare-bulk-confirm-btn"
              disabled={busy}
              onClick={() => onBulkAction(confirmingBulkAction)}
            >
              Confirm Bulk {confirmingBulkAction === 'reboot' ? 'Reboot' : 'Rollback'}
            </button>{' '}
            <button
              type="button"
              data-testid="compare-bulk-cancel-btn"
              disabled={busy}
              onClick={() => onSetConfirmingBulkAction(null)}
            >
              Cancel
            </button>
          </span>
        )}
      </div>
    </div>
  )
}

export function DeviceInspectorBulkPanel({
  project,
  groups,
  effectiveDevice,
  busy,
  holdAtStaged,
  leapfrogParent,
  targetGroupId,
  confirmingRma,
  onSetHoldAtStaged,
  onSetLeapfrogParent,
  onSetTargetGroupId,
  onSetConfirmingRma,
  onConfirmRmaDecommission,
}: {
  project: string
  groups: DeviceGroupSummary[]
  effectiveDevice: DeviceSummary | null
  busy: boolean
  holdAtStaged: boolean
  leapfrogParent: boolean
  targetGroupId: string
  confirmingRma: boolean
  onSetHoldAtStaged: (value: boolean) => void
  onSetLeapfrogParent: (value: boolean) => void
  onSetTargetGroupId: (value: string) => void
  onSetConfirmingRma: (value: boolean) => void
  onConfirmRmaDecommission: () => void
}) {
  return (
    <div className="inspector-panel" data-testid="inspector-panel-bulk">
      <div className="inspector-section" data-testid="bulk-cohort-controls">
        <h4>Cohort Rollout Policy ({project})</h4>
        <p>
          <label>
            <input
              type="checkbox"
              data-testid="bulk-hold-staged"
              checked={holdAtStaged}
              onChange={(e) => onSetHoldAtStaged(e.target.checked)}
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
              onChange={(e) => onSetLeapfrogParent(e.target.checked)}
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
                onChange={(e) => onSetTargetGroupId(e.target.value)}
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
            onClick={() => onSetConfirmingRma(true)}
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
              onClick={onConfirmRmaDecommission}
            >
              Confirm RMA Decommission
            </button>{' '}
            <button type="button" disabled={busy} onClick={() => onSetConfirmingRma(false)}>
              Cancel
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
