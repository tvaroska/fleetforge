// The partition-profiles section (R3-fe-1): the flash maps this server deploys to, and the
// one action an operator takes on them, naming a detected map. Render only; the read and
// the pure helpers are in `profiles.ts`, the name rules in `profileName.ts`.
//
// Naming a detected map IS adopting it (R3-be-2 D3): the row moves from "Detected, not
// named yet" to the named table, and builds uploaded under that name can then be deployed
// to the boards on it. There is no create-by-fingerprint form and no forget-pending button
// here; the API has both (`POST` / `DELETE /v1/partition-profiles`), see
// `docs/features/board-profiles.md`.
//
// Every device-controlled string (a detecting board's id, a fingerprint) renders as text.

import { useRef, useState, type FormEvent } from 'react'
import { ApiError, api, type DeviceSummary, type PartitionProfileSummary } from './api'
import { formatAgo } from './format'
import { checkProfileName } from './profileName'
import { ORIGIN_LABELS, pendingProfiles, shortSha, type Profiles } from './profiles'

const count = (n: number) => n.toLocaleString('en-US')

/**
 * The naming form of one pending row.
 *
 * MODULE-LEVEL ON PURPOSE, and keyed by fingerprint by its caller: the page re-renders at
 * 1 Hz, so a component declared inside the section would remount mid-typing. The drafts are
 * never resynced from props (the `NameBoard` rule); an SSE re-read must not clobber them.
 *
 * Sends `{layout_id}`, plus `ota_slot_size` only when the row has no measured slot (the
 * server answers 422 without one and 409 to a differing one). The server's sentences render
 * verbatim.
 */
function AdoptProfile({
  sha,
  measuredSlot,
  onSessionExpired,
  onAdopted,
}: {
  sha: string
  measuredSlot: number | null
  onSessionExpired: () => void
  onAdopted: (name: string) => void
}) {
  const [draft, setDraft] = useState('')
  const [slotDraft, setSlotDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const inFlight = useRef(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (inFlight.current) return
    const check = checkProfileName(draft)
    if (!check.ok) {
      setMessage(check.reason)
      return
    }
    let slot: number | undefined
    if (measuredSlot === null) {
      const n = Number(slotDraft.trim())
      if (slotDraft.trim() === '' || !Number.isInteger(n) || n <= 0) {
        setMessage('this map has no measured OTA slot: enter its size in bytes, a whole number')
        return
      }
      slot = n
    }
    inFlight.current = true
    setSaving(true)
    setMessage(null)
    try {
      const row = await api.adoptPartitionProfile(
        sha,
        slot === undefined ? { layout_id: check.name } : { layout_id: check.name, ota_slot_size: slot },
      )
      onAdopted(row.layout_id ?? check.name)
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
        return
      }
      setMessage(
        err instanceof Error && err.message !== '' ? err.message : 'the map could not be named',
      )
    } finally {
      inFlight.current = false
      setSaving(false)
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)}>
      <label>
        Name for this map{' '}
        <input
          type="text"
          value={draft}
          autoComplete="off"
          spellCheck={false}
          placeholder="e.g. min-spiffs-4m"
          onChange={(e) => {
            setDraft(e.target.value)
            setMessage(null)
          }}
        />
      </label>{' '}
      {measuredSlot === null && (
        <>
          <label>
            OTA slot size (bytes){' '}
            <input
              type="text"
              inputMode="numeric"
              value={slotDraft}
              autoComplete="off"
              onChange={(e) => {
                setSlotDraft(e.target.value)
                setMessage(null)
              }}
            />
          </label>{' '}
        </>
      )}
      <button type="submit" disabled={saving}>
        {saving ? 'Naming…' : 'Name and adopt'}
      </button>
      <span className="muted"> A name is final once adopted.</span>
      {message !== null && (
        <p className="bad" role="alert" data-testid="profile-adopt-error">
          {message}
        </p>
      )}
    </form>
  )
}

function PendingRow({
  profile,
  devices,
  now,
  onSessionExpired,
  onAdopted,
}: {
  profile: PartitionProfileSummary
  devices: DeviceSummary[] | null
  now: number
  onSessionExpired: () => void
  onAdopted: (name: string) => void
}) {
  const first = profile.detected_device_id
  const board = first === null ? undefined : (devices ?? []).find((d) => d.device_id === first)
  const boards = profile.device_ids.length
  return (
    <div
      data-testid="profile-row"
      data-sha={profile.partition_table_sha256}
      data-origin={profile.origin}
      data-deployable="false"
    >
      <p>
        First seen on{' '}
        {first === null ? (
          'a board'
        ) : board?.name ? (
          <strong>{board.name}</strong>
        ) : (
          <code>{first}</code>
        )}{' '}
        <span className="muted">({formatAgo(profile.created_at, now)})</span>
        {' · '}
        {boards} {boards === 1 ? 'board' : 'boards'} on this map now
      </p>
      <p className="muted">
        OTA slot {profile.ota_slot_size === null ? '—' : `${count(profile.ota_slot_size)} bytes`}
        {' · '}flash chip{' '}
        {profile.flash_chip_size === null ? '—' : `${count(profile.flash_chip_size)} bytes`}
      </p>
      <p>
        Fingerprint <code>{profile.partition_table_sha256}</code>
      </p>
      <AdoptProfile
        key={profile.partition_table_sha256}
        sha={profile.partition_table_sha256}
        measuredSlot={profile.ota_slot_size}
        onSessionExpired={onSessionExpired}
        onAdopted={onAdopted}
      />
    </div>
  )
}

export function PartitionProfiles({
  profiles,
  devices,
  now,
  onSessionExpired,
}: {
  profiles: Profiles
  devices: DeviceSummary[] | null
  now: number
  onSessionExpired: () => void
}) {
  const [adopted, setAdopted] = useState<string | null>(null)
  const list = profiles.profiles
  const named = (list ?? []).filter((p) => p.deployable)
  const pending = pendingProfiles(list ?? [])

  function handleAdopted(name: string) {
    setAdopted(name)
    profiles.reload()
  }

  return (
    <section aria-labelledby="profiles-heading" data-testid="profiles">
      <h2 id="profiles-heading">Partition profiles</h2>
      <p className="muted">
        The flash maps this server deploys to. A board on a map it does not know is listed under
        Detected until you name it; builds for it are then uploaded under that name.
      </p>

      {profiles.error !== null && (
        <p className="bad" data-testid="profiles-error">
          {profiles.error} — the partition profiles could not be read.
        </p>
      )}

      {adopted !== null && (
        <p className="ok" role="status" data-testid="profile-adopted">
          Named {adopted}. Upload builds under {adopted} in the form below; boards on this map can
          then take them.
        </p>
      )}

      {list === null ? (
        profiles.error === null && <p aria-busy="true">Loading…</p>
      ) : (
        <>
          <table>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Kind</th>
                <th scope="col">OTA slot</th>
                <th scope="col">Boards</th>
                <th scope="col">Fingerprint</th>
              </tr>
            </thead>
            <tbody>
              {named.map((p) => (
                <tr
                  key={p.partition_table_sha256}
                  data-testid="profile-row"
                  data-sha={p.partition_table_sha256}
                  data-origin={p.origin}
                  data-deployable="true"
                >
                  <td>
                    <code>{p.layout_id}</code>
                  </td>
                  <td>{ORIGIN_LABELS[p.origin] ?? p.origin}</td>
                  <td>{p.ota_slot_size === null ? '—' : `${count(p.ota_slot_size)} bytes`}</td>
                  <td>{p.device_ids.length}</td>
                  <td>
                    <code title={p.partition_table_sha256}>{shortSha(p.partition_table_sha256)}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {pending.length > 0 && (
            <div data-testid="profiles-detected">
              <h3>Detected, not named yet</h3>
              <p className="muted">
                Boards on these maps are refused deploys until the map is named.
              </p>
              {pending.map((p) => (
                <PendingRow
                  key={p.partition_table_sha256}
                  profile={p}
                  devices={devices}
                  now={now}
                  onSessionExpired={onSessionExpired}
                  onAdopted={handleAdopted}
                />
              ))}
            </div>
          )}
        </>
      )}
    </section>
  )
}
