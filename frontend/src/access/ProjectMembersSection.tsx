import { useState, type FormEvent } from 'react'
import { api, type ProjectMemberSummary, type UserSummary } from '../api'
import type { PendingConfirm } from './types'

export function ProjectMembersSection({
  activeProject,
  members,
  users,
  busy,
  isViewer,
  pending,
  onSetBusy,
  onSetError,
  onSetPending,
  onRefresh,
  onHandleError,
}: {
  activeProject: string
  members: ProjectMemberSummary[] | null
  users: UserSummary[] | null
  busy: boolean
  isViewer: boolean
  pending: PendingConfirm | null
  onSetBusy: (busy: boolean) => void
  onSetError: (error: string | null) => void
  onSetPending: (pending: PendingConfirm | null) => void
  onRefresh: () => Promise<void>
  onHandleError: (err: unknown) => void
}) {
  const [memberUsername, setMemberUsername] = useState('')
  const [memberRole, setMemberRole] = useState<'owner' | 'operator' | 'viewer'>('operator')

  async function handleAddMember(event: FormEvent) {
    event.preventDefault()
    onSetBusy(true)
    onSetError(null)
    try {
      await api.upsertProjectMember(activeProject, memberUsername.trim(), { role: memberRole })
      setMemberUsername('')
      await onRefresh()
    } catch (err) {
      onHandleError(err)
    } finally {
      onSetBusy(false)
    }
  }

  async function handleRemoveMember(username: string) {
    onSetError(null)
    try {
      await api.removeProjectMember(activeProject, username)
      await onRefresh()
    } catch (err) {
      onHandleError(err)
    } finally {
      onSetPending(null)
    }
  }

  return (
    <>
      <h3>Members of {activeProject}</h3>
      {members === null ? (
        <p aria-busy="true">Loading members…</p>
      ) : members.length === 0 ? (
        <p className="muted">No explicit project members (superadmins have owner access).</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">User</th>
              <th scope="col">Role</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {members.map((m) => (
              <tr key={m.username}>
                <td>
                  <code>{m.username}</code>
                  {m.display_name ? ` (${m.display_name})` : ''}
                </td>
                <td>{m.role}</td>
                <td>
                  <button
                    type="button"
                    aria-label={`Remove ${m.username}`}
                    disabled={busy || isViewer}
                    onClick={() => onSetPending({ kind: 'remove-member', username: m.username })}
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {pending?.kind === 'remove-member' && (
        <p data-testid="confirm-remove-member">
          Remove {pending.username} from {activeProject}? They lose access to its boards.{' '}
          <button
            type="button"
            disabled={busy}
            onClick={() => void handleRemoveMember(pending.username)}
          >
            Yes, remove {pending.username}
          </button>{' '}
          <button type="button" onClick={() => onSetPending(null)}>
            Keep
          </button>
        </p>
      )}
      <form className="compact-form" onSubmit={(e) => void handleAddMember(e)}>
        <div className="form-grid">
          <label>
            Username
            <input
              type="text"
              aria-label="Member username"
              list="fleetforge-known-users"
              value={memberUsername}
              onChange={(e) => setMemberUsername(e.target.value)}
              placeholder="username"
              disabled={busy || isViewer}
              required
            />
            {users !== null && users.length > 0 && (
              <datalist id="fleetforge-known-users">
                {users.map((u) => (
                  <option key={u.username} value={u.username}>
                    {u.display_name
                      ? `${u.display_name}${u.email ? ` <${u.email}>` : ''}`
                      : (u.email ?? u.username)}
                  </option>
                ))}
              </datalist>
            )}
          </label>
          <label>
            Role
            <select
              aria-label="Member role"
              value={memberRole}
              onChange={(e) => setMemberRole(e.target.value as 'owner' | 'operator' | 'viewer')}
              disabled={busy || isViewer}
            >
              <option value="owner">owner</option>
              <option value="operator">operator</option>
              <option value="viewer">viewer</option>
            </select>
          </label>
        </div>
        <p>
          <button type="submit" disabled={busy || isViewer || !memberUsername.trim()}>
            Save member
          </button>
        </p>
      </form>
    </>
  )
}
