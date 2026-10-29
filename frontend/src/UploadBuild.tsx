// "Upload a build" — R2b-fe-7, Flow 2 step 1 (`spec/flows.md`). It replaces the retired
// `docs/runbooks/upload-artifact.sh`: an admin holding only the password uploads an app
// `.bin` and finds it in the Deploy column, with no shell.
//
// Four rules shape this file.
//
// 1. **The header is advisory, the server is the authority.** The first bytes of the file
//    pre-fill target and version (`appImage.ts`) and a chip mismatch is refused HERE, in
//    the browser. The server never checks a target against the bytes and this must not
//    pretend it does. There is deliberately no client-side version regex: the server's
//    `VERSION_PATTERN` sentence is the one authority, rendered verbatim.
// 2. **Target is a select, never free text.** A typo'd target is accepted by the server
//    and then matches no board, so the build silently never shows in any Deploy list.
// 3. **Nothing here fetches on mount** and nothing is stored: no localStorage, no URL
//    beyond target/version/layout, never a log of the File.
// 4. **No progress bar.** `fetch` has no upload progress and this app has no bars.
//
// Layouts come from the adopted partition profiles (R3-fe-1), never from what boards
// announce: a board on an unnamed map announces `unknown`, which is not a name.

import { useRef, useState, type FormEvent } from 'react'
import {
  ApiError,
  api,
  type ArtifactSummary,
  type ArtifactUploaded,
  type DeviceSummary,
  type PartitionProfileSummary,
} from './api'
import { APP_IMAGE_HEAD_BYTES, readAppImage, type AppImageInfo } from './appImage'
import { adoptedNames, effectiveLayout } from './profiles'

/**
 * Given a version string and the set of versions already in use for that chip target,
 * returns the next available patch version (`x.y.(z+1)`) when `version` collides, or `null`
 * when `version` is unused or empty.
 */
export function suggestNextPatchVersion(
  version: string,
  existingVersions: Iterable<string>,
): string | null {
  const trimmed = version.trim()
  if (trimmed === '') return null
  const existing = new Set(existingVersions)
  if (!existing.has(trimmed)) return null

  const semverMatch = /^(.*?\d+\.\d+\.)(\d+)(.*)$/.exec(trimmed)
  if (semverMatch) {
    const [, prefix, patchStr, suffix] = semverMatch
    let nextPatch = Number(patchStr) + 1
    while (existing.has(`${prefix}${nextPatch}${suffix}`)) {
      nextPatch += 1
    }
    return `${prefix}${nextPatch}${suffix}`
  }

  let suffixNum = 1
  while (existing.has(`${trimmed}.${suffixNum}`)) {
    suffixNum += 1
  }
  return `${trimmed}.${suffixNum}`
}

/** The agent's build targets (`justfile` `agent_targets`), offered even on an empty fleet. */
const KNOWN_TARGETS = ['esp32', 'esp32c3', 'esp32c6', 'esp32s3']

const DEFAULT_LAYOUT = 'ab-4m-v1'

const count = (n: number) => n.toLocaleString('en-US')

/** `File.slice().arrayBuffer()` is missing in jsdom; `FileReader` exists everywhere. */
function readBytes(blob: Blob): Promise<Uint8Array> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer))
    reader.onerror = () => reject(reader.error)
    reader.readAsArrayBuffer(blob)
  })
}

/**
 * What to show for a failed upload. The server's `detail` sentences (409 "already names
 * artifact …", 413 "artifact is N bytes; an OTA slot … is 1966080") carry the numbers and
 * the next action, so they render VERBATIM. Only a body that is not one of those — empty,
 * or an HTML error page from a proxy — is replaced by a plain sentence.
 */
export function uploadErrorMessage(err: unknown, fileSize: number): string {
  const message = err instanceof Error ? err.message : ''
  const status = err instanceof ApiError ? err.status : null
  if (status === 0) return message
  if (message !== '' && !message.startsWith('<') && message !== `HTTP ${status}`) return message
  if (status === 413) {
    return (
      `This file (${count(fileSize)} bytes) is larger than the server accepts. An OTA slot is ` +
      'about 1.9 MB: pick the app .bin, not a merged or full-flash image.'
    )
  }
  return `The server refused the upload (HTTP ${status ?? 'unknown'}); try again, and if it repeats check the server log.`
}

export function UploadBuild({
  devices,
  profiles,
  artifacts,
  onUploaded,
  onSessionExpired,
  project,
  onGoToFleet,
}: {
  devices: DeviceSummary[] | null
  // The page's one profile list (`null` or absent until it loads): the options are its
  // adopted names, falling back to the prebuilt agent's layout.
  profiles?: PartitionProfileSummary[] | null
  artifacts?: ArtifactSummary[] | { byTarget?: Map<string, ArtifactSummary[]> } | null
  onUploaded: () => void
  onSessionExpired: () => void
  project?: string
  onGoToFleet?: () => void
}) {
  const [file, setFile] = useState<File | null>(null)
  const [info, setInfo] = useState<AppImageInfo | null>(null)
  const [inputKey, setInputKey] = useState(0)
  const [chosenTarget, setChosenTarget] = useState<string | null>(null)
  const [version, setVersion] = useState('')
  const [chosenLayout, setChosenLayout] = useState<string | null>(null)
  const [artifactType, setArtifactType] = useState<string>('app')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<ArtifactUploaded | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [conflictedVersions, setConflictedVersions] = useState<string[]>([])
  // A second pick before the first header read lands must win.
  const pickRef = useRef(0)

  const fleet = devices ?? []
  const fleetTargets = [...new Set(fleet.map((d) => d.platform_type))]
  const targets = [
    ...new Set([...KNOWN_TARGETS, ...fleetTargets, ...(info?.target ? [info.target] : [])]),
  ].sort()
  const target = chosenTarget ?? info?.target ?? (fleetTargets.length === 1 ? fleetTargets[0] : '')

  const names = profiles ? adoptedNames(profiles) : []
  const layouts = names.length > 0 ? names : [DEFAULT_LAYOUT]
  // The one adopted layout every board of this chip is on (the server's resolution order),
  // else the prebuilt agent's.
  const reported = new Set(
    fleet
      .filter((d) => d.platform_type === target)
      .map((d) => effectiveLayout(d, profiles ?? []))
      .filter((l): l is string => l !== null && layouts.includes(l)),
  )
  const defaultLayout =
    reported.size === 1
      ? [...reported][0]!
      : layouts.includes(DEFAULT_LAYOUT)
        ? DEFAULT_LAYOUT
        : layouts[0]!
  const layout = chosenLayout ?? defaultLayout

  const mismatch =
    artifactType === 'app' && info?.target != null && target !== '' && target !== info.target

  const existingVersionsForTarget = new Set<string>(conflictedVersions)
  for (const d of fleet) {
    if (target === '' || d.platform_type === target) {
      if (d.fw_version) existingVersionsForTarget.add(d.fw_version)
      if (d.deploy?.artifact_version) existingVersionsForTarget.add(d.deploy.artifact_version)
      if (d.deploy?.from_version) existingVersionsForTarget.add(d.deploy.from_version)
      if (d.sub_versions) {
        for (const subVer of Object.values(d.sub_versions)) {
          if (subVer) existingVersionsForTarget.add(subVer)
        }
      }
    }
  }
  if (artifacts) {
    if (Array.isArray(artifacts)) {
      for (const a of artifacts) {
        if (target === '' || a.target === target) {
          existingVersionsForTarget.add(a.version)
        }
      }
    } else if ('byTarget' in artifacts && artifacts.byTarget instanceof Map) {
      const list =
        target !== ''
          ? (artifacts.byTarget.get(target) ?? [])
          : Array.from(artifacts.byTarget.values()).flat()
      for (const a of list) {
        existingVersionsForTarget.add(a.version)
      }
    }
  }
  if (result && (target === '' || result.target === target)) {
    existingVersionsForTarget.add(result.version)
  }

  const nextPatchSuggestion = suggestNextPatchVersion(version, existingVersionsForTarget)

  async function pick(next: File | null) {
    const mine = ++pickRef.current
    setFile(next)
    setInfo(null)
    setChosenTarget(null)
    setVersion('')
    setResult(null)
    setError(null)
    if (next === null) return
    try {
      const head = await readBytes(next.slice(0, APP_IMAGE_HEAD_BYTES))
      if (mine !== pickRef.current) return
      const read = readAppImage(head)
      setInfo(read)
      setVersion(read.version ?? '')
    } catch {
      // Unreadable header: the form still works by hand.
      if (mine === pickRef.current) setInfo(readAppImage(new Uint8Array(0)))
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (file === null) return
    setBusy(true)
    setResult(null)
    setError(null)
    try {
      const uploaded = await api.uploadArtifact(file, {
        target,
        version,
        partitionLayout: layout,
        ...(artifactType !== 'app' ? { artifactType } : {}),
        ...(project ? { project } : {}),
      })
      onUploaded()
      setResult(uploaded)
      setChosenTarget(target)
      setChosenLayout(layout)
      pickRef.current++
      setFile(null)
      setInfo(null)
      setVersion('')
      setInputKey((k) => k + 1)
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
        return
      }
      if (err instanceof ApiError && err.status === 409 && version.trim() !== '') {
        setConflictedVersions((prev) =>
          prev.includes(version.trim()) ? prev : [...prev, version.trim()],
        )
      }
      setError(uploadErrorMessage(err, file.size))
    } finally {
      setBusy(false)
    }
  }

  const noBoardOfThisChip = result !== null && !fleet.some((d) => d.platform_type === result.target)

  return (
    <section className="console-card" aria-labelledby="upload-heading">
      <h2 id="upload-heading">Upload a build</h2>
      <p className="muted">
        The app <code>.bin</code> your IDE built — not a merged/full-flash image. It is listed
        under the chip target and version you give it, and offered in the Deploy column of every
        board with that chip.
      </p>
      <form className="compact-form" onSubmit={(event) => void submit(event)}>
        <p>
          <label>
            Build (.bin){' '}
            <input
              key={inputKey}
              type="file"
              accept=".bin,application/octet-stream"
              onChange={(event) => void pick(event.target.files?.[0] ?? null)}
              disabled={busy}
            />
          </label>
        </p>
        {file !== null && (
          <p className="muted" data-testid="upload-file-info">
            {file.name} — {count(file.size)} bytes
            {info?.recognised &&
              info.version !== null &&
              ` · built as ${info.projectName ?? 'an app'} ${info.version} for ${
                info.target ?? `chip id ${info.chipId}`
              }`}
            {info !== null &&
              !(info.recognised && info.version !== null) &&
              (info.recognised
                ? ' · its image header has no version to read.'
                : '. Not recognised as an ESP-IDF app image; the version and target were not read from it.')}
          </p>
        )}
        <div className="form-grid form-grid-3">
          <label>
            Chip target{' '}
            <select
              aria-label="Chip target"
              value={target}
              onChange={(event) => {
                setChosenTarget(event.target.value)
                setChosenLayout(null)
              }}
              disabled={busy}
            >
              <option value="">choose…</option>
              {targets.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          <label>
            Version{' '}
            <input
              type="text"
              aria-label="Version"
              value={version}
              onChange={(event) => setVersion(event.target.value)}
              size={24}
              disabled={busy}
            />
          </label>
          <label>
            Partition layout{' '}
            <select
              aria-label="Partition layout"
              value={layout}
              onChange={(event) => setChosenLayout(event.target.value)}
              disabled={busy}
            >
              {[...new Set([...layouts, layout])].sort().map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          </label>
          <label>
            Artifact type{' '}
            <select
              aria-label="Artifact type"
              value={artifactType}
              onChange={(event) => setArtifactType(event.target.value)}
              disabled={busy}
            >
              <option value="app">app (ESP-IDF firmware)</option>
              <option value="mcu_fw">mcu_fw (co-processor)</option>
              <option value="model_weights">model_weights (ML weights)</option>
              <option value="fpga_bitstream">fpga_bitstream (FPGA)</option>
              <option value="data_partition">data_partition (assets)</option>
            </select>
          </label>
        </div>
        <p className="muted">
          The version label is what the Deploy column shows. A label cannot be re-pointed at other
          bytes later.
        </p>
        {nextPatchSuggestion !== null && (
          <p className="warn" data-testid="version-conflict-hint">
            Version <code>{version}</code> already exists for <code>{target || 'this target'}</code>.{' '}
            <button
              type="button"
              data-testid="suggest-next-version"
              onClick={() => {
                setVersion(nextPatchSuggestion)
                setError(null)
              }}
              disabled={busy}
            >
              Use next patch: {nextPatchSuggestion}
            </button>
          </p>
        )}
        {mismatch && (
          <p className="bad" role="alert" data-testid="upload-target-mismatch">
            This file was built for {info.target} (its image header says so); a {target} board
            would refuse it. Pick {info.target}, or choose the {target} build.
          </p>
        )}
        <p>
          <button
            type="submit"
            disabled={busy || file === null || version === '' || target === '' || mismatch}
          >
            {busy ? 'Uploading…' : 'Upload'}
          </button>
        </p>
      </form>
      {result !== null && (
        <div className="upload-success-row">
          <p className="ok" data-testid="upload-result">
            {result.created
              ? `Uploaded ${result.target} ${result.version} (${result.size_bytes} bytes). It is in the Deploy list of every ${result.target} board.` +
                (noBoardOfThisChip
                  ? ` No ${result.target} board is in the fleet yet, so no Deploy list shows it until one enrolls.`
                  : '')
              : `Already uploaded: these exact bytes are ${result.target} ${result.version}. Nothing changed.`}
          </p>
          {onGoToFleet && (
            <button type="button" onClick={onGoToFleet}>
              Go to Fleet to deploy &gt;
            </button>
          )}
        </div>
      )}
      {error !== null && (
        <p className="bad" role="alert" data-testid="upload-error">
          {error}
        </p>
      )}
    </section>
  )
}
