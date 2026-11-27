// Shared TypeScript wire contracts mirroring FastAPI OpenAPI schemas (`D-3 / D-10 / A-16 / T-6`).
// Server schemas are generated into `./apiGenerated` by `scripts/generate_openapi_ts.py`;
// this module re-exports them alongside ergonomic client-side type aliases.

import type {
  ArtifactSimulateResponse,
  BuildJobCreate,
  BuildJobSummary,
  DeployPolicyEvaluation,
  DeployPolicySummary,
  DeviceLogTail,
  EnrollmentTokenStatus,
  LoginResponse,
  MeResponse,
  PartitionProfileSummary,
  PolicyRingStatus,
  ProjectUpdate,
} from './apiGenerated'

export * from './apiGenerated'

export type Me = MeResponse
export type LoginResult = LoginResponse
export type TokenStatus = EnrollmentTokenStatus
export type DeviceLogList = DeviceLogTail

export type TrackMode = NonNullable<ProjectUpdate['track_mode']>
export type AutoDeployTarget = NonNullable<ProjectUpdate['auto_deploy_target']>
export type SimStatus = ArtifactSimulateResponse['sim_status']
export type PartitionProfileOrigin = PartitionProfileSummary['origin']
export type BuildJobStatus = BuildJobSummary['status']
export type BuildTarget = BuildJobCreate['target']
export type PolicyMatchMode = DeployPolicySummary['match_mode']
export type PolicyEvalStatus = DeployPolicyEvaluation['status']
export type PolicyRingState = PolicyRingStatus['status']

export type ArtifactType =
  | 'app'
  | 'data_partition'
  | 'mcu_fw'
  | 'model_weights'
  | 'fpga_bitstream'
  | string
