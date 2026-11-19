// The dashboard's only HTTP surface.
//
// Every path here is **relative**. The API shares this page's origin (nginx in the
// `frontend` container proxies `/v1/*` to `api:8000`; `vite.config.ts` does the same
// in dev), so there is no base URL to configure and no CORS preflight to satisfy.

import type {
  AdminTokenIssued,
  AdminTokenSummary,
  AgentManifest,
  ArtifactList,
  ArtifactSimulateResponse,
  ArtifactUploaded,
  BuildJobCreate,
  BuildJobDetail,
  BuildJobList,
  BuildJobSummary,
  CommandAccepted,
  DeployAccepted,
  DeployKpisSummary,
  DeployPolicyCreate,
  DeployPolicyEvaluation,
  DeployPolicyList,
  DeployPolicySummary,
  DeployPolicyUpdate,
  DeployPrecheck,
  DeviceGroupSummary,
  DeviceList,
  DeviceLogList,
  DeviceSummary,
  DeviceUpdate,
  DockSyncRequest,
  DockSyncResponse,
  EnrollmentTokenIssued,
  EnrollmentTokenSummary,
  Health,
  LoginResult,
  Me,
  PartitionProfileAdopt,
  PartitionProfileList,
  PartitionProfileSummary,
  ProjectDeployAccepted,
  ProjectDeployCancelAccepted,
  ProjectMemberSummary,
  ProjectSecretList,
  ProjectSummary,
  ProjectUpdate,
  SelfProfileUpdate,
  TelemetrySeries,
  UserSummary,
} from './apiTypes'

export * from './apiTypes'

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }

  /** A dead session. Callers drop back to the login screen rather than retrying. */
  get isUnauthorized(): boolean {
    return this.status === 401
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      credentials: 'same-origin',
      headers:
        init.body === undefined ? init.headers : { 'content-type': 'application/json', ...init.headers },
    })
  } catch {
    throw new ApiError(0, 'the API is unreachable')
  }

  if (response.status === 204) {
    return undefined as T
  }

  const body = await response.text()

  if (!response.ok) {
    throw new ApiError(response.status, detailOf(body) ?? `HTTP ${response.status}`)
  }

  return JSON.parse(body) as T
}

/**
 * `request<T>()`'s byte-returning sibling, for the megabyte-scale agent images.
 */
async function requestBytes(path: string): Promise<Uint8Array> {
  let response: Response
  try {
    response = await fetch(path, { credentials: 'same-origin' })
  } catch {
    throw new ApiError(0, 'the API is unreachable')
  }
  if (!response.ok) {
    throw new ApiError(response.status, detailOf(await response.text()) ?? `HTTP ${response.status}`)
  }
  return new Uint8Array(await response.arrayBuffer())
}

/**
 * FastAPI puts the human-readable reason in `detail`: a string from an `HTTPException`, or
 * (422, request validation) an array of `{msg, ...}` items.
 */
function detailOf(body: string): string | null {
  try {
    const parsed: unknown = JSON.parse(body)
    if (parsed && typeof parsed === 'object' && 'detail' in parsed) {
      const detail = (parsed as { detail: unknown }).detail
      if (typeof detail === 'string') return detail
      if (Array.isArray(detail)) {
        const messages = (detail as unknown[]).flatMap((item) => {
          if (item && typeof item === 'object' && 'msg' in item) {
            const msg = (item as { msg: unknown }).msg
            if (typeof msg === 'string') return [msg.replace(/^Value error, /, '')]
          }
          return []
        })
        if (messages.length > 0) return messages.join('; ')
      }
    }
  } catch {
    /* not JSON — fall through */
  }
  return body.trim() === '' ? null : body.trim()
}

export const api = {
  health: () => request<Health>('/v1/healthz'),

  me: () => request<Me>('/v1/auth/me'),

  updateMe: (body: SelfProfileUpdate) =>
    request<Me>('/v1/auth/me', {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  login: (password: string, username = 'admin') =>
    request<LoginResult>('/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify(username === 'admin' ? { password } : { username, password }),
    }),

  resetPassword: (oobCode: string, newPassword: string) =>
    request<LoginResult>('/v1/auth/reset-password', {
      method: 'POST',
      body: JSON.stringify({ oob_code: oobCode, new_password: newPassword }),
    }),

  logout: () => request<void>('/v1/auth/logout', { method: 'POST' }),

  listDevices: (project?: string) =>
    request<DeviceList>(
      project && project !== 'default'
        ? `/v1/devices?project=${encodeURIComponent(project)}`
        : '/v1/devices',
    ),

  updateDevice: (deviceId: string, patch: DeviceUpdate) =>
    request<DeviceSummary>(`/v1/devices/${encodeURIComponent(deviceId)}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),

  decommissionDevice: (deviceId: string) =>
    request<DeviceSummary>(`/v1/devices/${encodeURIComponent(deviceId)}/decommission`, {
      method: 'POST',
    }),

  deleteDevice: (deviceId: string) =>
    request<void>(`/v1/devices/${encodeURIComponent(deviceId)}`, {
      method: 'DELETE',
    }),

  deviceLogs: (
    deviceId: string,
    limitOrOpts: number | { limit?: number; level?: string; prefix?: string } = 200,
  ) => {
    const opts = typeof limitOrOpts === 'number' ? { limit: limitOrOpts } : limitOrOpts
    const limit = opts.limit ?? 200
    let path = `/v1/devices/${encodeURIComponent(deviceId)}/logs?limit=${limit}`
    if (opts.level) {
      path += `&level=${encodeURIComponent(opts.level)}`
    }
    if (opts.prefix) {
      path += `&prefix=${encodeURIComponent(opts.prefix)}`
    }
    return request<DeviceLogList>(path)
  },

  deviceTelemetry: (deviceId: string, name = 'free_heap', limit = 500) =>
    request<TelemetrySeries>(
      `/v1/devices/${encodeURIComponent(deviceId)}/telemetry?name=${encodeURIComponent(name)}&limit=${limit}`,
    ),

  dockSync: (deviceId: string, bundle: DockSyncRequest) =>
    request<DockSyncResponse>(`/v1/devices/${encodeURIComponent(deviceId)}/dock-sync`, {
      method: 'POST',
      body: JSON.stringify(bundle),
    }),

  listEnrollmentTokens: (project?: string) =>
    request<{ tokens: EnrollmentTokenSummary[] }>(
      project && project !== 'default'
        ? `/v1/enrollment-tokens?project=${encodeURIComponent(project)}`
        : '/v1/enrollment-tokens',
    ),

  issueEnrollmentToken: (groupId: string | null = null, project?: string) =>
    request<EnrollmentTokenIssued>(
      project && project !== 'default'
        ? `/v1/enrollment-tokens?project=${encodeURIComponent(project)}`
        : '/v1/enrollment-tokens',
      {
        method: 'POST',
        body: JSON.stringify({ group_id: groupId }),
      },
    ),

  createEnrollmentToken: (
    groupIdOrBody: string | null | { group_id?: string | null; project?: string } = null,
    project?: string,
  ) => {
    const groupId =
      groupIdOrBody !== null && typeof groupIdOrBody === 'object'
        ? (groupIdOrBody.group_id ?? null)
        : groupIdOrBody
    const proj =
      groupIdOrBody !== null && typeof groupIdOrBody === 'object'
        ? (groupIdOrBody.project ?? project)
        : project
    return request<EnrollmentTokenIssued>(
      proj && proj !== 'default'
        ? `/v1/enrollment-tokens?project=${encodeURIComponent(proj)}`
        : '/v1/enrollment-tokens',
      {
        method: 'POST',
        body: JSON.stringify({ group_id: groupId }),
      },
    )
  },

  revokeEnrollmentToken: (id: string) =>
    request<void>(`/v1/enrollment-tokens/${encodeURIComponent(id)}/revoke`, { method: 'POST' }),

  listArtifacts: (project?: string) =>
    request<ArtifactList>(
      project && project !== 'default'
        ? `/v1/artifact?project=${encodeURIComponent(project)}`
        : '/v1/artifact',
    ),

  uploadArtifact: (
    file: Blob,
    meta: {
      target: string
      version: string
      partitionLayout: string
      artifactType?: string
      project?: string
      simulate?: boolean
    },
  ) => {
    const params: Record<string, string> = {
      target: meta.target,
      version: meta.version,
      partition_layout: meta.partitionLayout,
    }
    if (meta.artifactType && meta.artifactType !== 'app') {
      params.artifact_type = meta.artifactType
    }
    if (meta.project && meta.project !== 'default') {
      params.project = meta.project
    }
    if (meta.simulate) {
      params.simulate = 'true'
    }
    const query = new URLSearchParams(params)
    return request<ArtifactUploaded>(`/v1/artifact?${query}`, {
      method: 'POST',
      body: file,
      headers: { 'content-type': 'application/octet-stream' },
    })
  },

  simulateArtifact: (sha256: string, project?: string) =>
    request<ArtifactSimulateResponse>(
      project && project !== 'default'
        ? `/v1/artifact/${encodeURIComponent(sha256)}/simulate?project=${encodeURIComponent(project)}`
        : `/v1/artifact/${encodeURIComponent(sha256)}/simulate`,
      { method: 'POST' },
    ),

  deployDevice: (
    deviceId: string,
    version: string,
    apply: 'auto' | 'on_command' = 'auto',
    override: readonly string[] = [],
  ) =>
    request<DeployAccepted>(`/v1/devices/${encodeURIComponent(deviceId)}/deploy`, {
      method: 'POST',
      body: JSON.stringify(
        override.length > 0 ? { version, apply, override: [...override] } : { version, apply },
      ),
    }),

  precheckDeploy: (deviceId: string, version: string, apply: 'auto' | 'on_command' = 'auto') =>
    request<DeployPrecheck>(`/v1/devices/${encodeURIComponent(deviceId)}/deploy/precheck`, {
      method: 'POST',
      body: JSON.stringify({ version, apply }),
    }),

  deployKpis: (project?: string) =>
    request<DeployKpisSummary>(
      project && project !== 'default'
        ? `/v1/devices/kpis?project=${encodeURIComponent(project)}`
        : '/v1/devices/kpis',
    ),

  applyDeploy: (deviceId: string, cmdId: string) =>
    request<CommandAccepted>(
      `/v1/devices/${encodeURIComponent(deviceId)}/deploys/${encodeURIComponent(cmdId)}/apply`,
      { method: 'POST' },
    ),

  cancelDeploy: (deviceId: string, cmdId: string) =>
    request<CommandAccepted>(
      `/v1/devices/${encodeURIComponent(deviceId)}/deploys/${encodeURIComponent(cmdId)}/cancel`,
      { method: 'POST' },
    ),

  rebootDevice: (deviceId: string) =>
    request<CommandAccepted>(`/v1/devices/${encodeURIComponent(deviceId)}/reboot`, {
      method: 'POST',
    }),

  rollbackDevice: (deviceId: string) =>
    request<CommandAccepted>(`/v1/devices/${encodeURIComponent(deviceId)}/rollback`, {
      method: 'POST',
    }),

  listPartitionProfiles: () => request<PartitionProfileList>('/v1/partition-profiles'),

  adoptPartitionProfile: (sha256: string, body: PartitionProfileAdopt) =>
    request<PartitionProfileSummary>(`/v1/partition-profiles/${encodeURIComponent(sha256)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  agentManifest: () => request<AgentManifest>('/v1/agent/manifest'),

  agentPart: (target: string, part: string, layout?: string | null) => {
    let path = `/v1/agent/${encodeURIComponent(target)}/${encodeURIComponent(part)}`
    if (layout) {
      path += `?layout=${encodeURIComponent(layout)}`
    }
    return requestBytes(path)
  },

  listProjects: () => request<{ projects: ProjectSummary[] }>('/v1/projects'),

  listGroups: (project?: string) =>
    request<{ groups: DeviceGroupSummary[] }>(
      project && project !== 'default'
        ? `/v1/groups?project=${encodeURIComponent(project)}`
        : '/v1/groups',
    ),

  createGroup: (body: { name: string; description?: string | null }, project?: string) =>
    request<DeviceGroupSummary>(
      project && project !== 'default'
        ? `/v1/groups?project=${encodeURIComponent(project)}`
        : '/v1/groups',
      {
        method: 'POST',
        body: JSON.stringify(body),
      },
    ),

  updateGroup: (groupId: string, patch: { name?: string; description?: string | null }) =>
    request<DeviceGroupSummary>(`/v1/groups/${encodeURIComponent(groupId)}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),

  deleteGroup: (groupId: string) =>
    request<void>(`/v1/groups/${encodeURIComponent(groupId)}`, { method: 'DELETE' }),

  createProject: (body: { slug: string; name: string; description?: string | null }) =>
    request<ProjectSummary>('/v1/projects', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  updateProject: (slug: string, patch: ProjectUpdate) =>
    request<ProjectSummary>(`/v1/projects/${encodeURIComponent(slug)}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),

  deployProject: (
    slug: string,
    body: {
      version: string
      target_filter?: 'all' | 'dev'
      apply?: 'auto' | 'on_command'
      override?: readonly string[]
      group_id?: string | null
    },
  ) =>
    request<ProjectDeployAccepted>(`/v1/projects/${encodeURIComponent(slug)}/deploy`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  cancelProjectDeploy: (
    slug: string,
    body: {
      version?: string | null
      target_filter?: 'all' | 'dev'
      group_id?: string | null
    } = {},
  ) =>
    request<ProjectDeployCancelAccepted>(`/v1/projects/${encodeURIComponent(slug)}/deploy/cancel`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  listProjectMembers: (slug: string) =>
    request<{ members: ProjectMemberSummary[] }>(
      `/v1/projects/${encodeURIComponent(slug)}/members`,
    ),

  upsertProjectMember: (
    slug: string,
    username: string,
    body: { role: 'owner' | 'operator' | 'viewer' },
  ) =>
    request<ProjectMemberSummary>(
      `/v1/projects/${encodeURIComponent(slug)}/members/${encodeURIComponent(username)}`,
      { method: 'PUT', body: JSON.stringify({ role: body.role }) },
    ),

  removeProjectMember: (slug: string, username: string) =>
    request<void>(
      `/v1/projects/${encodeURIComponent(slug)}/members/${encodeURIComponent(username)}`,
      { method: 'DELETE' },
    ),

  listProjectSecrets: (slug: string) =>
    request<ProjectSecretList>(`/v1/projects/${encodeURIComponent(slug)}/secrets`),

  upsertProjectSecret: (slug: string, key: string, value: string) =>
    request<ProjectSecretList>(
      `/v1/projects/${encodeURIComponent(slug)}/secrets/${encodeURIComponent(key)}`,
      { method: 'PUT', body: JSON.stringify({ value }) },
    ),

  deleteProjectSecret: (slug: string, key: string) =>
    request<ProjectSecretList>(
      `/v1/projects/${encodeURIComponent(slug)}/secrets/${encodeURIComponent(key)}`,
      { method: 'DELETE' },
    ),

  listBuilds: (slug: string) =>
    request<BuildJobList>(`/v1/projects/${encodeURIComponent(slug)}/builds`),

  createBuild: (slug: string, body: BuildJobCreate, runInline = false) =>
    request<BuildJobSummary>(
      runInline
        ? `/v1/projects/${encodeURIComponent(slug)}/builds?run_inline=true`
        : `/v1/projects/${encodeURIComponent(slug)}/builds`,
      {
        method: 'POST',
        body: JSON.stringify(body),
      },
    ),

  getBuild: (slug: string, buildId: string) =>
    request<BuildJobDetail>(
      `/v1/projects/${encodeURIComponent(slug)}/builds/${encodeURIComponent(buildId)}`,
    ),

  cancelBuild: (slug: string, buildId: string) =>
    request<BuildJobSummary>(
      `/v1/projects/${encodeURIComponent(slug)}/builds/${encodeURIComponent(buildId)}/cancel`,
      { method: 'POST' },
    ),

  retryBuild: (slug: string, buildId: string, runInline = false) =>
    request<BuildJobSummary>(
      runInline
        ? `/v1/projects/${encodeURIComponent(slug)}/builds/${encodeURIComponent(buildId)}/retry?run_inline=true`
        : `/v1/projects/${encodeURIComponent(slug)}/builds/${encodeURIComponent(buildId)}/retry`,
      { method: 'POST' },
    ),

  listPolicies: (slug: string) =>
    request<DeployPolicyList>(`/v1/projects/${encodeURIComponent(slug)}/policies`),

  createPolicy: (slug: string, body: DeployPolicyCreate) =>
    request<DeployPolicySummary>(`/v1/projects/${encodeURIComponent(slug)}/policies`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  updatePolicy: (slug: string, policyId: string, patch: DeployPolicyUpdate) =>
    request<DeployPolicySummary>(
      `/v1/projects/${encodeURIComponent(slug)}/policies/${encodeURIComponent(policyId)}`,
      {
        method: 'PATCH',
        body: JSON.stringify(patch),
      },
    ),

  deletePolicy: (slug: string, policyId: string) =>
    request<void>(
      `/v1/projects/${encodeURIComponent(slug)}/policies/${encodeURIComponent(policyId)}`,
      { method: 'DELETE' },
    ),

  evaluatePolicy: (slug: string, policyId: string, version: string) =>
    request<DeployPolicyEvaluation>(
      `/v1/projects/${encodeURIComponent(slug)}/policies/${encodeURIComponent(policyId)}/evaluate`,
      {
        method: 'POST',
        body: JSON.stringify({ version }),
      },
    ),

  listAdminTokens: () => request<{ tokens: AdminTokenSummary[] }>('/v1/auth/tokens'),

  issueAdminToken: (body: {
    name: string
    project?: string | null
    scopes?: string[]
    expires_in_days?: number | null
  }) =>
    request<AdminTokenIssued>('/v1/auth/tokens', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  createAdminToken: (body: {
    name: string
    project?: string | null
    scopes?: string[]
    expires_in_days?: number | null
  }) =>
    request<AdminTokenIssued>('/v1/auth/tokens', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  revokeAdminToken: (id: string) =>
    request<void>(`/v1/auth/tokens/${encodeURIComponent(id)}/revoke`, { method: 'POST' }),

  loginFirebase: (idToken: string) =>
    request<LoginResult>('/v1/auth/firebase', {
      method: 'POST',
      body: JSON.stringify({ id_token: idToken }),
    }),

  listUsers: () => request<{ users: UserSummary[] }>('/v1/users'),

  createUser: (body: {
    username: string
    email?: string | null
    display_name?: string | null
    password?: string | null
    is_superadmin?: boolean
    send_invite_email?: boolean
    project?: string | null
    role?: 'owner' | 'operator' | 'viewer' | null
  }) =>
    request<UserSummary>('/v1/users', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  updateUser: (
    username: string,
    body: {
      email?: string | null
      display_name?: string | null
      password?: string | null
      is_superadmin?: boolean
      disabled?: boolean
      send_password_reset?: boolean
    },
  ) =>
    request<UserSummary>(`/v1/users/${encodeURIComponent(username)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  deleteUser: (username: string) =>
    request<void>(`/v1/users/${encodeURIComponent(username)}`, {
      method: 'DELETE',
    }),
}
