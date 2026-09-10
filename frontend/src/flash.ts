// The flashing engine: every rule about what may be written to a board, and in what order.
//
// No DOM API and no esptool-js — the port lives behind the `BoardFlasher` seam
// (`flasher.ts`), so all of this runs in jsdom against a fake. Four rules shape it, and
// each one is a board that would otherwise be bricked, mis-flashed or silently inert:
//
// 1. **Every address comes from the manifest.** `builds[].parts[].offset` and
//    `config_partition.offset`, never a constant: the bootloader is at 0x1000 on ESP32
//    and 0x0 on the RISC-V parts, and a hardcoded offset flashes cleanly and never boots.
//    `planWrite` is the only place a `FlashPart` is constructed, and `flash.test.tsx`
//    asserts the addresses against the manifest fixture.
// 2. **Nothing is written that has not been checked.** Chip family, flash size, the
//    presence of a config partition, and the sha256 + length of every downloaded part —
//    all before a single byte goes to the chip.
// 3. **The enrollment token is minted LAST and revoked on failure.** It is single-use and
//    it is a fleet-join credential: minting it before the checks means every failed
//    attempt spends one, and leaving a live one baked into a half-flashed board means an
//    orphan credential nobody is tracking. The plaintext lives in a local variable for the
//    length of one call — never in React state, never in the log, never in the DOM.
// 4. **The port is always released.** `close()` in a `finally`, whatever happened.

import { useCallback, useRef, useState } from 'react'
import {
  ApiError,
  api,
  type AgentBuildInfo,
  type AgentManifest,
  type AgentPartInfo,
} from './api'
import { buildFfCfgFields, encodeFfCfg, validateFfCfg, type FlashConfigInput } from './ffcfg'
import type { BoardFlasher, ChipInfo, FlashPart, FlasherFactory } from './flasher'
import { defaultFlasherFactory, explainFlashError } from './flasher'

export type Phase = 'idle' | 'connecting' | 'detected' | 'flashing' | 'done' | 'error'

/** The esptool log panel is capped: a full flash emits a line per block. */
export const MAX_LOG_LINES = 200

export type FlashProgress = {
  /** 0-based index into the write plan. */
  partIndex: number
  partCount: number
  label: string
  /** COMPRESSED bytes — esptool-js reports the wire size, not `part.size`. */
  written: number
  total: number
}

export type FlashRequest = {
  config: FlashConfigInput
  eraseAll: boolean
  baudRate: number
}

/**
 * The build whose `chip_family` is the chip on the desk.
 *
 * `chip_family` is exactly `ESPLoader.chip.CHIP_NAME` on both sides, so this is `===` and
 * not a translation table. Flashing an ESP32-S3 bundle to an ESP32-C3 produces a board
 * that erases cleanly and never boots.
 */
export function selectBuild(manifest: AgentManifest, chip: ChipInfo): AgentBuildInfo {
  const build = manifest.builds.find((candidate) => candidate.chip_family === chip.chipName)
  if (build === undefined) {
    const available = manifest.builds.map((b) => `${b.target} (${b.chip_family})`).join(', ')
    throw new Error(
      `no agent bundle for ${chip.chipName}. Available: ${available || 'none'} — ` +
        'build one with `just agent-build <target>`.',
    )
  }
  return build
}

/** The highest byte the write plan will touch: every part, plus the config partition. */
export function requiredFlashBytes(build: AgentBuildInfo): number {
  const ends = build.parts.map((part) => part.offset + part.size)
  if (build.config_partition !== null) {
    ends.push(build.config_partition.offset + build.config_partition.size)
  }
  return Math.max(0, ...ends)
}

/**
 * Everything that must be true before a single byte is written.
 *
 * The flash-size check is ours to do: `flashSize: 'keep'` (see `esptoolFlasher.ts`) makes
 * esptool-js skip its own "doesn't fit in the available flash" guard, and a 2 MB board
 * given the 4 MB A/B layout becomes a mystery boot loop rather than a clean refusal.
 * The bound is DERIVED from the manifest — never `4 MB`, never `ab-4m-v1`.
 */
export function checkFlashable(build: AgentBuildInfo, chip: ChipInfo): void {
  if (build.config_partition === null) {
    throw new Error(
      `the ${build.target} bundle has no ff_cfg partition, so this board would have nowhere ` +
        'to read its server, network and enrollment token from — rebuild it with ' +
        '`just agent-build ' +
        build.target +
        '`.',
    )
  }
  const required = requiredFlashBytes(build)
  if (chip.flashSizeBytes < required) {
    throw new Error(
      `this board reports ${formatBytes(chip.flashSizeBytes)} of flash and the ` +
        `${build.partition_layout} layout needs ${formatBytes(required)}. Nothing was written.`,
    )
  }
}

/** MB with no decimals — flash sizes are always powers of two megabytes. */
export function formatBytes(bytes: number): string {
  return `${Math.round((bytes / (1024 * 1024)) * 10) / 10} MB`
}

/**
 * The manifest's parts plus the config blob, ascending by offset.
 *
 * Ascending because that is the order a flash write wants and the order a human reads a
 * memory map in; esptool-js does not require it, but a plan that is not sorted makes the
 * progress display jump around the address space.
 */
export function planWrite(
  build: AgentBuildInfo,
  downloaded: Map<string, Uint8Array>,
  config: Uint8Array,
): FlashPart[] {
  if (build.config_partition === null) {
    throw new Error('planWrite called for a bundle with no config partition')
  }
  const parts: FlashPart[] = build.parts.map((part) => {
    const data = downloaded.get(part.name)
    if (data === undefined) {
      throw new Error(`the ${part.name} image was not downloaded`)
    }
    return { label: part.name, address: part.offset, data }
  })
  parts.push({
    label: build.config_partition.label,
    address: build.config_partition.offset,
    data: config,
  })
  return parts.sort((a, b) => a.address - b.address)
}

/** Lowercase hex sha256, to compare against the manifest's. */
export async function sha256Hex(bytes: Uint8Array): Promise<string> {
  // Re-wrapped rather than cast: `Uint8Array` is declared over `ArrayBufferLike`, which
  // includes `SharedArrayBuffer`, and `crypto.subtle.digest` accepts neither that nor a
  // view onto one. `fetch` never produces one, so this is a narrowing, not a conversion.
  const digest = await crypto.subtle.digest('SHA-256', new Uint8Array(bytes))
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
}

/**
 * What the board will call itself: the eFuse MAC, lowercased, colons stripped.
 *
 * A PREDICTION and labelled as one in the UI. The agent reads
 * `esp_read_mac(ESP_MAC_EFUSE_FACTORY)` (`agent/main/ff_identity.c`), which is the same
 * value on a stock board — but the dashboard cannot promise it, and on QEMU it is all
 * zeros for every emulated board.
 */
export function predictDeviceId(chip: ChipInfo): string | null {
  if (chip.macAddress === null) return null
  const id = chip.macAddress.replace(/[^0-9a-fA-F]/g, '').toLowerCase()
  return /^[0-9a-f]{12}$/.test(id) ? id : null
}

export type FlashBoardState = {
  phase: Phase
  /** A human-readable "what is happening right now", shown next to the phase. */
  step: string
  chip: ChipInfo | null
  build: AgentBuildInfo | null
  error: string | null
  log: string[]
  progress: FlashProgress | null
  /** Set once a flash succeeds, so the done panel can point at the fleet table. */
  flashedDeviceId: string | null
  busy: boolean
  /** Must be called straight from a click handler — it asks for the port. */
  connect: (baudRate: number) => Promise<void>
  flash: (request: FlashRequest) => Promise<void>
  /** Back to "select a port", keeping the form as the operator filled it in. */
  reset: () => void
}

export type UseFlashBoardOptions = {
  onSessionExpired: () => void
  /** Injected by tests only: jsdom has no `navigator.serial` (see `flasher.ts`). */
  createFlasher?: FlasherFactory
}

export function useFlashBoard({
  onSessionExpired,
  createFlasher,
}: UseFlashBoardOptions): FlashBoardState {
  const [phase, setPhase] = useState<Phase>('idle')
  const [step, setStep] = useState('')
  const [chip, setChip] = useState<ChipInfo | null>(null)
  const [build, setBuild] = useState<AgentBuildInfo | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [log, setLog] = useState<string[]>([])
  const [progress, setProgress] = useState<FlashProgress | null>(null)
  const [flashedDeviceId, setFlashedDeviceId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // Refs, for the same reason as `fleet.ts`: the callbacks below must not be rebuilt on
  // every parent render, and the connected flasher outlives the call that created it.
  const factoryRef = useRef<FlasherFactory>(createFlasher ?? defaultFlasherFactory)
  factoryRef.current = createFlasher ?? defaultFlasherFactory
  const expiredRef = useRef(onSessionExpired)
  expiredRef.current = onSessionExpired
  const flasherRef = useRef<BoardFlasher | null>(null)

  const appendLog = useCallback((line: string) => {
    setLog((lines) => {
      const next = [...lines, line]
      return next.length > MAX_LOG_LINES ? next.slice(next.length - MAX_LOG_LINES) : next
    })
  }, [])

  // A dead cookie bounces to the login screen rather than rendering as a page error —
  // the rule `EnrollBoard` and `fleet.ts` already follow. Returns the message to show,
  // or null when the session gate has taken over.
  //
  // `explainFlashError` runs HERE and not only inside the adapter: the commonest failure
  // of all is `requestPort()` rejecting because the operator dismissed the chooser, and
  // that one is thrown before an adapter object exists. Untranslated it reads "Failed to
  // execute 'requestPort' on 'Serial': No port selected by the user." — which sounds like
  // a fault. It is not: it is "No board selected."
  const handle = useCallback((err: unknown): string | null => {
    if (err instanceof ApiError && err.isUnauthorized) {
      expiredRef.current()
      return null
    }
    if (err instanceof ApiError) return err.message
    const explained = explainFlashError(err)
    return explained === '' ? 'flashing failed' : explained
  }, [])

  const reset = useCallback(() => {
    const flasher = flasherRef.current
    flasherRef.current = null
    void flasher?.close()
    setPhase('idle')
    setStep('')
    setChip(null)
    setBuild(null)
    setError(null)
    setProgress(null)
    setLog([])
  }, [])

  const connect = useCallback(
    async (baudRate: number) => {
      setBusy(true)
      setError(null)
      setProgress(null)
      setFlashedDeviceId(null)
      setPhase('connecting')
      setStep('Waiting for a port…')
      // Any previous port must go before a second one is opened.
      const previous = flasherRef.current
      flasherRef.current = null
      if (previous !== null) await previous.close()
      try {
        const flasher = await factoryRef.current({ baudRate, onLog: appendLog })
        flasherRef.current = flasher
        setStep('Detecting the chip…')
        const detected = await flasher.detect()
        setChip(detected)
        setPhase('detected')
        setStep('')
      } catch (err) {
        const flasher = flasherRef.current
        flasherRef.current = null
        await flasher?.close()
        setPhase('idle')
        setStep('')
        // Cancelling the chooser is not an error state — the page must stay usable and a
        // second click must open it again. `explainFlashError` turns Chromium's
        // NotFoundError into "No board selected."
        const message = handle(err)
        if (message !== null) setError(message)
      } finally {
        setBusy(false)
      }
    },
    [appendLog, handle],
  )

  const flash = useCallback(
    async (request: FlashRequest) => {
      const flasher = flasherRef.current
      const detected = chip
      if (flasher === null || detected === null) {
        setError('Select a port and detect the board first.')
        return
      }

      setBusy(true)
      setError(null)
      setProgress(null)
      setPhase('flashing')

      // The plaintext token lives HERE and nowhere else: a local `const` for the length
      // of this call. Not React state (it would survive in a fiber and in devtools), not
      // the log, not the DOM. `tokenId` is not a secret and may be shown.
      let tokenId: string | null = null
      let minted = false

      try {
        // 1. Validate before anything is minted or downloaded — the ordering rule
        //    `POST /v1/enroll`, the simulator and `ff_cfg.py` all follow.
        setStep('Checking the configuration…')
        const fields = buildFfCfgFields(request.config)
        validateFfCfg(fields)

        // 2. The manifest, and the checks that depend on the chip.
        setStep('Reading the agent manifest…')
        const manifest = await api.agentManifest()
        const selected = selectBuild(manifest, detected)
        checkFlashable(selected, detected)
        setBuild(selected)

        // 3. Download and verify every part BEFORE minting. A truncated or garbled
        //    download is the failure that actually happens, and catching it here means
        //    the retry costs no token.
        const downloaded = new Map<string, Uint8Array>()
        for (const part of selected.parts) {
          setStep(`Downloading ${part.name}…`)
          const bytes = await api.agentPart(selected.target, part.name)
          await verifyPart(part, bytes)
          downloaded.set(part.name, bytes)
        }

        // 4. Mint last. Single-use: every attempt that got this far will write it.
        setStep('Minting an enrollment token…')
        const issued = await api.issueEnrollmentToken(null)
        tokenId = issued.id
        minted = true

        const config = encodeFfCfg({ ...fields, token: issued.token })
        const plan = planWrite(selected, downloaded, config)

        setStep(request.eraseAll ? 'Erasing and writing…' : 'Writing…')
        await flasher.write(plan, {
          eraseAll: request.eraseAll,
          onProgress: (partIndex, written, total) => {
            setProgress({
              partIndex,
              partCount: plan.length,
              label: plan[partIndex]?.label ?? `part ${partIndex + 1}`,
              written,
              total,
            })
          },
        })

        setStep('Resetting the board…')
        await flasher.finish()
        flasherRef.current = null
        setFlashedDeviceId(predictDeviceId(detected))
        setPhase('done')
        setStep('')
      } catch (err) {
        const message = handle(err)
        let text = message
        if (minted && tokenId !== null) {
          // An orphan live token is a fleet-join credential nobody is tracking, and a
          // half-flashed board has to be re-flashed anyway. Best effort, and say what
          // happened either way — the token ID is not a secret, the plaintext is.
          try {
            await api.revokeEnrollmentToken(tokenId)
            if (text !== null) text += ` — the enrollment token was revoked; retrying mints a new one.`
          } catch {
            if (text !== null) {
              text +=
                ` — the enrollment token could not be revoked. Revoke ${tokenId} from the` +
                ' list below; it is still live.'
            }
          }
        }
        if (text !== null) setError(text)
        setPhase('error')
        setStep('')
      } finally {
        // Always release the port, whatever happened. `close()` is idempotent, so the
        // successful path's `finish()` having already closed it is fine.
        await flasherRef.current?.close()
        flasherRef.current = null
        setBusy(false)
      }
    },
    [chip, handle],
  )

  return {
    phase,
    step,
    chip,
    build,
    error,
    log,
    progress,
    flashedDeviceId,
    busy,
    connect,
    flash,
    reset,
  }
}

/** Length + sha256 against the manifest. Anything else is a corrupt image on a board. */
async function verifyPart(part: AgentPartInfo, bytes: Uint8Array): Promise<void> {
  if (bytes.length !== part.size) {
    throw new Error(
      `the ${part.name} image is ${bytes.length} bytes, not the ${part.size} the manifest ` +
        'declares — the download was truncated. Nothing was written.',
    )
  }
  const digest = await sha256Hex(bytes)
  if (digest !== part.sha256) {
    throw new Error(
      `the ${part.name} image does not match its manifest sha256 — the download was ` +
        'corrupted. Nothing was written.',
    )
  }
}
