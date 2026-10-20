// Project switcher & access/token management drawer (P1-fe-1).
//
// Plaintext `ffa_...` API tokens follow the exact same rule as `EnrollBoard.tsx`:
// shown once in component state after POST, never stored in localStorage or URLs.

import { useCallback, useEffect, useState, type FormEvent } from 'react'
import {
  ApiError,
  api,
  type AdminTokenIssued,
  type AdminTokenSummary,
  type Me,
  type ProjectMemberSummary,
  type ProjectSummary,
  type UserSummary,
} from './api'
import { formatWhen } from './format'

// The user row is saved either way; the API reports whether the setup email went out.
function emailDeliveryError(
  delivery: UserSummary['email_delivery'],
  failedMessage: string,
): string | null {
  if (delivery === 'failed') {
    return failedMessage
  }
  if (delivery === 'not_configured') {
    return 'Firebase is not configured on the server; no email was sent.'
  }
  return null
}

type PendingConfirm =
  | { kind: 'remove-member'; username: string }
  | { kind: 'revoke-token'; id: string; name: string }
  | { kind: 'disable-user'; username: string }

export function ProjectAccess({
  me,
  activeProject,
  onSelectProject,
  onSessionExpired,
}: {
  me: Me
  activeProject: string
  onSelectProject: (slug: string) => void
  onSessionExpired: () => void
}) {
  const [open, setOpen] = useState(false)
  const [projects, setProjects] = useState<ProjectSummary[]>(
    () =>
      me.projects?.map((p) => ({
        id: p.id,
        slug: p.slug,
        name: p.name,
        description: p.description,
        role: p.role,
        created_at: '',
      })) ?? [],
  )
  const [members, setMembers] = useState<ProjectMemberSummary[] | null>(null)
  const [tokens, setTokens] = useState<AdminTokenSummary[] | null>(null)
  const [users, setUsers] = useState<UserSummary[] | null>(null)
  const [issuedToken, setIssuedToken] = useState<AdminTokenIssued | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // Destructive actions ask first; one pending question for the whole drawer.
  const [pending, setPending] = useState<PendingConfirm | null>(null)

  // New project form (superadmin only)
  const [newSlug, setNewSlug] = useState('')
  const [newName, setNewName] = useState('')

  // Invite / create user form (superadmin only)
  const [inviteUsername, setInviteUsername] = useState('')
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteDisplayName, setInviteDisplayName] = useState('')
  const [invitePassword, setInvitePassword] = useState('')
  const [inviteSendEmail, setInviteSendEmail] = useState(true)
  const [inviteSuperadmin, setInviteSuperadmin] = useState(false)

  // Add member form
  const [memberUsername, setMemberUsername] = useState('')
  const [memberRole, setMemberRole] = useState<'owner' | 'operator' | 'viewer'>('operator')

  // Issue API token form
  const [tokenName, setTokenName] = useState('')
  const [tokenScope, setTokenScope] = useState('deploy')
  const [tokenPinned, setTokenPinned] = useState(true)

  // A stale question about another project's member must not survive a switch or a close.
  useEffect(() => {
    setPending(null)
  }, [activeProject, open])

  const handle = useCallback(
    (err: unknown) => {
      if (err instanceof ApiError && err.isUnauthorized) {
        onSessionExpired()
        return
      }
      setError(err instanceof Error ? err.message : 'request failed')
    },
    [onSessionExpired],
  )

  const refreshDrawer = useCallback(async () => {
    try {
      const [projRes, memRes, tokRes, userRes] = await Promise.all([
        api.listProjects(),
        api.listProjectMembers(activeProject),
        api.listAdminTokens(),
        me.is_superadmin ? api.listUsers() : Promise.resolve(null),
      ])
      setProjects(projRes.projects)
      setMembers(memRes.members)
      setTokens(tokRes.tokens)
      if (userRes !== null) {
        setUsers(userRes.users)
      }
    } catch (err) {
      handle(err)
    }
  }, [activeProject, handle, me.is_superadmin])

  useEffect(() => {
    if (open) {
      void refreshDrawer()
    }
  }, [open, refreshDrawer])

  async function handleCreateProject(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const created = await api.createProject({ slug: newSlug.trim(), name: newName.trim() })
      setNewSlug('')
      setNewName('')
      await refreshDrawer()
      onSelectProject(created.slug)
    } catch (err) {
      handle(err)
    } finally {
      setBusy(false)
    }
  }

  async function handleInviteUser(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const created = await api.createUser({
        username: inviteUsername.trim(),
        email: inviteEmail.trim() || null,
        display_name: inviteDisplayName.trim() || null,
        password: invitePassword !== '' ? invitePassword : null,
        is_superadmin: inviteSuperadmin,
        send_invite_email: inviteEmail.trim() !== '' && inviteSendEmail,
      })
      setInviteUsername('')
      setInviteEmail('')
      setInviteDisplayName('')
      setInvitePassword('')
      setInviteSuperadmin(false)
      await refreshDrawer()
      const deliveryError = emailDeliveryError(
        created.email_delivery,
        'User saved, but the setup email could not be sent. Try "Send reset email" again.',
      )
      if (deliveryError !== null) {
        setError(deliveryError)
      }
    } catch (err) {
      handle(err)
    } finally {
      setBusy(false)
    }
  }

  async function handleToggleUserDisabled(user: UserSummary) {
    setError(null)
    try {
      await api.updateUser(user.username, { disabled: user.disabled_at === null })
      await refreshDrawer()
    } catch (err) {
      handle(err)
    } finally {
      setPending(null)
    }
  }

  async function handleSendPasswordReset(user: UserSummary) {
    setError(null)
    try {
      const updated = await api.updateUser(user.username, { send_password_reset: true })
      const deliveryError = emailDeliveryError(
        updated.email_delivery,
        'The password reset email could not be sent.',
      )
      if (deliveryError !== null) {
        setError(deliveryError)
      }
    } catch (err) {
      handle(err)
    }
  }

  async function handleAddMember(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.upsertProjectMember(activeProject, memberUsername.trim(), { role: memberRole })
      setMemberUsername('')
      await refreshDrawer()
    } catch (err) {
      handle(err)
    } finally {
      setBusy(false)
    }
  }

  async function handleRemoveMember(username: string) {
    setError(null)
    try {
      await api.removeProjectMember(activeProject, username)
      await refreshDrawer()
    } catch (err) {
      handle(err)
    } finally {
      setPending(null)
    }
  }

  async function handleIssueToken(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const scopes =
        tokenScope === 'admin'
          ? ['admin']
          : tokenScope === 'read'
            ? ['read']
            : ['read', 'write:artifacts', 'deploy']
      const issued = await api.issueAdminToken({
        name: tokenName.trim(),
        project: tokenPinned ? activeProject : null,
        scopes,
      })
      setTokenName('')
      setIssuedToken(issued)
      await refreshDrawer()
    } catch (err) {
      handle(err)
    } finally {
      setBusy(false)
    }
  }

  async function handleRevokeToken(id: string) {
    setError(null)
    try {
      await api.revokeAdminToken(id)
      setIssuedToken((curr) => (curr?.id === id ? null : curr))
      await refreshDrawer()
    } catch (err) {
      handle(err)
    } finally {
      setPending(null)
    }
  }

  const options =
    projects.length > 0
      ? projects
      : [{ id: 'default', slug: activeProject, name: activeProject, description: null, role: 'owner', created_at: '' }]

  return (
    <section aria-label="Project and access" data-testid="project-access">
      <p>
        <label>
          Project{' '}
          <select
            aria-label="Project"
            value={activeProject}
            onChange={(e) => onSelectProject(e.target.value)}
          >
            {options.map((p) => (
              <option key={p.slug} value={p.slug}>
                {p.name} ({p.slug})
              </option>
            ))}
          </select>
        </label>{' '}
        <button type="button" onClick={() => setOpen((v) => !v)}>
          {open ? 'Hide access & tokens' : 'Project access & API tokens'}
        </button>
      </p>

      {open && (
        <div className="issued" data-testid="project-drawer">
          {error !== null && (
            <p className="bad" role="alert">
              {error}
            </p>
          )}

          {me.is_superadmin && (
            <>
              <form onSubmit={(e) => void handleCreateProject(e)}>
                <h3>Create project</h3>
                <p>
                  <label>
                    Slug{' '}
                    <input
                      type="text"
                      aria-label="New project slug"
                      value={newSlug}
                      onChange={(e) => setNewSlug(e.target.value)}
                      placeholder="home-lab"
                      disabled={busy}
                      required
                    />
                  </label>{' '}
                  <label>
                    Name{' '}
                    <input
                      type="text"
                      aria-label="New project name"
                      value={newName}
                      onChange={(e) => setNewName(e.target.value)}
                      placeholder="Home Lab"
                      disabled={busy}
                      required
                    />
                  </label>{' '}
                  <button type="submit" disabled={busy || !newSlug.trim() || !newName.trim()}>
                    Create project
                  </button>
                </p>
              </form>

              <h3>Invited users (Firebase &amp; local)</h3>
              <form onSubmit={(e) => void handleInviteUser(e)}>
                <p>
                  <label>
                    Username{' '}
                    <input
                      type="text"
                      aria-label="Invite username"
                      value={inviteUsername}
                      onChange={(e) => setInviteUsername(e.target.value)}
                      placeholder="alice"
                      disabled={busy}
                      required
                    />
                  </label>{' '}
                  <label>
                    Email (Firebase invite){' '}
                    <input
                      type="email"
                      aria-label="Invite email"
                      value={inviteEmail}
                      onChange={(e) => setInviteEmail(e.target.value)}
                      placeholder="alice@example.com"
                      disabled={busy}
                    />
                  </label>{' '}
                  <label>
                    Display name{' '}
                    <input
                      type="text"
                      aria-label="Invite display name"
                      value={inviteDisplayName}
                      onChange={(e) => setInviteDisplayName(e.target.value)}
                      placeholder="Alice Smith"
                      disabled={busy}
                    />
                  </label>{' '}
                  <label>
                    Local password (optional if email set){' '}
                    <input
                      type="password"
                      aria-label="Invite local password"
                      value={invitePassword}
                      onChange={(e) => setInvitePassword(e.target.value)}
                      disabled={busy}
                    />
                  </label>{' '}
                  <label>
                    <input
                      type="checkbox"
                      checked={inviteSendEmail}
                      onChange={(e) => setInviteSendEmail(e.target.checked)}
                      disabled={busy || inviteEmail.trim() === ''}
                    />{' '}
                    Send Firebase password-setup email
                  </label>{' '}
                  <label>
                    <input
                      type="checkbox"
                      checked={inviteSuperadmin}
                      onChange={(e) => setInviteSuperadmin(e.target.checked)}
                      disabled={busy}
                    />{' '}
                    Superadmin
                  </label>{' '}
                  <button
                    type="submit"
                    disabled={
                      busy ||
                      !inviteUsername.trim() ||
                      (inviteEmail.trim() === '' && invitePassword === '')
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
                        </td>
                        <td>{u.email ?? '—'}</td>
                        <td>
                          {u.firebase_uid
                            ? 'firebase linked'
                            : u.email
                              ? 'firebase invited'
                              : 'local'}
                        </td>
                        <td>{u.disabled_at ? 'disabled' : 'active'}</td>
                        <td>
                          {u.email && (
                            <button
                              type="button"
                              onClick={() => void handleSendPasswordReset(u)}
                            >
                              Send reset email
                            </button>
                          )}{' '}
                          <button
                            type="button"
                            aria-label={`${u.disabled_at ? 'Enable' : 'Disable'} ${u.username}`}
                            disabled={busy}
                            onClick={() =>
                              u.disabled_at
                                ? void handleToggleUserDisabled(u)
                                : setPending({ kind: 'disable-user', username: u.username })
                            }
                          >
                            {u.disabled_at ? 'Enable' : 'Disable'}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
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
                        setPending(null)
                      }
                    }}
                  >
                    Yes, disable {pending.username}
                  </button>{' '}
                  <button type="button" onClick={() => setPending(null)}>
                    Keep
                  </button>
                </p>
              )}
            </>
          )}

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
                        disabled={busy}
                        onClick={() => setPending({ kind: 'remove-member', username: m.username })}
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
              <button type="button" onClick={() => setPending(null)}>
                Keep
              </button>
            </p>
          )}
          <form onSubmit={(e) => void handleAddMember(e)}>
            <p>
              <label>
                Username{' '}
                <input
                  type="text"
                  aria-label="Member username"
                  value={memberUsername}
                  onChange={(e) => setMemberUsername(e.target.value)}
                  disabled={busy}
                  required
                />
              </label>{' '}
              <label>
                Role{' '}
                <select
                  aria-label="Member role"
                  value={memberRole}
                  onChange={(e) =>
                    setMemberRole(e.target.value as 'owner' | 'operator' | 'viewer')
                  }
                  disabled={busy}
                >
                  <option value="owner">owner</option>
                  <option value="operator">operator</option>
                  <option value="viewer">viewer</option>
                </select>
              </label>{' '}
              <button type="submit" disabled={busy || !memberUsername.trim()}>
                Save member
              </button>
            </p>
          </form>

          <h3>API tokens (CLI / CI)</h3>
          <form onSubmit={(e) => void handleIssueToken(e)}>
            <p>
              <label>
                Token label{' '}
                <input
                  type="text"
                  aria-label="Token label"
                  value={tokenName}
                  onChange={(e) => setTokenName(e.target.value)}
                  placeholder="github-actions"
                  disabled={busy}
                  required
                />
              </label>{' '}
              <label>
                Preset{' '}
                <select
                  aria-label="Token preset"
                  value={tokenScope}
                  onChange={(e) => setTokenScope(e.target.value)}
                  disabled={busy}
                >
                  <option value="deploy">CI deploy (read + write:artifacts + deploy)</option>
                  <option value="read">Read-only (read)</option>
                  <option value="admin">Full access (admin)</option>
                </select>
              </label>{' '}
              <label>
                <input
                  type="checkbox"
                  checked={tokenPinned}
                  onChange={(e) => setTokenPinned(e.target.checked)}
                  disabled={busy}
                />{' '}
                Pin to <code>{activeProject}</code>
              </label>{' '}
              <button type="submit" disabled={busy || !tokenName.trim()}>
                Issue API token
              </button>
            </p>
          </form>

          {issuedToken !== null && (
            <div className="issued" role="alert">
              <p className="warn">Copy this API token now — it is shown once:</p>
              <code data-testid="issued-admin-token">{issuedToken.token}</code>{' '}
              <button type="button" onClick={() => setIssuedToken(null)}>
                Done
              </button>
            </div>
          )}

          {tokens !== null && tokens.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th scope="col">Label</th>
                  <th scope="col">Project</th>
                  <th scope="col">Scopes</th>
                  <th scope="col">Created</th>
                  <th scope="col" />
                </tr>
              </thead>
              <tbody>
                {tokens.map((t) => (
                  <tr key={t.id}>
                    <td>{t.name}</td>
                    <td>{t.project_slug ?? 'all'}</td>
                    <td>
                      <code>{t.scopes.join(', ')}</code>
                    </td>
                    <td>{formatWhen(t.created_at)}</td>
                    <td>
                      {t.revoked_at === null ? (
                        <button
                          type="button"
                          aria-label={`Revoke ${t.name}`}
                          disabled={busy}
                          onClick={() =>
                            setPending({ kind: 'revoke-token', id: t.id, name: t.name })
                          }
                        >
                          Revoke
                        </button>
                      ) : (
                        <span className="muted">revoked</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {pending?.kind === 'revoke-token' && (
            <p data-testid="confirm-revoke-token">
              Revoke {pending.name}? Anything using it stops working at once.{' '}
              <button
                type="button"
                disabled={busy}
                onClick={() => void handleRevokeToken(pending.id)}
              >
                Revoke token
              </button>{' '}
              <button type="button" onClick={() => setPending(null)}>
                Keep
              </button>
            </p>
          )}
        </div>
      )}
    </section>
  )
}
