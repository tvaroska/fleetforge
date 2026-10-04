// Name a board (R2b-fe-6): the last line of Flow 1, on the success result card. Also built
// to be reused by a fleet-table rename (props: id, current name, two callbacks).
//
// MODULE-LEVEL ON PURPOSE, and keyed by device id by its caller. The panel that hosts it
// re-renders at 1 Hz; a component declared inside would be a new type on every render and
// remount mid-typing. The draft is read from `currentName` once, at mount, and never
// resynced from props: an SSE re-read must not clobber what the operator is typing. After
// a save it takes the server's normalised value.
//
// Sends `{name}` and nothing else. Never `group_id`: merge-patch reads an explicit null as
// "ungroup". The operator's text is never logged.

import { useRef, useState, type FormEvent } from 'react'
import { api, ApiError } from './api'
import { checkBoardName } from './boardName'

type Message = { tone: 'ok' | 'bad'; text: string }

export function NameBoard({
  deviceId,
  currentName,
  onSessionExpired,
  onSaved,
}: {
  deviceId: string
  currentName: string | null
  onSessionExpired: () => void
  onSaved: () => void
}) {
  const [draft, setDraft] = useState(currentName ?? '')
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState<Message | null>(null)
  // A second submit before React has re-rendered the disabled button (Enter, double click).
  const inFlight = useRef(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (inFlight.current) return
    const check = checkBoardName(draft)
    if (!check.ok) {
      setMessage({ tone: 'bad', text: check.reason })
      return
    }
    inFlight.current = true
    setSaving(true)
    setMessage(null)
    try {
      const row = await api.updateDevice(deviceId, { name: check.name })
      setDraft(row.name ?? '')
      setMessage({
        tone: 'ok',
        text:
          row.name === null
            ? 'Name cleared. The fleet table shows the device id.'
            : `Saved. The fleet table shows “${row.name}”, with the id beneath it.`,
      })
      onSaved()
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
        return
      }
      setMessage({
        tone: 'bad',
        text: err instanceof Error && err.message !== '' ? err.message : 'the name could not be saved',
      })
    } finally {
      inFlight.current = false
      setSaving(false)
    }
  }

  return (
    <form className="name-board" data-testid="name-board" onSubmit={(e) => void submit(e)}>
      <h4>Name this board</h4>
      <label>
        Board name
        <input
          type="text"
          value={draft}
          autoComplete="off"
          spellCheck={false}
          placeholder="e.g. coop door"
          onChange={(e) => {
            setDraft(e.target.value)
            setMessage(null)
          }}
        />
      </label>{' '}
      <button type="submit" disabled={saving}>
        {saving ? 'Saving…' : 'Save name'}
      </button>
      <p className="muted">
        A label shown in the fleet table instead of the id. The id stays beside it, and deploys
        still address the board by its id.
      </p>
      {message !== null && (
        <p data-testid="name-board-message" role="status" className={message.tone}>
          {message.text}
        </p>
      )}
    </form>
  )
}
