// Mode A (Triage & Bench) panel and 1-bit ASCII sparkline renderer for `DeviceInspector`.

import type { ReactNode } from 'react'
import type { DeviceSummary } from './api'
import { describeFaults, describeReset } from './deviceHealth'
import { formatAgo, formatBytes } from './format'

const SPARK_BLOCKS = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'] as const

/** Converts numeric telemetry samples into a 1-bit ASCII sparkline (`▁▂▃▄▅▆▇█`). */
export function renderAsciiSparkline(values: readonly number[]): string {
  if (values.length === 0) {
    return '—'
  }
  const slice = values.slice(0, 16)
  const min = Math.min(...slice)
  const max = Math.max(...slice)
  if (max === min) {
    return '▄'.repeat(slice.length)
  }
  return slice
    .map((v) => {
      const norm = (v - min) / (max - min)
      const idx = Math.max(
        0,
        Math.min(SPARK_BLOCKS.length - 1, Math.round(norm * (SPARK_BLOCKS.length - 1))),
      )
      return SPARK_BLOCKS[idx]!
    })
    .join('')
}

export function DeviceInspectorTriagePanel({
  effectiveDevice,
  now,
  busy,
  confirmingRollback,
  sparklineText,
  sparklineSummary,
  onSetConfirmingRollback,
  onToggleDeviceFlag,
  onConfirmRollback,
  onRebootSingle,
  onLoadSparklines,
  children,
}: {
  effectiveDevice: DeviceSummary | null
  now: number
  busy: boolean
  confirmingRollback: boolean
  sparklineText: string | null
  sparklineSummary: string | null
  onSetConfirmingRollback: (confirming: boolean) => void
  onToggleDeviceFlag: (patch: { is_dev?: boolean; stay_awake?: boolean }) => void
  onConfirmRollback: () => void
  onRebootSingle: () => void
  onLoadSparklines: () => void
  children?: ReactNode
}) {
  const isSafeMode = Boolean(effectiveDevice?.safe_mode)
  const resetInfo = effectiveDevice ? describeReset(effectiveDevice.last_reset_reason) : null
  const faultsText = effectiveDevice ? describeFaults(effectiveDevice.fault_resets) : null
  const returnedToSlot = effectiveDevice?.deploy?.selftest?.returned_to ?? null

  return (
    <div className="inspector-panel" data-testid="inspector-panel-triage">
      {effectiveDevice === null ? (
        <p className="muted" data-testid="inspector-empty">
          Select a board in the fleet grid to inspect its identity, rollback slot, vitals, and 1-bit
          telemetry sparklines.
        </p>
      ) : (
        <>
          {isSafeMode && (
            <div className="bad emcon-badge" data-testid="inspector-safe-mode-alert">
              <strong>!! SAFE MODE (R5-fw-6):</strong> 3× crash latch detected within 60 s — board
              halted on fallback partition ({returnedToSlot}).
            </div>
          )}

          <div className="inspector-section" data-testid="inspector-identity">
            <h4>
              {effectiveDevice.name ?? effectiveDevice.device_id}{' '}
              <code className="muted">({effectiveDevice.device_id})</code>
            </h4>
            <p className="muted">
              Chip: <code>{effectiveDevice.platform_type}</code> · Map:{' '}
              <code>{effectiveDevice.partition_layout ?? 'unknown'}</code> · FW:{' '}
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
                onClick={() => onToggleDeviceFlag({ is_dev: !effectiveDevice.is_dev })}
              >
                {effectiveDevice.is_dev ? '★ dev' : 'dev'}
              </button>{' '}
              <button
                type="button"
                className={`flag-pill${effectiveDevice.stay_awake ? ' active' : ''}`}
                aria-pressed={Boolean(effectiveDevice.stay_awake)}
                data-testid="inspector-toggle-stay-awake"
                disabled={busy}
                onClick={() => onToggleDeviceFlag({ stay_awake: !effectiveDevice.stay_awake })}
              >
                {effectiveDevice.stay_awake ? '⚡ stay awake' : 'stay awake'}
              </button>
            </div>
          </div>

          <div className="inspector-section" data-testid="inspector-last-update">
            <h4>Last Update &amp; Slot</h4>
            {effectiveDevice.deploy == null ? (
              <p className="muted">No deploy transactions recorded yet.</p>
            ) : (
              <p>
                State: <strong>{effectiveDevice.deploy.state}</strong>
                {effectiveDevice.deploy.artifact_version && (
                  <> &gt; {effectiveDevice.deploy.artifact_version}</>
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
                      <>
                        {' '}
                        — <code>{effectiveDevice.deploy.selftest.reason}</code>
                      </>
                    )}
                    {returnedToSlot !== null && (
                      <>
                        {' · '}returned_to: <code>{returnedToSlot}</code>
                      </>
                    )}
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
                  onClick={() => onSetConfirmingRollback(true)}
                >
                  ↶ Rollback slot{returnedToSlot ? ` (${returnedToSlot})` : ''}
                </button>
              ) : (
                <span data-testid="inspector-rollback-confirm">
                  Revert {effectiveDevice.name ?? effectiveDevice.device_id} to previous slot?{' '}
                  <button
                    type="button"
                    data-testid="inspector-rollback-confirm-btn"
                    disabled={busy}
                    onClick={onConfirmRollback}
                  >
                    Confirm Rollback
                  </button>{' '}
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => onSetConfirmingRollback(false)}
                  >
                    Cancel
                  </button>
                </span>
              )}{' '}
              <button
                type="button"
                data-testid="inspector-reboot-btn"
                disabled={busy}
                onClick={onRebootSingle}
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
                <strong className={resetInfo.fault ? 'warn' : undefined}>{resetInfo.reason}</strong>
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
                onClick={onLoadSparklines}
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
  )
}
