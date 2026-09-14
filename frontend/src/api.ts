// The dashboard's only HTTP surface.
//
// Every path here is **relative**. The API shares this page's origin (nginx in the
// `frontend` container proxies `/v1/*` to `api:8000`; `vite.config.ts` does the same
// in dev), so there is no base URL to configure and no CORS preflight to satisfy.
// An absolute URL anywhere in this file breaks the one-origin invariant
// (design/production.md -> "Same origin, two backends") and the repair someone
// reaches for is CORS middleware on the API, which is the wrong repair.

export type Health = { status: string; version: string }

export type Me = {
  token_id: string
  subject: string
  scopes: string[]
  expires_at: string | null
}

export type LoginResult = { expires_at: string }

// Mirrors `EnrollmentTokenStatus` in api/schemas.py. The server derives this from the
// burn predicate itself, so the dashboard never says "active" about a token that
// would not enroll — do not recompute it here from the timestamps.
export type TokenStatus = 'active' | 'used' | 'revoked' | 'expired'

export type EnrollmentTokenSummary = {
  id: string
  group_id: string | null
  status: TokenStatus
  created_at: string
  expires_at: string
  used_at: string | null
  used_by_device_id: string | null
  revoked_at: string | null
}

// The plaintext `token` exists in this object and nowhere else in the system: not in
// Postgres, not in a log line, not retrievable from any endpoint. It must never be
// written to localStorage, a URL, or an error message.
export type EnrollmentTokenIssued = {
  id: string
  token: string
  group_id: string | null
  expires_at: string
  created_at: string
}

// Mirrors `DeviceSummary` in api/schemas.py, field for field and in its order.
//
// `online` is computed SERVER-SIDE on every read, by `fleetforge.presence.is_online`.
// It must never be re-derived here from `last_seen`: the 2.5x tolerance lives in one
// place (`config.presence_tolerance`), and `presence_reported` — the other ingredient
// — is deliberately not in this payload precisely so that no client can try.
export type DeviceSummary = {
  device_id: string
  name: string | null
  group_id: string | null
  platform_type: string
  fw_version: string | null
  agent_version: string | null
  link_type: string
  power_class: string
  expected_wake_interval_s: number | null
  parent_device_id: string | null
  partition_layout: string | null
  ota_slot_size: number | null
  capabilities: string[]
  last_seen: string | null
  enrolled_at: string
  broker_provisioned_at: string | null
  online: boolean
}

// Mirrors `ArrivalSummary` in api/schemas.py. A board that has reported a boot stage and
// is NOT yet in the fleet — so `device_id` here may name a board with no row in `devices`
// at all. That is the point: it is what fills the gap between "flashed" and "online".
//
// `stalled` is computed SERVER-SIDE from the age of `at`, the same rule and the same
// reason as `DeviceSummary.online`: the threshold lives once, in `config.progress_stall_s`,
// and is deliberately not in this payload so that no client can try to re-derive it.
//
// `stage` and `detail` are DEVICE-CONTROLLED strings, bounded only in length and charset.
// Render them as text, and never switch on `stage` in a way that breaks on a value this
// file has not heard of — the server does not whitelist the vocabulary either.
export type ArrivalSummary = {
  device_id: string
  stage: string
  detail: string | null
  at: string
  stalled: boolean
}

export type DeviceList = { devices: DeviceSummary[]; arrivals: ArrivalSummary[] }

// Mirrors `firmware/manifest.py::ConfigPartition`. Optional in the schema ON PURPOSE:
// bundles built before R0-fw-1 have nowhere to write a board's config, and the flasher
// must refuse those rather than silently flash a board that can never enroll.
export type ConfigPartition = { label: string; offset: number; size: number }

// Mirrors `AgentPartInfo` in api/schemas.py. `offset` is where this part is written —
// NEVER derive one: the bootloader sits at 0x1000 on ESP32 and 0x0 on the RISC-V parts,
// and a hardcoded offset flashes cleanly and never boots on half the fleet.
export type AgentPartInfo = { name: string; offset: number; size: number; sha256: string }

// Mirrors `AgentBuildInfo` in api/schemas.py, field for field and in its order.
// `chip_family` is exactly `ESPLoader.chip.CHIP_NAME` ('ESP32', 'ESP32-C3', 'ESP32-C6',
// 'ESP32-S3') — compare it with `===`, never through a translation table.
export type AgentBuildInfo = {
  target: string
  chip_family: string
  agent_version: string
  idf_version: string
  idf_image: string
  source_commit: string
  built_at: string
  partition_layout: string
  ota_slot_size: number
  flash_size: string
  // S0-infra-3 build identity: the sha256 of the build's `sdkconfig.resolved`, and one
  // digest over the parts plus the provenance fields. `null` for a bundle built before
  // the field existed. Printed verbatim in the diagnostic bundle (`diagnostics.ts`) and
  // compared as strings — never truncated, never parsed.
  config_sha256: string | null
  build_digest: string | null
  config_partition: ConfigPartition | null
  parts: AgentPartInfo[]
}

export type AgentManifest = { agent_version: string; builds: AgentBuildInfo[] }

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
      // The session is an HttpOnly cookie; there is no header to attach and no token
      // JavaScript can read. Same-origin (not 'include') because cross-origin is the
      // thing this app does not do.
      credentials: 'same-origin',
      headers:
        init.body === undefined ? init.headers : { 'content-type': 'application/json', ...init.headers },
    })
  } catch {
    // fetch() rejects only on a transport failure; there is no status to report.
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
 *
 * Same `credentials: 'same-origin'` and the same `ApiError` mapping, so a session that
 * dies mid-flash still bounces to the login gate instead of writing a 401 page into a
 * board's flash. A separate function rather than a flag on `request` because the two
 * differ in their whole body handling and `T` would be a lie.
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

/** FastAPI puts the human-readable reason in `detail`; fall back to the raw body. */
function detailOf(body: string): string | null {
  try {
    const parsed: unknown = JSON.parse(body)
    if (parsed && typeof parsed === 'object' && 'detail' in parsed) {
      const detail = (parsed as { detail: unknown }).detail
      if (typeof detail === 'string') return detail
    }
  } catch {
    /* not JSON — fall through */
  }
  return body.trim() === '' ? null : body.trim()
}

export const api = {
  health: () => request<Health>('/v1/healthz'),

  me: () => request<Me>('/v1/auth/me'),

  // The password goes in the body of a POST and nowhere else. The response
  // deliberately does not carry the token — it arrives as a Set-Cookie.
  login: (password: string) =>
    request<LoginResult>('/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),

  logout: () => request<void>('/v1/auth/logout', { method: 'POST' }),

  // The fleet read model, and the ONLY source of device state in this app. A frame on
  // `/v1/events` is a hint that says "go re-read"; this is the re-read.
  listDevices: () => request<DeviceList>('/v1/devices'),

  listEnrollmentTokens: () => request<{ tokens: EnrollmentTokenSummary[] }>('/v1/enrollment-tokens'),

  // R0 has no group CRUD, so every token is ungrouped. The parameter exists because
  // the API takes it, not because the UI can populate it yet.
  issueEnrollmentToken: (groupId: string | null = null) =>
    request<EnrollmentTokenIssued>('/v1/enrollment-tokens', {
      method: 'POST',
      body: JSON.stringify({ group_id: groupId }),
    }),

  revokeEnrollmentToken: (id: string) =>
    request<void>(`/v1/enrollment-tokens/${encodeURIComponent(id)}/revoke`, { method: 'POST' }),

  // The prebuilt agent images the Web Serial flasher writes (R0-fe-3). Every offset the
  // flasher uses comes out of this manifest.
  agentManifest: () => request<AgentManifest>('/v1/agent/manifest'),

  // `target`/`part` are manifest-supplied logical ids, not operator input — but encode
  // them anyway, the same rule `revokeEnrollmentToken` follows.
  agentPart: (target: string, part: string) =>
    requestBytes(`/v1/agent/${encodeURIComponent(target)}/${encodeURIComponent(part)}`),
}
