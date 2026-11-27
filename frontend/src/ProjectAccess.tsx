// Project switcher & access/token management drawer (P1-fe-1).
//
// Plaintext `ffa_...` API tokens follow the exact same rule as `EnrollBoard.tsx`:
// shown once in component state after POST, never stored in localStorage or URLs.

import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { ApiTokensSection } from './access/ApiTokensSection'
import { MyAccountSection } from './access/MyAccountSection'
import { ProjectMembersSection } from './access/ProjectMembersSection'
import { ProjectSecretsSection } from './access/ProjectSecretsSection'
import { UsersAdminSection } from './access/UsersAdminSection'
import { type PendingConfirm } from './access/types'
import {
  ApiError,
  api,
  type AdminTokenSummary,
  type DeviceGroupSummary,
  type DeviceSummary,
  type Me,
  type ProjectMemberSummary,
  type ProjectSecretSummary,
  type ProjectSummary,
  type UserSummary,
} from './api'

export function ProjectAccess({
  me,
  activeProject,
  onSelectProject,
  onSessionExpired,
  open: controlledOpen,
  onToggleOpen,
  tabs,
  devices,
}: {
  me: Me
  activeProject: string
  onSelectProject: (slug: string) => void
  onSessionExpired: () => void
  open?: boolean
  onToggleOpen?: () => void
  tabs?: ReactNode
  devices?: DeviceSummary[] | null
}) {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false)
  const open = controlledOpen ?? uncontrolledOpen
  const toggleOpen = onToggleOpen ?? (() => setUncontrolledOpen((v) => !v))
  const [projects, setProjects] = useState<ProjectSummary[]>(
    () =>
      me.projects?.map((p) => ({
        id: p.id,
        slug: p.slug,
        name: p.name,
        description: p.description ?? null,
        role: p.role,
        created_at: '',
        updated_at: '',
      })) ?? [],
  )
  const [members, setMembers] = useState<ProjectMemberSummary[] | null>(null)
  const [tokens, setTokens] = useState<AdminTokenSummary[] | null>(null)
  const [users, setUsers] = useState<UserSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [pending, setPending] = useState<PendingConfirm | null>(null)

  const [secretsRev, setSecretsRev] = useState<number>(0)
  const [secrets, setSecrets] = useState<ProjectSecretSummary[]>([])
  const [otelEndpoint, setOtelEndpoint] = useState('')
  const [groups, setGroups] = useState<DeviceGroupSummary[]>([])

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
      const [projRes, memRes, tokRes, secRes, userRes] = await Promise.all([
        api.listProjects(),
        api.listProjectMembers(activeProject),
        api.listAdminTokens(),
        api.listProjectSecrets(activeProject),
        me.is_superadmin ? api.listUsers() : Promise.resolve(null),
      ])
      const projs = projRes.projects ?? []
      setProjects(projs)
      setMembers(memRes.members ?? [])
      setTokens(tokRes.tokens ?? [])
      setSecretsRev(secRes?.secrets_rev ?? 0)
      setSecrets(secRes?.secrets ?? [])
      const currProj = projs.find((p) => p.slug === activeProject)
      if (currProj) {
        setOtelEndpoint(currProj.otel_endpoint ?? '')
      }
      if (userRes !== null) {
        setUsers(userRes.users ?? [])
      }
      if (typeof api.listGroups === 'function') {
        const grpRes = await api.listGroups(activeProject !== 'default' ? activeProject : undefined)
        setGroups(grpRes?.groups ?? [])
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

  const options: ProjectSummary[] =
    projects.length > 0
      ? projects
      : [
          {
            id: 'default',
            slug: activeProject,
            name: activeProject,
            description: null,
            role: 'owner',
            created_at: '',
            updated_at: '',
          },
        ]
  const activeOption = options.find((p) => p.slug === activeProject) ?? options[0]
  const activeRole = activeOption?.role ?? 'owner'
  const canManage = Boolean(me.is_superadmin) || activeRole === 'owner'
  const isViewer = !me.is_superadmin && activeRole === 'viewer'
  const projectBoards = devices ?? []

  return (
    <section aria-label="Project and access" data-testid="project-access">
      <div className="project-bar">
        <div className="project-bar-left">
          <label className="project-picker-label">
            <span className="project-tag">PROJECT</span>{' '}
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
          </label>
          {activeOption?.role && (
            <span
              className="badge muted"
              title="Your role in this project"
              data-testid="active-project-role"
            >
              role: {activeOption.role}
            </span>
          )}
        </div>
        <button type="button" onClick={toggleOpen}>
          {open ? 'Hide access & tokens' : 'Project access & API tokens'}
        </button>
      </div>

      {tabs}

      {open && (
        <div className="project-drawer" data-testid="project-drawer">
          {error !== null && (
            <p className="bad" role="alert">
              {error}
            </p>
          )}
          {isViewer && (
            <p className="warn" role="status" data-testid="rbac-viewer-notice">
              Read-only viewer role in <code>{activeProject}</code> — owner or operator access is
              required to modify settings or issue credentials.
            </p>
          )}

          <div className="access-grid">
            <div className="console-card">
              <h2>Project access · {activeProject}</h2>

              <ProjectMembersSection
                activeProject={activeProject}
                members={members}
                users={users}
                busy={busy}
                isViewer={isViewer}
                pending={pending}
                onSetBusy={setBusy}
                onSetError={setError}
                onSetPending={setPending}
                onRefresh={refreshDrawer}
                onHandleError={handle}
              />

              <ApiTokensSection
                activeProject={activeProject}
                tokens={tokens}
                busy={busy}
                isViewer={isViewer}
                pending={pending}
                onSetBusy={setBusy}
                onSetError={setError}
                onSetPending={setPending}
                onRefresh={refreshDrawer}
                onHandleError={handle}
              />

              <ProjectSecretsSection
                activeProject={activeProject}
                open={open}
                hasOtelHeaders={Boolean(activeOption?.has_otel_headers)}
                canManage={canManage}
                isViewer={isViewer}
                busy={busy}
                pending={pending}
                secretsRev={secretsRev}
                secrets={secrets}
                projectBoards={projectBoards}
                otelEndpoint={otelEndpoint}
                groups={groups}
                onSetBusy={setBusy}
                onSetError={setError}
                onSetPending={setPending}
                onSetSecretsState={(rev, items) => {
                  setSecretsRev(rev)
                  setSecrets(items)
                }}
                onSetOtelEndpoint={setOtelEndpoint}
                onRefresh={refreshDrawer}
                onHandleError={handle}
              />

              <MyAccountSection
                me={me}
                activeProject={activeProject}
                open={open}
                activeRole={activeRole}
                busy={busy}
                onSetBusy={setBusy}
                onSetError={setError}
                onRefresh={refreshDrawer}
                onHandleError={handle}
              />
            </div>

            <UsersAdminSection
              me={me}
              activeProject={activeProject}
              open={open}
              busy={busy}
              pending={pending}
              users={users}
              onSetBusy={setBusy}
              onSetError={setError}
              onSetPending={setPending}
              onSelectProject={onSelectProject}
              onRefresh={refreshDrawer}
              onHandleError={handle}
            />
          </div>
        </div>
      )}
    </section>
  )
}
