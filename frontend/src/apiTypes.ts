// Shared TypeScript wire contracts mirroring FastAPI OpenAPI schemas (`D-3 / A-16 / T-6`).
// Extracted from `api.ts` and re-exported there; `tests/test_frontend_contract.py` checks
// every exported object type in both files against `/v1/openapi.json`.

export type Health = {
  status: string
  version: string
  commit?: string
  built_at?: string
}

export type UserProjectSummary = {
  id: string
  slug: string
  name: string
  description: string | null
  role: string
}

export type Me = {
  token_id: string
  subject: string
  scopes: string[]
  expires_at: string | null
  is_superadmin?: boolean
  project_id?: string | null
  projects?: UserProjectSummary[]
  user_id?: string | null
  email?: string | null
  display_name?: string | null
  has_local_password?: boolean
}

export type SelfProfileUpdate = {
  email?: string | null
  display_name?: string | null
  current_password?: string | null
  password?: string | null
}

export type TrackMode = 'off' | 'branch' | 'tag' | 'commit'
export type AutoDeployTarget = 'off' | 'dev' | 'all'

export type ProjectSummary = {
  id: string
  slug: string
  name: string
  description: string | null
  role: string | null
  repo_url?: string | null
  track_mode?: TrackMode
  track_ref?: string | null
  auto_deploy_target?: AutoDeployTarget
  ci_webhook_url?: string | null
  has_ci_webhook_header?: boolean
  secrets_rev?: number
  otel_endpoint?: string | null
  has_otel_headers?: boolean
  auto_deployed_devices?: string[] | null
  ci_webhook_triggered?: boolean | null
  created_at: string
  updated_at?: string
}

export type ProjectUpdate = {
  name?: string
  description?: string | null
  repo_url?: string | null
  track_mode?: TrackMode
  track_ref?: string | null
  auto_deploy_target?: AutoDeployTarget
  ci_webhook_url?: string | null
  ci_webhook_header?: string | null
  otel_endpoint?: string | null
  otel_headers?: string | null
}

export type ProjectSecretSummary = {
  key: string
  updated_at: string
}

export type ProjectSecretList = {
  secrets_rev: number
  secrets: ProjectSecretSummary[]
}

export type ProjectDeployAccepted = {
  project: string
  version: string
  target_filter: 'all' | 'dev'
  deployed: DeployAccepted[]
  skipped: Record<string, string>
}

export type ProjectDeployCancelAccepted = {
  project: string
  cancelled: CommandAccepted[]
  skipped: Record<string, string>
}

export type ProjectMemberSummary = {
  user_id: string
  username: string
  display_name: string | null
  email?: string | null
  is_superadmin?: boolean
  role: 'owner' | 'operator' | 'viewer'
  created_at: string
}

export type DeviceGroupSummary = {
  id: string
  project_id: string
  name: string
  description: string | null
  created_at: string
  updated_at: string
}

export type UserProjectRole = {
  project_slug: string
  role: string
}

export type UserSummary = {
  id: string
  username: string
  email: string | null
  firebase_uid: string | null
  has_local_password: boolean
  display_name: string | null
  is_superadmin: boolean
  disabled_at: string | null
  created_at: string
  email_delivery?: 'sent' | 'failed' | 'not_configured' | null
  projects?: UserProjectRole[]
}

export type AdminTokenSummary = {
  id: string
  name: string
  subject: string
  scopes: string[]
  project_id: string | null
  project_slug: string | null
  created_at: string
  last_used_at: string | null
  expires_at: string | null
  revoked_at: string | null
}

export type AdminTokenIssued = {
  id: string
  token: string
  name: string
  subject: string
  scopes: string[]
  project_id: string | null
  project_slug: string | null
  created_at: string
  expires_at: string | null
}

export type ArtifactType =
  | 'app'
  | 'data_partition'
  | 'mcu_fw'
  | 'model_weights'
  | 'fpga_bitstream'
  | string

export type LoginResult = { expires_at: string }

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

export type EnrollmentTokenIssued = {
  id: string
  token: string
  group_id: string | null
  expires_at: string
  created_at: string
}

export type HealthSnapshot = {
  at: string
  uptime_s: number | null
  rssi: number | null
  free_heap: number | null
  min_free_heap: number | null
  boot_ok: boolean | null
}

export type FaultResets = { window_s: number; panic: number; task_wdt: number; brownout: number }

export type MetricValue = { name: string; value: number; at: string }

export type DeviceSummary = {
  device_id: string
  name: string | null
  group_id: string | null
  platform_type: string
  fw_version: string | null
  agent_version: string | null
  link_type: string
  ssid: string | null
  known_networks: number | null
  power_class: string
  expected_wake_interval_s: number | null
  parent_device_id: string | null
  partition_layout: string | null
  ota_slot_size: number | null
  flash_chip_size?: number | null
  partition_table_sha256?: string | null
  rollback_capable?: boolean | null
  capabilities: string[]
  last_seen: string | null
  enrolled_at: string
  broker_provisioned_at: string | null
  online: boolean
  last_reset_reason?: string | null
  fault_resets?: FaultResets | null
  health?: HealthSnapshot | null
  metrics?: MetricValue[]
  last_command?: CommandSummary | null
  is_dev?: boolean
  stay_awake?: boolean
  secrets_rev?: number | null
  sub_versions?: Record<string, string> | null
  safe_mode?: boolean
  deploy: DeploySummary | null
}

export type CommandSummary = {
  command_id: string
  type: string
  cmd_id: string | null
  sent_at: string
  sent_by: DeploySender | null
  result: string | null
  detail: string | null
  answered_at: string | null
}

export type CommandAccepted = {
  command_id: string
  device_id: string
  type: string
  cmd_id: string | null
  device_online: boolean
}

export type DeviceUpdate = {
  name?: string | null
  group_id?: string | null
  project?: string
  is_dev?: boolean
  stay_awake?: boolean
}

export type DeploySender = { subject: string; credential: string | null }

export type DeploySummary = {
  cmd_id: string | null
  state: string
  at: string
  is_terminal: boolean
  artifact_version: string | null
  from_version: string | null
  pct: number | null
  detail: string | null
  steps?: DeployStep[]
  confirm_timeout_s?: number
  sent_by?: DeploySender | null
  selftest?: DeploySelfTest | null
}

export type DeploySelfTest = {
  outcome: 'passed' | 'failed' | 'timed_out'
  reason: string | null
  returned_to: string | null
}

export type DeployStep = { state: string; at: string }

export type ArrivalSummary = {
  device_id: string
  stage: string
  detail: string | null
  at: string
  stalled: boolean
}

export type DeviceList = { devices: DeviceSummary[]; arrivals: ArrivalSummary[] }

export type DeviceLogLine = {
  id: number
  logged_at: string
  received_at: string
  level: string
  text: string | null
  dropped: number
  context?: Record<string, string | number | boolean> | null
}
export type DeviceLogTail = { device_id: string; lines: DeviceLogLine[] }
export type DeviceLogList = DeviceLogTail

export type TelemetryPoint = {
  at: string
  value: number
}

export type TelemetrySeries = {
  device_id: string
  name: string
  samples: TelemetryPoint[]
}

export type DockSyncRequest = {
  announce?: Record<string, unknown> | null
  statuses?: Record<string, unknown>[]
  telemetry?: Record<string, unknown>[]
  logs?: Record<string, unknown>[]
}

export type DockSyncResponse = {
  device_id: string
  link_type: string
  fw_version: string | null
  sub_versions?: Record<string, string> | null
  statuses_recorded: number
  telemetry_batches_recorded: number
  log_batches_recorded: number
  device: DeviceSummary
}

export type ConfigPartition = { label: string; offset: number; size: number }

export type AgentPartInfo = { name: string; offset: number; size: number; sha256: string }

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
  config_sha256: string | null
  build_digest: string | null
  config_partition: ConfigPartition | null
  parts: AgentPartInfo[]
}

export type AgentManifest = { agent_version: string; builds: AgentBuildInfo[] }

export type SimStatus = 'pending' | 'running' | 'passed' | 'failed' | 'skipped'

export type ArtifactSimulateResponse = {
  sha256: string
  target: string | null
  sim_status: SimStatus
  sim_detail: string | null
  sim_ran_at: string
}

export type ArtifactSummary = {
  target: string
  version: string
  sha256: string
  size_bytes: number
  partition_layout: string | null
  kind: string
  artifact_type?: string
  created_at: string
  sig?: string | null
  repo_url?: string | null
  branch?: string | null
  commit_sha?: string | null
  tag?: string | null
  build_url?: string | null
  test_passed?: boolean | null
  sim_status?: SimStatus | null
  sim_detail?: string | null
  sim_ran_at?: string | null
}

export type ArtifactList = { artifacts: ArtifactSummary[] }

export type ArtifactUploaded = {
  sha256: string
  size_bytes: number
  target: string
  version: string
  partition_layout: string
  artifact_type?: string
  created: boolean
  has_lib_marker?: boolean
  sig?: string | null
  repo_url?: string | null
  branch?: string | null
  commit_sha?: string | null
  tag?: string | null
  build_url?: string | null
  test_passed?: boolean | null
  auto_deployed_devices?: string[] | null
  sim_status?: SimStatus | null
  sim_detail?: string | null
  sim_ran_at?: string | null
}

export type DeployKpisSummary = {
  total_completed: number
  confirmed: number
  rolled_back: number
  failed: number
  cancelled: number
  in_flight: number
  delivery_success_pct: number | null
  fleet_safety_pct: number | null
  safety_gap_pct: number | null
  delivery_target_pct: number
  safety_target_pct: number
  delivery_target_met: boolean | null
  safety_target_met: boolean | null
}

export type DeployAccepted = {
  cmd_id: string
  device_id: string
  version: string
  sha256: string
  size_bytes: number
  apply: string
  reused: boolean
  device_online: boolean
}

export type PrecheckFinding = { code: string; message: string; needs_override?: boolean }

export type DeployPrecheck = {
  device_id: string
  target: string
  version: string
  from_version: string | null
  sha256: string | null
  size_bytes: number | null
  artifact_partition_layout: string | null
  device_partition_layout: string | null
  device_partition_profile?: string | null
  ota_slot_size: number | null
  power_class: string
  expected_wake_interval_s: number | null
  device_online: boolean
  confirm_timeout_s: number
  deployable: boolean
  refusals: PrecheckFinding[]
  warnings: PrecheckFinding[]
}

export type PartitionProfileOrigin = 'builtin' | 'user' | 'detected'

export type PartitionProfileSummary = {
  partition_table_sha256: string
  layout_id: string | null
  origin: PartitionProfileOrigin
  deployable: boolean
  ota_slot_size: number | null
  flash_chip_size: number | null
  detected_device_id: string | null
  device_ids: string[]
  created_at: string
  adopted_at: string | null
}

export type PartitionProfileList = { profiles: PartitionProfileSummary[] }

export type PartitionProfileAdopt = { layout_id: string; ota_slot_size?: number }

export type BuildJobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'

export type BuildTarget = 'esp32' | 'esp32s3' | 'esp32c3' | 'esp32c6'

export type BuildJobCreate = {
  repo_url?: string | null
  git_ref: string
  target: BuildTarget
  version: string
  partition_layout?: string
}

export type BuildJobSummary = {
  id: string
  project_id: string
  repo_url: string
  git_ref: string
  commit_sha: string | null
  target: string
  version: string
  partition_layout: string
  idf_image: string
  cache_key: string | null
  status: BuildJobStatus
  cache_hit: boolean
  artifact_sha256: string | null
  error_detail: string | null
  triggered_by: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export type BuildJobDetail = {
  id: string
  project_id: string
  repo_url: string
  git_ref: string
  commit_sha: string | null
  target: string
  version: string
  partition_layout: string
  idf_image: string
  cache_key: string | null
  status: BuildJobStatus
  cache_hit: boolean
  artifact_sha256: string | null
  error_detail: string | null
  triggered_by: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  log_text: string
}

export type BuildJobList = {
  builds: BuildJobSummary[]
}

export type PolicyMatchMode = 'branch' | 'tag'
export type PolicyEvalStatus = 'in_progress' | 'soaking' | 'halted' | 'completed' | 'skipped'
export type PolicyRingState =
  | 'pending'
  | 'in_progress'
  | 'soaking'
  | 'promoted'
  | 'halted'
  | 'empty'

export type DeployPolicyCreate = {
  name: string
  match_mode: PolicyMatchMode
  match_ref: string
  ring_group_ids: string[]
  max_parallel?: number
  require_sim_passed?: boolean
  min_soak_s?: number
  min_free_heap_bytes?: number | null
  enabled?: boolean
}

export type DeployPolicyUpdate = {
  name?: string | null
  match_mode?: PolicyMatchMode | null
  match_ref?: string | null
  ring_group_ids?: string[] | null
  max_parallel?: number | null
  require_sim_passed?: boolean | null
  min_soak_s?: number | null
  min_free_heap_bytes?: number | null
  enabled?: boolean | null
}

export type DeployPolicySummary = {
  id: string
  project_id: string
  name: string
  match_mode: PolicyMatchMode
  match_ref: string
  ring_group_ids: string[]
  max_parallel: number
  require_sim_passed: boolean
  min_soak_s: number
  min_free_heap_bytes: number | null
  enabled: boolean
  created_at: string
  updated_at: string
}

export type DeployPolicyList = {
  policies: DeployPolicySummary[]
}

export type DeployPolicyEvaluateRequest = {
  version: string
}

export type PolicyRingStatus = {
  ring_index: number
  group_id: string
  status: PolicyRingState
  total_devices: number
  confirmed_devices: number
  in_flight_devices: number
}

export type DeployPolicyEvaluation = {
  policy_id: string
  version: string
  status: PolicyEvalStatus
  halt_reason: string | null
  rings: PolicyRingStatus[]
  deployed: DeployAccepted[]
}

export type GatewayRelayConfigRequest = {
  relay_base_url: string
  cached_artifacts?: string[]
}

export type GatewayArtifactPull = {
  sha256: string
  size_bytes: number
  url: string
}

export type GatewayRelaySummary = {
  gateway_id: string
  relay_base_url: string
  cached_artifacts: string[]
  child_device_ids: string[]
  needed_artifacts: GatewayArtifactPull[]
  updated_at: string
}

export type CreateDeltaRequest = {
  from_sha256: string
}

export type ArtifactDeltaSummary = {
  from_sha256: string
  to_sha256: string
  patch_sha256: string
  patch_size_bytes: number
  target_size_bytes: number
  savings_pct: number
  format: string
  created_at: string
}

export type ArtifactDeltaList = {
  deltas: ArtifactDeltaSummary[]
}

export type SwarmChildNode = {
  device_id: string
  name: string | null
  platform_type: string
  fw_version: string | null
  link_type: string
  online: boolean
  has_delta_capability: boolean
}

export type SwarmGatewayNode = {
  gateway_id: string
  name: string | null
  platform_type: string
  fw_version: string | null
  online: boolean
  relay_base_url: string | null
  cached_artifacts: string[]
  children: SwarmChildNode[]
}

export type SwarmTopologyResponse = {
  project: string
  gateways: SwarmGatewayNode[]
  direct_device_count: number
  deltas: ArtifactDeltaSummary[]
}
