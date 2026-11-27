import { useEffect, useState, type FormEvent } from 'react'
import { api, type Me } from '../api'

export function MyAccountSection({
  me,
  activeProject,
  open,
  activeRole,
  busy,
  onSetBusy,
  onSetError,
  onRefresh,
  onHandleError,
}: {
  me: Me
  activeProject: string
  open: boolean
  activeRole: string
  busy: boolean
  onSetBusy: (busy: boolean) => void
  onSetError: (error: string | null) => void
  onRefresh: () => Promise<void>
  onHandleError: (err: unknown) => void
}) {
  const [selfDisplayName, setSelfDisplayName] = useState(me.display_name ?? '')
  const [selfEmail, setSelfEmail] = useState(me.email ?? '')
  const [selfCurrentPassword, setSelfCurrentPassword] = useState('')
  const [selfNewPassword, setSelfNewPassword] = useState('')
  const [selfHasPassword, setSelfHasPassword] = useState(Boolean(me.has_local_password))
  const [selfStatus, setSelfStatus] = useState<string | null>(null)

  useEffect(() => {
    setSelfStatus(null)
  }, [activeProject, open])

  useEffect(() => {
    setSelfDisplayName(me.display_name ?? '')
    setSelfEmail(me.email ?? '')
    setSelfHasPassword(Boolean(me.has_local_password))
  }, [me.display_name, me.email, me.has_local_password])

  if (me.project_id) {
    return null
  }

  async function handleSaveSelfProfile(event: FormEvent) {
    event.preventDefault()
    onSetBusy(true)
    onSetError(null)
    setSelfStatus(null)
    try {
      const body: Parameters<typeof api.updateMe>[0] = {
        display_name: selfDisplayName.trim() || null,
        email: selfEmail.trim() || null,
      }
      if (selfNewPassword !== '') {
        body.password = selfNewPassword
        if (selfCurrentPassword !== '') {
          body.current_password = selfCurrentPassword
        }
      }
      const updated = await api.updateMe(body)
      setSelfDisplayName(updated.display_name ?? '')
      setSelfEmail(updated.email ?? '')
      setSelfHasPassword(Boolean(updated.has_local_password))
      setSelfCurrentPassword('')
      setSelfNewPassword('')
      setSelfStatus(
        selfNewPassword !== ''
          ? 'Account profile and password updated.'
          : 'Account profile updated.',
      )
      if (me.is_superadmin) {
        await onRefresh()
      }
    } catch (err) {
      onHandleError(err)
    } finally {
      onSetBusy(false)
    }
  }

  return (
    <div data-testid="my-account-card">
      <h3>
        My account · <code>{me.subject}</code>{' '}
        <span className="badge muted">
          {me.is_superadmin ? 'superadmin (full capability)' : activeRole}
        </span>{' '}
        <span className="badge muted">
          {selfHasPassword ? 'local password set' : 'Firebase / env login'}
        </span>
      </h3>
      <form className="compact-form" onSubmit={(e) => void handleSaveSelfProfile(e)}>
        <div className="form-grid">
          <label>
            Display name
            <input
              type="text"
              aria-label="My display name"
              value={selfDisplayName}
              onChange={(e) => setSelfDisplayName(e.target.value)}
              placeholder="Your name"
              disabled={busy}
            />
          </label>
          <label>
            Email
            <input
              type="email"
              aria-label="My email"
              value={selfEmail}
              onChange={(e) => setSelfEmail(e.target.value)}
              placeholder="you@example.com"
              disabled={busy}
            />
          </label>
          <label>
            Current password (optional)
            <input
              type="password"
              aria-label="Current password"
              value={selfCurrentPassword}
              onChange={(e) => setSelfCurrentPassword(e.target.value)}
              placeholder="Current password"
              disabled={busy}
            />
          </label>
          <label>
            New password (min 8 chars)
            <input
              type="password"
              aria-label="New password"
              value={selfNewPassword}
              onChange={(e) => setSelfNewPassword(e.target.value)}
              placeholder="Leave blank to keep current"
              disabled={busy}
            />
          </label>
        </div>
        <p>
          <button type="submit" disabled={busy}>
            Save my account
          </button>
        </p>
      </form>
      {selfStatus !== null && (
        <p className="ok" data-testid="self-status">
          {selfStatus}
        </p>
      )}
    </div>
  )
}
