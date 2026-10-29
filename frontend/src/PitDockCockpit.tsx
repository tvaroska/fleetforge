// Full-Width 3-Column `RULE E301` Venue Offline & USB-C Pit Cockpit (`P2-1` + `H6` Enhancement #2).
//
// Designed for Tab 3 (`3. ADD BOARD & DOCK`) during 90-second competition pit turnarounds
// and contested-RF carrier-deck operations:
// 1. Top EMCON Posture Banner (`data-testid="emcon-posture-banner"`): explicit radio lock
//    pills (`802.11 Wi-Fi: OFF`, `Improv BLE: OFF`, `Shared NimBLE GATT: LIVE`), Pit Cache
//    status, and queued dock bundle counter.
// 2. Pinned Live Mini-Fleet & Peer Mesh Strip (`data-testid="pit-mini-fleet-strip"`): shows
//    live `fw_version`, `link_type`, and Peer Mesh `sub_versions` Skew Detection (`! SKEW`)
//    when any peer node's `fw_version` or `sub_versions` (e.g. `batteries_csv`) disagrees
//    with the mesh target.
// 3. 3-Column Pit Turnaround Grid:
//    - Panel 1: Auto-Drained Field Rings & Cloud Replay (`api.dockSync` + receipt).
//    - Panel 2: Stage Signed Bundle over USB-C Dock (`app` or `data_partition` with Safe Window).
//    - Panel 3: Peripheral `ff_selftest` Verification (`SX1262 915MHz LoRa SPI`,
//      `SN65HVD230 CAN Loopback`, `Shared NimBLE GATT ble_hs_synced()`) + locked
//      `[Factory Full Flash (Locked 🔒)]` guard protecting `ff_sec` NVS keys.

import { useMemo, useState } from 'react'
import {
  ApiError,
  api,
  type ArtifactSummary,
  type DeviceSummary,
  type DockSyncRequest,
  type DockSyncResponse,
} from './api'

export type PitDockBundle = DockSyncRequest
export type PitDockReceipt = DockSyncResponse

export type PitDockCockpitProps = {
  devices?: DeviceSummary[] | null
  artifacts?: ArtifactSummary[] | { byTarget?: Map<string, ArtifactSummary[]> } | null
  project?: string
  selectedDeviceId?: string | null
  onSelectDevice?: (deviceId: string) => void
  onSynced?: () => void
  onSessionExpired?: () => void
}

type MeshNode = {
  device_id: string
  name: string
  roleLabel: string
  platform_type: string
  fw_version: string
  link_type: string
  offline_mode: boolean
  sub_versions: Record<string, string>
  secrets_rev: number
}

function buildEmptyDockBundle(deviceId: string, platformType = 'esp32s3', fwVersion = '1.0.0'): PitDockBundle {
  return {
    announce: {
      device_id: deviceId,
      platform_type: platformType,
      link_type: 'dock',
      fw_version: fwVersion,
    },
    statuses: [],
    telemetry: [],
    logs: [],
  }
}

function normalizeArtifacts(
  artifacts?: ArtifactSummary[] | { byTarget?: Map<string, ArtifactSummary[]> } | null,
): ArtifactSummary[] {
  if (!artifacts) return []
  if (Array.isArray(artifacts)) return artifacts
  if ('byTarget' in artifacts && artifacts.byTarget instanceof Map) {
    const out: ArtifactSummary[] = []
    for (const list of artifacts.byTarget.values()) {
      out.push(...list)
    }
    return out
  }
  return []
}

function toMeshNodes(devices?: DeviceSummary[] | null): MeshNode[] {
  if (!devices || devices.length === 0) {
    return []
  }
  return devices.map((d, idx) => ({
    device_id: d.device_id,
    name: d.name ?? d.device_id,
    roleLabel: d.name ?? `node-${idx + 1}`,
    platform_type: d.platform_type,
    fw_version: d.fw_version ?? 'unknown',
    link_type: d.link_type || 'wifi',
    offline_mode: Boolean((d as { offline_mode?: boolean }).offline_mode ?? false),
    sub_versions: d.sub_versions ?? {},
    secrets_rev: d.secrets_rev ?? 0,
  }))
}

/**
 * Determine target `fw_version` and target `sub_versions` across the peer mesh so any node
 * lagging behind (e.g. `batteries_csv: v14` when peers are on `v15`) is flagged with `! SKEW`.
 */
function detectMeshSkew(nodes: MeshNode[]): {
  targetFw: string | null
  targetSubs: Record<string, string>
  skewByDevice: Record<string, string[]>
} {
  if (nodes.length === 0) {
    return { targetFw: null, targetSubs: {}, skewByDevice: {} }
  }
  // Pick the highest/first known fw_version as mesh reference
  const knownFws = nodes.map((n) => n.fw_version).filter((v) => v !== 'unknown')
  const targetFw = knownFws.sort().at(-1) ?? null

  const targetSubs: Record<string, string> = {}
  for (const node of nodes) {
    for (const [k, v] of Object.entries(node.sub_versions)) {
      if (!targetSubs[k] || v > targetSubs[k]) {
        targetSubs[k] = v
      }
    }
  }

  const skewByDevice: Record<string, string[]> = {}
  for (const node of nodes) {
    const reasons: string[] = []
    if (targetFw !== null && node.fw_version !== 'unknown' && node.fw_version !== targetFw) {
      reasons.push(`app:${node.fw_version} != ${targetFw}`)
    }
    for (const [key, expected] of Object.entries(targetSubs)) {
      const actual = node.sub_versions[key]
      if (!actual) {
        reasons.push(`${key}:missing (expected ${expected})`)
      } else if (actual !== expected) {
        reasons.push(`${key}:${actual} != ${expected}`)
      }
    }
    skewByDevice[node.device_id] = reasons
  }

  return { targetFw, targetSubs, skewByDevice }
}

export function PitDockCockpit({
  devices,
  artifacts,
  selectedDeviceId,
  onSelectDevice,
  onSynced,
  onSessionExpired,
}: PitDockCockpitProps) {
  const meshNodes = useMemo(() => toMeshNodes(devices), [devices])
  const { targetFw, targetSubs, skewByDevice } = useMemo(
    () => detectMeshSkew(meshNodes),
    [meshNodes],
  )

  const [chosenDeviceId, setChosenDeviceId] = useState<string>('')
  const effectiveDeviceId =
    chosenDeviceId ||
    (selectedDeviceId && meshNodes.some((n) => n.device_id === selectedDeviceId)
      ? selectedDeviceId
      : (meshNodes[0]?.device_id ?? ''))

  const activeNode =
    meshNodes.find((n) => n.device_id === effectiveDeviceId) ?? meshNodes[0] ?? null
  const activeDevice =
    (devices ?? []).find((d) => d.device_id === effectiveDeviceId) ?? null

  const [bundleText, setBundleText] = useState<string>('')
  const [syncBusy, setSyncBusy] = useState<boolean>(false)
  const [syncError, setSyncError] = useState<string | null>(null)
  const [receipt, setReceipt] = useState<PitDockReceipt | null>(null)

  const artifactList = useMemo(() => normalizeArtifacts(artifacts), [artifacts])
  const stageChoices = useMemo(
    () =>
      artifactList.map((a) => ({
        id: `${a.artifact_type ?? 'app'}:${a.version}:${a.target}`,
        label: `[${a.artifact_type ?? 'app'}] ${a.version} (${a.target} · ${a.sig ? '✓ signed' : 'unsigned'})`,
        kind: a.artifact_type ?? 'app',
        version: a.version,
      })),
    [artifactList],
  )

  const [selectedStageId, setSelectedStageId] = useState<string>('')
  const activeStageChoice =
    stageChoices.find((c) => c.id === selectedStageId) ?? stageChoices[0] ?? null

  function selectBoard(deviceId: string) {
    setChosenDeviceId(deviceId)
    onSelectDevice?.(deviceId)
    setSyncError(null)
  }

  function loadTemplateBundle() {
    if (!activeNode) return
    const template = buildEmptyDockBundle(
      activeNode.device_id,
      activeNode.platform_type,
      activeNode.fw_version !== 'unknown' ? activeNode.fw_version : '1.0.0',
    )
    setBundleText(JSON.stringify(template, null, 2))
    setSyncError(null)
  }

  // Parse current bundle for live ring summary
  const parsedSummary = useMemo(() => {
    if (!bundleText.trim()) {
      return { valid: false, statuses: 0, telemetry: 0, logs: 0, prevBootLines: 0 }
    }
    try {
      const parsed = JSON.parse(bundleText) as PitDockBundle
      const statuses = Array.isArray(parsed.statuses) ? parsed.statuses.length : 0
      const telemetry = Array.isArray(parsed.telemetry) ? parsed.telemetry.length : 0
      const logs = Array.isArray(parsed.logs) ? parsed.logs.length : 0
      let prevBootLines = 0
      if (Array.isArray(parsed.logs)) {
        for (const batch of parsed.logs) {
          const lines = (batch as { lines?: Array<{ msg?: string; text?: string }> }).lines
          if (Array.isArray(lines)) {
            for (const line of lines) {
              const text = line.msg ?? line.text ?? ''
              if (text.includes('[prev-boot]')) prevBootLines += 1
            }
          }
        }
      }
      return { valid: true, statuses, telemetry, logs, prevBootLines }
    } catch {
      return { valid: false, statuses: 0, telemetry: 0, logs: 0, prevBootLines: 0 }
    }
  }, [bundleText])

  async function submitDockSync() {
    if (!effectiveDeviceId) {
      setSyncError('Enroll or select a board before replaying a dock bundle.')
      return
    }
    setSyncBusy(true)
    setSyncError(null)
    let parsed: PitDockBundle
    try {
      parsed = JSON.parse(bundleText) as PitDockBundle
    } catch {
      setSyncBusy(false)
      setSyncError('Dock bundle JSON is not valid JSON.')
      return
    }

    try {
      const res = await api.dockSync(effectiveDeviceId, parsed)
      const statusesCount = res.ingested_statuses ?? res.statuses_recorded ?? 0
      const telemetryCount = res.ingested_telemetry ?? res.telemetry_batches_recorded ?? 0
      const logsCount = res.ingested_logs ?? res.log_batches_recorded ?? 0
      const corruptedCount = res.corrupted_lines ?? 0

      setReceipt({
        ...res,
        statuses_recorded: statusesCount,
        telemetry_batches_recorded: telemetryCount,
        log_batches_recorded: logsCount,
        ingested_statuses: statusesCount,
        ingested_telemetry: telemetryCount,
        ingested_logs: logsCount,
        corrupted_lines: corruptedCount,
      })
      onSynced?.()
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired?.()
        return
      }
      setSyncError(err instanceof Error ? err.message : 'Dock sync replay failed')
    } finally {
      setSyncBusy(false)
    }
  }

  return (
    <section
      className="console-card pit-cockpit"
      aria-labelledby="pit-dock-heading"
      data-testid="pit-dock-cockpit"
    >
      <h2 id="pit-dock-heading">Pit &amp; Carrier Dock Cockpit (RULE E301)</h2>

      {/* Top EMCON Posture Banner */}
      <div
        className="emcon-badge"
        data-testid="emcon-posture-banner"
        role="status"
        aria-label="EMCON radio posture"
      >
        <strong>⊘ VENUE OFFLINE / EMCON MODE (RULE E301)</strong>{' '}
        <span data-testid="emcon-wifi-pill">[⊘ 802.11 Wi-Fi: OFF]</span>{' '}
        <span data-testid="emcon-improv-pill">[⊘ Improv BLE: OFF]</span>{' '}
        <span data-testid="emcon-nimble-pill">[✓ Shared NimBLE GATT: LIVE]</span>
      </div>

      {/* H6 Pinned Live Mini-Fleet & Peer Mesh Strip */}
      <div className="mini-fleet-strip" data-testid="pit-mini-fleet-strip">
        <h3>
          Pinned Live Mini-Fleet &amp; Peer Mesh Strip{' '}
          {targetFw !== null && (
            <span className="muted">
              (Target: {targetFw}
              {Object.keys(targetSubs).length > 0
                ? ` · ${Object.entries(targetSubs)
                    .map(([k, v]) => `${k}:${v}`)
                    .join(' · ')}`
                : ''}
              )
            </span>
          )}
        </h3>
        {meshNodes.length === 0 ? (
          <p className="muted" data-testid="pit-empty-fleet">
            No enrolled boards in this project yet.
          </p>
        ) : (
          <div className="form-grid form-grid-3">
            {meshNodes.map((node, index) => {
              const skewReasons = skewByDevice[node.device_id] ?? []
              const hasSkew = skewReasons.length > 0
              const isSelected = node.device_id === effectiveDeviceId
              return (
                <div
                  key={node.device_id}
                  className={`console-card${isSelected ? ' active' : ''}`}
                  data-testid={`pit-mesh-node-${node.name}`}
                  data-device-id={node.device_id}
                >
                  <div>
                    <strong>
                      {index + 1}. {node.name}
                    </strong>{' '}
                    <code>({node.device_id})</code>{' '}
                    <button
                      type="button"
                      onClick={() => selectBoard(node.device_id)}
                      aria-pressed={isSelected}
                    >
                      {isSelected ? '▶ Active on Dock' : 'Select'}
                    </button>
                  </div>
                  <div className="muted">
                    Link: <code>{node.link_type}</code> · FW: <code>{node.fw_version}</code> ·
                    sec:r{node.secrets_rev}
                  </div>
                  <div>
                    {Object.entries(node.sub_versions).map(([k, v]) => (
                      <span key={k}>
                        <code>
                          {k}:{v}
                        </code>{' '}
                      </span>
                    ))}
                    {hasSkew ? (
                      <span className="bad" data-testid="peer-mesh-skew">
                        ! SKEW ({skewReasons.join(', ')})
                      </span>
                    ) : (
                      <span className="ok">✓ mesh synced</span>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* 3-Column Pit Turnaround Grid */}
      <div className="form-grid form-grid-3 pit-turnaround-grid">
        {/* Panel 1: Auto-Drained Field Rings & Cloud Replay */}
        <section
          className="console-card"
          aria-labelledby="pit-panel-drain-heading"
          data-testid="pit-panel-drain"
        >
          <h3 id="pit-panel-drain-heading">1 · Drained Field Rings &amp; Cloud Replay</h3>
          <p>
            <label>
              Docked board{' '}
              <select
                aria-label="Docked board"
                data-testid="dock-device-select"
                value={effectiveDeviceId}
                onChange={(e) => selectBoard(e.target.value)}
                disabled={syncBusy || meshNodes.length === 0}
              >
                {meshNodes.length === 0 ? (
                  <option value="">(no boards)</option>
                ) : (
                  meshNodes.map((node) => (
                    <option key={node.device_id} value={node.device_id}>
                      {node.name} ({node.device_id})
                    </option>
                  ))
                )}
              </select>
            </label>{' '}
            <button
              type="button"
              data-testid="dock-generate-sample"
              onClick={loadTemplateBundle}
              disabled={syncBusy || !activeNode}
            >
              Insert Empty Skeleton
            </button>
          </p>

          <ul className="muted" data-testid="dock-drained-summary">
            <li>
              <code>up/status</code>: {parsedSummary.statuses} transition(s) in bundle
            </li>
            <li>
              <code>up/telemetry</code>: {parsedSummary.telemetry} batch(es) with relative{' '}
              <code>age_ms</code>
            </li>
            <li>
              <code>up/log</code>: {parsedSummary.logs} batch(es) ({parsedSummary.prevBootLines}{' '}
              <code>[prev-boot]</code> crash ring line(s))
            </li>
          </ul>

          <p>
            <label>
              USB-C Dock Drain Bundle (JSON)
              <textarea
                className="log console"
                rows={6}
                aria-label="USB-C Dock Drain Bundle (JSON)"
                data-testid="dock-bundle-input"
                placeholder='{"announce": {...}, "statuses": [], "telemetry": [], "logs": []}'
                value={bundleText}
                onChange={(e) => setBundleText(e.target.value)}
                disabled={syncBusy}
              />
            </label>
          </p>

          <p>
            <button
              type="button"
              data-testid="dock-sync-submit"
              onClick={() => void submitDockSync()}
              disabled={syncBusy || !effectiveDeviceId || !parsedSummary.valid}
            >
              {syncBusy ? 'Replaying…' : 'Replay Dock Bundle to Server'}
            </button>
          </p>

          {syncError !== null && (
            <p className="bad" role="alert" data-testid="dock-sync-error">
              {syncError}
            </p>
          )}

          {receipt !== null && (
            <div className="issued" data-testid="dock-sync-receipt">
              <h4>Dock Replay Receipt ({receipt.device_id})</h4>
              <p className="ok">
                ✓ Cloud replay verified (link: <code>{receipt.link_type}</code>, fw:{' '}
                <code>{receipt.fw_version ?? 'unchanged'}</code>)
              </p>
              <dl>
                <dt>ingested_statuses</dt>
                <dd data-testid="receipt-ingested-statuses">
                  {receipt.ingested_statuses ?? receipt.statuses_recorded ?? 0}
                </dd>
                <dt>ingested_telemetry</dt>
                <dd data-testid="receipt-ingested-telemetry">
                  {receipt.ingested_telemetry ?? receipt.telemetry_batches_recorded ?? 0}
                </dd>
                <dt>ingested_logs</dt>
                <dd data-testid="receipt-ingested-logs">
                  {receipt.ingested_logs ?? receipt.log_batches_recorded ?? 0}
                </dd>
                <dt>corrupted_lines</dt>
                <dd data-testid="receipt-corrupted-lines">{receipt.corrupted_lines ?? 0}</dd>
              </dl>
            </div>
          )}
        </section>

        {/* Panel 2: Stage Signed Bundle over USB-C Dock */}
        <section
          className="console-card"
          aria-labelledby="pit-panel-stage-heading"
          data-testid="pit-panel-stage"
        >
          <h3 id="pit-panel-stage-heading">2 · USB-C Dock CLI Stage Reference</h3>
          <p className="muted">
            When Wi-Fi is disabled under <code>RULE E301</code>, stream a signed{' '}
            <code>app</code> or <code>data_partition</code> artifact to{' '}
            <code>ff_ota_dock_stage</code> over USB-C and sync the drained bundle with the CLI:
          </p>
          {stageChoices.length > 0 && (
            <p>
              <label>
                Uploaded artifact{' '}
                <select
                  aria-label="Signed artifact bundle"
                  data-testid="pit-stage-select"
                  value={activeStageChoice?.id ?? ''}
                  onChange={(e) => setSelectedStageId(e.target.value)}
                >
                  {stageChoices.map((choice) => (
                    <option key={choice.id} value={choice.id}>
                      {choice.label}
                    </option>
                  ))}
                </select>
              </label>
            </p>
          )}
          <pre className="log" data-testid="pit-cli-command">
            {`fleetforge dock-sync ${effectiveDeviceId || '<device_id>'} dock_bundle.json`}
          </pre>
        </section>

        {/* Panel 3: Last Reported Self-Test Outcome */}
        <section
          className="console-card"
          aria-labelledby="pit-panel-selftest-heading"
          data-testid="pit-panel-selftest"
        >
          <h3 id="pit-panel-selftest-heading">3 · Reported Self-Test Outcome</h3>
          {activeDevice?.deploy?.selftest ? (
            <p data-testid="pit-selftest-checklist">
              Outcome: <strong>{activeDevice.deploy.selftest.outcome}</strong>
              {activeDevice.deploy.selftest.reason && (
                <>
                  {' '}
                  — <code>{activeDevice.deploy.selftest.reason}</code>
                </>
              )}
              {activeDevice.deploy.selftest.returned_to && (
                <>
                  {' '}
                  (returned to <code>{activeDevice.deploy.selftest.returned_to}</code>)
                </>
              )}
            </p>
          ) : (
            <p className="muted" data-testid="pit-selftest-checklist">
              No self-test result reported for the selected board yet.
            </p>
          )}
          <p className="muted">
            In Venue Offline Mode, <code>ff_mqtt_confirm_offline()</code> requires a registered{' '}
            <code>ff_selftest</code> callback to pass before marking a <code>PENDING_VERIFY</code>{' '}
            OTA slot valid.
          </p>
        </section>
      </div>
    </section>
  )
}
