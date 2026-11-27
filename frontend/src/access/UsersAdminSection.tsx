import { useEffect, useState, type FormEvent } from 'react'
import { api, type Me, type UserSummary } from '../api'
import { emailDeliveryError, type PendingConfirm } from './types'

export function UsersAdminSection({
  me,
  activeProject,
  open,
  busy,
  pending,
  users,
  onSetBusy,
  onSetError,
  onSetPending,
  onSelectProject,
  onRefresh,
  onHandleError,
}: {
  me: Me
  activeProject: string
  open: boolean
  busy: boolean
  pending: PendingConfirm | null
  users: UserSummary[] | null
  onSetBusy: (busy: boolean) => void
  onSetError: (error: string | null) => void
  onSetPending: (pending: PendingConfirm | null) => void
  onSelectProject: (slug: string) => void
  onRefresh: () => Promise<void>
  onHandleError: (err: unknown) => void
}) {
  const [newSlug, setNewSlug] = useState('')
  const [newName, setNewName] = useState('')
  const [inviteUsername, setInviteUsername] = useState('')
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteDisplayName, setInviteDisplayName] = useState('')
  const [invitePassword, setInvitePassword] = useState('')
  const [inviteSendEmail, setInviteSendEmail] = useState(true)
  const [inviteSuperadmin, setInviteSuperadmin] = useState(false)
  const [inviteProjectRole, setInviteProjectRole] = useState<'' | 'owner' | 'operator' | 'viewer'>(
    '',
  )
  const [editingUser, setEditingUser] = useState<string | null>(null)
  const [editDisplayName, setEditDisplayName] = useState('')
  const [editEmail, setEditEmail] = useState('')
  const [editPassword, setEditPassword] = useState('')
  const [editSuperadmin, setEditSuperadmin] = useState(false)
  const [userNotice, setUserNotice] = useState<string | null>(null)

  useEffect(() => {
    setUserNotice(null)
  }, [activeProject, open])

  if (!me.is_superadmin) {
    return null
  }

  async function handleCreateProject(event: FormEvent) {
    event.preventDefault()
    onSetBusy(true)
    onSetError(null)
    try {
      const created = await api.createProject({ slug: newSlug.trim(), name: newName.trim() })
      setNewSlug('')
      setNewName('')
      await onRefresh()
      onSelectProject(created.slug)
    } catch (err) {
      onHandleError(err)
    } finally {
      onSetBusy(false)
    }
  }

  async function handleInviteUser(event: FormEvent) {
    event.preventDefault()
    onSetBusy(true)
    onSetError(null)
    setUserNotice(null)
    try {
      const payload: Parameters<typeof api.createUser>[0] = {
        username: inviteUsername.trim(),
        email: inviteEmail.trim() || null,
        display_name: inviteDisplayName.trim() || null,
        password: invitePassword !== '' ? invitePassword : null,
        is_superadmin: inviteSuperadmin,
        send_invite_email: inviteEmail.trim() !== '' && inviteSendEmail,
      }
      if (inviteProjectRole !== '') {
        payload.project = activeProject
        payload.role = inviteProjectRole
      }
      const created = await api.createUser(payload)
      setInviteUsername('')
      setInviteEmail('')
      setInviteDisplayName('')
      setInvitePassword('')
      setInviteSuperadmin(false)
      setInviteProjectRole('')
      await onRefresh()
      const deliveryError = emailDeliveryError(
        created.email_delivery,
        'User saved, but the setup email could not be sent. Try "Send reset email" again.',
      )
      if (deliveryError !== null) {
        onSetError(deliveryError)
      } else {
        setUserNotice(`Saved user ${created.username}.`)
      }
    } catch (err) {
      onHandleError(err)
    } finally {
      onSetBusy(false)
    }
  }

  function startEditUser(user: UserSummary) {
    setEditingUser(user.username)
    setEditDisplayName(user.display_name ?? '')
    setEditEmail(user.email ?? '')
    setEditPassword('')
    setEditSuperadmin(user.is_superadmin)
    onSetError(null)
    setUserNotice(null)
  }

  async function handleSaveUserEdit(event: FormEvent) {
    event.preventDefault()
    if (!editingUser) {
      return
    }
    onSetBusy(true)
    onSetError(null)
    setUserNotice(null)
    try {
      const patch: Parameters<typeof api.updateUser>[1] = {
        display_name: editDisplayName.trim() || null,
        email: editEmail.trim() || null,
        is_superadmin: editSuperadmin,
      }
      if (editPassword !== '') {
        patch.password = editPassword
      }
      const updated = await api.updateUser(editingUser, patch)
      setEditingUser(null)
      setEditPassword('')
      await onRefresh()
      setUserNotice(
        `Updated ${updated.username}${editPassword !== '' ? ' (password updated)' : ''}.`,
      )
    } catch (err) {
      onHandleError(err)
    } finally {
      onSetBusy(false)
    }
  }

  async function handleDeleteUser(username: string) {
    onSetBusy(true)
    onSetError(null)
    setUserNotice(null)
    try {
      await api.deleteUser(username)
      if (editingUser === username) {
        setEditingUser(null)
      }
      await onRefresh()
      setUserNotice(`Deleted user ${username}.`)
    } catch (err) {
      onHandleError(err)
    } finally {
      onSetBusy(false)
      onSetPending(null)
    }
  }

  async function handleToggleUserDisabled(user: UserSummary) {
    onSetError(null)
    try {
      await api.updateUser(user.username, { disabled: user.disabled_at === null })
      await onRefresh()
    } catch (err) {
      onHandleError(err)
    } finally {
      onSetPending(null)
    }
  }

  async function handleSendPasswordReset(user: UserSummary) {
    onSetError(null)
    try {
      const updated = await api.updateUser(user.username, { send_password_reset: true })
      const deliveryError = emailDeliveryError(
        updated.email_delivery,
        'The password reset email could not be sent.',
      )
      if (deliveryError !== null) {
        onSetError(deliveryError)
      }
    } catch (err) {
      onHandleError(err)
    }
  }

  return (
    <div className="console-card">
      <h2>Instance admin</h2>
      <form className="compact-form" onSubmit={(e) => void handleCreateProject(e)}>
        <h3>Create project</h3>
        <div className="form-grid">
          <label>
            Slug
            <input
              type="text"
              aria-label="New project slug"
              value={newSlug}
              onChange={(e) => setNewSlug(e.target.value)}
              placeholder="home-lab"
              disabled={busy}
              required
            />
          </label>
          <label>
            Name
            <input
              type="text"
              aria-label="New project name"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="Home Lab"
              disabled={busy}
              required
            />
          </label>
        </div>
        <p>
          <button type="submit" disabled={busy || !newSlug.trim() || !newName.trim()}>
            Create project
          </button>
        </p>
      </form>

      <h3>Invited users (Firebase &amp; local)</h3>
      {userNotice !== null && (
        <p className="ok" data-testid="user-notice">
          {userNotice}
        </p>
      )}
      <form className="compact-form" onSubmit={(e) => void handleInviteUser(e)}>
        <div className="form-grid">
          <label>
            Username
            <input
              type="text"
              aria-label="Invite username"
              value={inviteUsername}
              onChange={(e) => setInviteUsername(e.target.value)}
              placeholder="alice"
              disabled={busy}
              required
            />
          </label>
          <label>
            Email (Firebase invite)
            <input
              type="email"
              aria-label="Invite email"
              value={inviteEmail}
              onChange={(e) => setInviteEmail(e.target.value)}
              placeholder="alice@example.com"
              disabled={busy}
            />
          </label>
          <label>
            Display name
            <input
              type="text"
              aria-label="Invite display name"
              value={inviteDisplayName}
              onChange={(e) => setInviteDisplayName(e.target.value)}
              placeholder="Alice Smith"
              disabled={busy}
            />
          </label>
          <label>
            Local password (optional if email set)
            <input
              type="password"
              aria-label="Invite local password"
              value={invitePassword}
              onChange={(e) => setInvitePassword(e.target.value)}
              disabled={busy}
            />
          </label>
          <label>
            Initial role in {activeProject}
            <select
              aria-label="Invite project role"
              value={inviteProjectRole}
              onChange={(e) =>
                setInviteProjectRole(e.target.value as '' | 'owner' | 'operator' | 'viewer')
              }
              disabled={busy}
            >
              <option value="">None (grant later)</option>
              <option value="owner">owner</option>
              <option value="operator">operator</option>
              <option value="viewer">viewer</option>
            </select>
          </label>
        </div>
        <p>
          <label className="choice">
            <input
              type="checkbox"
              checked={inviteSendEmail}
              onChange={(e) => setInviteSendEmail(e.target.checked)}
              disabled={busy || inviteEmail.trim() === ''}
            />{' '}
            Send Firebase password-setup email
          </label>
          <label className="choice">
            <input
              type="checkbox"
              checked={inviteSuperadmin}
              onChange={(e) => setInviteSuperadmin(e.target.checked)}
              disabled={busy}
            />{' '}
            Superadmin
          </label>
        </p>
        <p>
          <button
            type="submit"
            disabled={
              busy || !inviteUsername.trim() || (inviteEmail.trim() === '' && invitePassword === '')
            }
          >
            Invite user
          </button>
        </p>
      </form>

      {users !== null && users.length > 0 && (
        <table>
          <thead>
            <tr>
              <th scope="col">User</th>
              <th scope="col">Email</th>
              <th scope="col">Auth</th>
              <th scope="col">Status</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td>
                  <code>{u.username}</code>
                  {u.display_name ? ` (${u.display_name})` : ''}
                  {u.is_superadmin ? ' · superadmin' : ''}
                  {u.projects && u.projects.length > 0 ? (
                    <span className="muted">
                      {' · '}
                      {u.projects.map((p) => `${p.project_slug}:${p.role}`).join(', ')}
                    </span>
                  ) : null}
                </td>
                <td>{u.email ?? '—'}</td>
                <td>
                  {u.firebase_uid && u.has_local_password
                    ? 'firebase + password'
                    : u.firebase_uid
                      ? 'firebase linked'
                      : u.email
                        ? u.has_local_password
                          ? 'invited + password'
                          : 'firebase invited'
                        : 'local'}
                </td>
                <td>{u.disabled_at ? 'disabled' : 'active'}</td>
                <td>
                  <button
                    type="button"
                    aria-label={`Edit ${u.username}`}
                    disabled={busy}
                    onClick={() =>
                      editingUser === u.username ? setEditingUser(null) : startEditUser(u)
                    }
                  >
                    {editingUser === u.username ? 'Close' : 'Edit'}
                  </button>{' '}
                  {u.email && (
                    <>
                      <button type="button" onClick={() => void handleSendPasswordReset(u)}>
                        Send reset email
                      </button>{' '}
                    </>
                  )}
                  <button
                    type="button"
                    aria-label={`${u.disabled_at ? 'Enable' : 'Disable'} ${u.username}`}
                    disabled={busy}
                    onClick={() =>
                      u.disabled_at
                        ? void handleToggleUserDisabled(u)
                        : onSetPending({ kind: 'disable-user', username: u.username })
                    }
                  >
                    {u.disabled_at ? 'Enable' : 'Disable'}
                  </button>{' '}
                  <button
                    type="button"
                    aria-label={`Delete ${u.username}`}
                    disabled={busy || u.username === me.subject}
                    onClick={() => onSetPending({ kind: 'delete-user', username: u.username })}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {editingUser !== null && (
        <form
          className="compact-form"
          data-testid={`edit-user-${editingUser}`}
          onSubmit={(e) => void handleSaveUserEdit(e)}
        >
          <h4>
            Edit user <code>{editingUser}</code>
          </h4>
          <div className="form-grid">
            <label>
              Display name
              <input
                type="text"
                aria-label="Edit user display name"
                value={editDisplayName}
                onChange={(e) => setEditDisplayName(e.target.value)}
                placeholder="Full name"
                disabled={busy}
              />
            </label>
            <label>
              Email
              <input
                type="email"
                aria-label="Edit user email"
                value={editEmail}
                onChange={(e) => setEditEmail(e.target.value)}
                placeholder="user@example.com"
                disabled={busy}
              />
            </label>
            <label>
              Set / reset password
              <input
                type="password"
                aria-label="Edit user password"
                value={editPassword}
                onChange={(e) => setEditPassword(e.target.value)}
                placeholder="Leave blank to keep existing"
                disabled={busy}
              />
            </label>
          </div>
          <p>
            <label className="choice">
              <input
                type="checkbox"
                aria-label="Edit user superadmin"
                checked={editSuperadmin}
                onChange={(e) => setEditSuperadmin(e.target.checked)}
                disabled={busy}
              />{' '}
              Superadmin (full capability across all projects &amp; users)
            </label>
          </p>
          <p>
            <button type="submit" disabled={busy}>
              Save user
            </button>{' '}
            <button type="button" disabled={busy} onClick={() => setEditingUser(null)}>
              Cancel
            </button>
          </p>
        </form>
      )}
      {pending?.kind === 'disable-user' && (
        <p data-testid="confirm-disable-user">
          Disable {pending.username}? They are signed out at once.{' '}
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              const target = users?.find((u) => u.username === pending.username)
              if (target) {
                void handleToggleUserDisabled(target)
              } else {
                onSetPending(null)
              }
            }}
          >
            Yes, disable {pending.username}
          </button>{' '}
          <button type="button" onClick={() => onSetPending(null)}>
            Keep
          </button>
        </p>
      )}
      {pending?.kind === 'delete-user' && (
        <p data-testid="confirm-delete-user">
          Delete user <code>{pending.username}</code> permanently? All their sessions and project
          memberships are removed at once.{' '}
          <button
            type="button"
            disabled={busy}
            onClick={() => void handleDeleteUser(pending.username)}
          >
            Yes, delete {pending.username}
          </button>{' '}
          <button type="button" onClick={() => onSetPending(null)}>
            Keep
          </button>
        </p>
      )}
    </div>
  )
}
