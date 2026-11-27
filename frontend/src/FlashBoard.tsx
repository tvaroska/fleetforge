// "Flash a board" — R0-fe-3: plug in a board over USB, write the agent plus its `ff_cfg`
// partition from the browser, and watch it come online.
//
// Rules live in `flash.ts` and `ffcfg.ts`; Web Serial lives behind the Port, Adapter and
// Contract boundary in `flasher.ts`. No `localStorage`, `sessionStorage`, `indexedDB`,
// cookies, or URL query parameters are ever touched for Wi-Fi passphrases or enrollment tokens.

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api, type AgentManifest, type DeviceGroupSummary } from './api'
import { BoardConsolePanel } from './BoardConsole'
import { type ConsoleFactory } from './boardConsole'
import { OTHER_BOARD_ID, shortlist } from './boards'
import { uriSecrets, type DiagnosticContext } from './diagnostics'
import {
  MAX_NETWORKS,
  agentReadsNets,
  buildFfCfgFields,
  validateFfCfg,
  type FlashConfigInput,
} from './ffcfg'
import { predictDeviceId, useFlashBoard } from './flash'
import {
  BUILT_IN_PORT,
  NO_BOARD_SELECTED,
  webSerialSupported,
  type FlasherFactory,
} from './flasher'
import { BAUD_RATES, FlashConfigForm, type FormState, type NetworkRow } from './FlashConfigForm'
import {
  FlashActionSection,
  FlashDetectSection,
  FlashStepperRail,
  FlashUnavailable,
} from './FlashStepPanels'
import { type Fleet } from './fleet'
import { type ResultContext } from './onboardingResult'
import { describePreflight, installFor } from './preflight'
import { takeBaseline, type FleetBaseline } from './serverWatch'
import { type VersionLine } from './statusStrip'

function defaultMqttUri(): string {
  if (typeof window === 'undefined') return 'mqtt://localhost:8883'
  const scheme = window.location.protocol === 'https:' ? 'mqtts' : 'mqtt'
  return `${scheme}://${window.location.hostname}:8883`
}

function defaultApiBase(): string {
  return typeof window === 'undefined' ? '' : window.location.origin
}

const INITIAL_FORM: FormState = {
  mqttUri: defaultMqttUri(),
  link: 'wifi',
  networks: [{ key: 0, ssid: '', psk: '' }],
  hbS: '',
  power: 'always_on',
  wakeS: '',
  baudRate: BAUD_RATES[0],
}

function validationMessage(input: FlashConfigInput): string | null {
  try {
    validateFfCfg(buildFfCfgFields(input))
    return null
  } catch (err) {
    return err instanceof Error ? err.message : 'the configuration is not valid'
  }
}

export function FlashBoard({
  onSessionExpired,
  createFlasher,
  createConsole,
  onBoardIdentified,
  fleet,
  versions,
  project,
  groups,
  onGoToFleet,
}: {
  onSessionExpired: () => void
  onBoardIdentified?: (deviceId: string) => void
  fleet?: Pick<Fleet, 'devices' | 'arrivals' | 'error' | 'now'> & Partial<Pick<Fleet, 'refresh'>>
  versions?: VersionLine
  createFlasher?: FlasherFactory
  createConsole?: ConsoleFactory
  project?: string
  groups?: DeviceGroupSummary[]
  onGoToFleet?: () => void
}) {
  const [form, setForm] = useState<FormState>(INITIAL_FORM)
  const [selectedGroupId, setSelectedGroupId] = useState<string>('')
  const [batchPrefix, setBatchPrefix] = useState<string>('')
  const [batchSeq, setBatchSeq] = useState<number>(1)
  const [board, setBoard] = useState<string>(OTHER_BOARD_ID)
  const [manifest, setManifest] = useState<AgentManifest | null>(null)
  const [manifestError, setManifestError] = useState<string | null>(null)
  const [serverVersion, setServerVersion] = useState<string | null>(null)
  const state = useFlashBoard({ onSessionExpired, createFlasher, project })
  const [portHelpOpen, setPortHelpOpen] = useState(false)

  const supported = webSerialSupported()
  const secure = typeof window === 'undefined' || window.isSecureContext

  const set = useCallback(<K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((current) => ({ ...current, [key]: value }))
  }, [])

  const nextNetworkKey = useRef(1)
  const setNetwork = useCallback((index: number, patch: Partial<Omit<NetworkRow, 'key'>>) => {
    setForm((current) => ({
      ...current,
      networks: current.networks.map((row, i) => (i === index ? { ...row, ...patch } : row)),
    }))
  }, [])
  const addNetwork = useCallback(() => {
    setForm((current) => {
      if (current.networks.length >= MAX_NETWORKS) return current
      const key = nextNetworkKey.current
      nextNetworkKey.current += 1
      return { ...current, networks: [...current.networks, { key, ssid: '', psk: '' }] }
    })
  }, [])
  const removeNetwork = useCallback((index: number) => {
    if (index === 0) return
    setForm((current) => ({
      ...current,
      networks: current.networks.filter((_, i) => i !== index),
    }))
  }, [])

  useEffect(() => {
    if (!supported || !secure) return
    let live = true
    api
      .agentManifest()
      .then((value) => {
        if (live) setManifest(value)
      })
      .catch((err: unknown) => {
        if (!live) return
        if (err instanceof ApiError && err.isUnauthorized) {
          onSessionExpired()
          return
        }
        setManifestError(err instanceof Error ? err.message : 'could not read the agent manifest')
      })
    api
      .health()
      .then((value) => {
        if (live) setServerVersion(value.version)
      })
      .catch(() => {})
    return () => {
      live = false
    }
  }, [supported, secure, onSessionExpired])

  const identified = useRef(onBoardIdentified)
  identified.current = onBoardIdentified
  const predictedId = state.chip === null ? null : predictDeviceId(state.chip)
  useEffect(() => {
    if (predictedId !== null) identified.current?.(predictedId)
  }, [predictedId])
  useEffect(() => {
    if (state.flashedDeviceId !== null) identified.current?.(state.flashedDeviceId)
  }, [state.flashedDeviceId])

  const [flashBaseline, setFlashBaseline] = useState<FleetBaseline | null>(null)
  const [flashBaselineWanted, setFlashBaselineWanted] = useState(false)
  const fleetDevices = fleet?.devices ?? null
  const devicesRef = useRef(fleetDevices)
  devicesRef.current = fleetDevices
  const hasFleet = fleet !== undefined
  useEffect(() => {
    if (state.phase === 'flashing') {
      if (!hasFleet) return
      const devices = devicesRef.current
      setFlashBaseline(devices === null ? null : takeBaseline(devices))
      setFlashBaselineWanted(devices === null)
    } else if (state.phase === 'idle') {
      setFlashBaseline(null)
      setFlashBaselineWanted(false)
    }
  }, [state.phase, hasFleet])

  useEffect(() => {
    if (!flashBaselineWanted || fleetDevices === null) return
    setFlashBaselineWanted(false)
    setFlashBaseline(takeBaseline(fleetDevices))
  }, [flashBaselineWanted, fleetDevices])

  useEffect(() => {
    if (state.error === NO_BOARD_SELECTED || state.error === BUILT_IN_PORT) setPortHelpOpen(true)
  }, [state.error])

  if (!supported) return <FlashUnavailable reason="no-web-serial" />
  if (!secure) return <FlashUnavailable reason="insecure" />

  const chip = state.chip
  const candidates = chip === null ? [] : shortlist(chip)
  const mixedVersions =
    manifest !== null && new Set(manifest.builds.map((b) => b.agent_version)).size > 1
  const predicted = chip === null ? null : predictDeviceId(chip)
  const install = chip === null ? null : installFor(manifest, chip)
  const preflight =
    chip !== null && fleet !== undefined
      ? describePreflight({
          predictedId: predicted,
          devices: fleet.devices,
          arrivals: fleet.arrivals,
          fleetError: fleet.error,
          install,
        })
      : null
  const apiBase = defaultApiBase()
  const wifi = form.link === 'wifi'
  const [firstNetwork, ...extraNetworks] = form.networks
  const readsNets = install === null ? null : agentReadsNets(install.agentVersion)
  const config: FlashConfigInput = {
    apiBase,
    mqttUri: form.mqttUri,
    link: form.link,
    ssid: wifi ? firstNetwork.ssid : undefined,
    psk: wifi ? firstNetwork.psk : undefined,
    nets:
      wifi && extraNetworks.length > 0
        ? extraNetworks.map(({ ssid, psk }) => ({ ssid, psk }))
        : undefined,
    hbS: form.hbS,
    power: form.power,
    wakeS: form.wakeS,
  }
  const configError = validationMessage(config)

  const groupId = selectedGroupId || null
  const recoverByReflash = () => state.reflash({ config, baudRate: form.baudRate, groupId })

  const trimmedBatchPrefix = batchPrefix.trim()
  const formattedBatchSeq = String(batchSeq).padStart(2, '0')
  const suggestedBatchName =
    trimmedBatchPrefix !== '' ? `${trimmedBatchPrefix}${formattedBatchSeq}` : null
  const targetDeviceId = (state.flashedDeviceId ?? predicted)?.toLowerCase() ?? null
  const effectiveDevices =
    fleet?.devices == null
      ? null
      : suggestedBatchName !== null && targetDeviceId !== null
        ? fleet.devices.map((d) =>
            d.device_id.toLowerCase() === targetDeviceId && (d.name === null || d.name === '')
              ? { ...d, name: suggestedBatchName }
              : d,
          )
        : fleet.devices

  const resultContext: ResultContext = {
    devices: effectiveDevices,
    versions: versions ?? null,
    flashed:
      state.flashedDeviceId !== null || state.build !== null
        ? {
            deviceId: state.flashedDeviceId ?? predicted,
            agentVersion: state.build?.agent_version ?? null,
            layout: state.build?.partition_layout ?? null,
            link: form.link,
            ssid:
              wifi && form.networks.length === 1 && firstNetwork.ssid !== ''
                ? firstNetwork.ssid
                : null,
          }
        : null,
    flashBaseline,
    now: fleet?.now,
  }

  const failure = state.failure
  const retryAction =
    failure === null ? null : configError !== null ? (
      <span className="muted">Fill in the network details in step 2 to try again.</span>
    ) : (
      <button
        type="button"
        className="remedy"
        disabled={state.busy}
        onClick={() => {
          const baudRate = failure.retryBaud ?? form.baudRate
          if (failure.retryBaud !== null) set('baudRate', baudRate)
          void state.reflash({ config, baudRate, groupId })
        }}
      >
        {failure.retryBaud !== null ? `Retry at ${failure.retryBaud}` : 'Try the flash again'}
      </button>
    )

  const diagnostics: DiagnosticContext = {
    chip,
    build: state.build,
    agentVersion: manifest?.agent_version ?? null,
    serverVersion,
    config: {
      apiBase,
      mqttUri: form.mqttUri,
      link: form.link,
      ssid: firstNetwork.ssid,
      networks: form.networks.map((row) => row.ssid),
      ntp: null,
      hbS: form.hbS || null,
      power: form.power,
      wakeS: form.wakeS || null,
      pskLength: firstNetwork.psk.length,
    },
    knownSecrets: [
      ...form.networks.map((row) => row.psk),
      ...uriSecrets(form.mqttUri),
      ...uriSecrets(apiBase),
    ],
  }

  const step1Done = chip !== null
  const step2Done = chip !== null && configError === null
  const step3Done = state.phase === 'done'

  return (
    <section className="console-card" aria-labelledby="flash-heading">
      <h2 id="flash-heading">Flash a board</h2>
      <p className="muted">
        Connect an ESP32 over USB, confirm what it is, and write the agent plus this board&apos;s
        own configuration. A single-use enrollment token is minted for it automatically — you do not
        need to generate one below.
      </p>

      <FlashStepperRail
        step1Done={step1Done}
        step2Done={step2Done}
        step3Done={step3Done}
        flashing={state.phase === 'flashing'}
      />

      {manifestError !== null && (
        <p className="bad" role="alert">
          {manifestError}
        </p>
      )}
      {manifest !== null && (
        <p className="muted" data-testid="agent-targets">
          {mixedVersions ? 'Agent per target · ' : `Agent ${manifest.agent_version} · `}
          {manifest.builds
            .map((b) =>
              mixedVersions
                ? `${b.target} (${b.chip_family}, ${b.agent_version})`
                : `${b.target} (${b.chip_family})`,
            )
            .join(', ')}
        </p>
      )}

      <FlashDetectSection
        baudRate={form.baudRate}
        busy={state.busy}
        chip={chip}
        predicted={predicted}
        candidates={candidates}
        board={board}
        portHelpOpen={portHelpOpen}
        preflight={preflight}
        fleetNow={fleet?.now}
        showPreflight={fleet !== undefined && state.phase !== 'done'}
        onChangeBaud={(rate) => set('baudRate', rate)}
        onConnect={() => void state.connect(form.baudRate)}
        onTogglePortHelp={setPortHelpOpen}
        onSelectBoard={setBoard}
      />

      <FlashConfigForm
        apiBase={apiBase}
        form={form}
        readsNets={readsNets}
        install={install}
        onSetField={set}
        onSetNetwork={setNetwork}
        onAddNetwork={addNetwork}
        onRemoveNetwork={removeNetwork}
      />

      <FlashActionSection
        state={state}
        chip={chip}
        manifest={manifest}
        configError={configError}
        preflight={preflight}
        groups={groups}
        selectedGroupId={selectedGroupId}
        batchPrefix={batchPrefix}
        formattedBatchSeq={formattedBatchSeq}
        suggestedBatchName={suggestedBatchName}
        retryAction={retryAction}
        onSelectGroup={setSelectedGroupId}
        onChangeBatchPrefix={setBatchPrefix}
        onFlash={() => void state.flash({ config, baudRate: form.baudRate, groupId })}
        onFlashAnother={() => {
          if (trimmedBatchPrefix !== '') {
            setBatchSeq((n) => (n < 99 ? n + 1 : 1))
          }
          state.reset()
        }}
        onGoToFleet={onGoToFleet}
      />

      <BoardConsolePanel
        autoWatch={state.phase === 'done'}
        createConsole={createConsole}
        diagnostics={diagnostics}
        result={resultContext}
        hideResult={failure !== null}
        naming={
          fleet === undefined
            ? undefined
            : { onSessionExpired, onSaved: fleet.refresh ?? (() => {}) }
        }
        onReflash={configError === null ? recoverByReflash : undefined}
        reflashBlockedReason={
          configError === null
            ? null
            : 'Fill in the network details in step 2 to re-flash from here.'
        }
      />
    </section>
  )
}
