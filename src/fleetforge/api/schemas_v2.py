"""V2 & V3 API request/response schemas (`R10` builds, `R11` policies, `V3` swarm/delta)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from fleetforge.rollout_types import (
    DeployAccepted as DeployAccepted,
)
from fleetforge.rollout_types import (
    DeployPolicyEvaluation as DeployPolicyEvaluation,
)
from fleetforge.rollout_types import (
    PolicyEvalStatusLiteral as PolicyEvalStatusLiteral,
)
from fleetforge.rollout_types import (
    PolicyRingStatus as PolicyRingStatus,
)
from fleetforge.url_safety import validate_git_ref

BuildJobStatusLiteral = Literal["queued", "running", "succeeded", "failed", "cancelled"]
BuildTargetLiteral = Literal["esp32", "esp32s3", "esp32c3", "esp32c6"]
PolicyMatchModeLiteral = Literal["branch", "tag"]


class BuildJobCreate(BaseModel):
    """`POST /v1/projects/{slug}/builds` request body (`R10-1`, `R10-2`)."""

    model_config = ConfigDict(extra="forbid")

    repo_url: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="Git repository URL (defaults to project.repo_url when omitted)",
    )
    git_ref: str = Field(
        min_length=1,
        max_length=128,
        description="Branch, tag, or commit SHA to build",
    )
    target: BuildTargetLiteral
    version: str = Field(min_length=1, max_length=64)
    partition_layout: str = Field(default="ab-4m-v1", min_length=1, max_length=32)

    @field_validator("git_ref")
    @classmethod
    def _check_git_ref(cls, value: str) -> str:
        return validate_git_ref(value)


class BuildJobSummary(BaseModel):
    """One server-side build job without full log text (`R10-2`)."""

    id: uuid.UUID
    project_id: uuid.UUID
    repo_url: str
    git_ref: str
    commit_sha: str | None
    target: str
    version: str
    partition_layout: str
    idf_image: str
    cache_key: str | None
    status: BuildJobStatusLiteral
    cache_hit: bool
    artifact_sha256: str | None
    error_detail: str | None
    triggered_by: str | None
    created_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None


class BuildJobDetail(BuildJobSummary):
    """Single build job including captured `log_text` (`GET /v1/projects/{slug}/builds/{id}`)."""

    log_text: str


class BuildJobList(BaseModel):
    """Envelope returned by `GET /v1/projects/{slug}/builds`."""

    builds: list[BuildJobSummary]


class DeployPolicyCreate(BaseModel):
    """`POST /v1/projects/{slug}/policies` request body (`R11-1`)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    match_mode: PolicyMatchModeLiteral
    match_ref: str = Field(min_length=1, max_length=128)
    ring_group_ids: list[uuid.UUID] = Field(min_length=1, max_length=16)
    max_parallel: int = Field(default=5, ge=1, le=100)
    require_sim_passed: bool = False
    min_soak_s: int = Field(default=0, ge=0, le=86400)
    min_free_heap_bytes: int | None = Field(default=None, ge=0)
    enabled: bool = True


class DeployPolicyUpdate(BaseModel):
    """`PATCH /v1/projects/{slug}/policies/{policy_id}` request body (`R11-1`)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64)
    match_mode: PolicyMatchModeLiteral | None = None
    match_ref: str | None = Field(default=None, min_length=1, max_length=128)
    ring_group_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=16)
    max_parallel: int | None = Field(default=None, ge=1, le=100)
    require_sim_passed: bool | None = None
    min_soak_s: int | None = Field(default=None, ge=0, le=86400)
    min_free_heap_bytes: int | None = Field(default=None, ge=0)
    enabled: bool | None = None


class DeployPolicySummary(BaseModel):
    """One multi-ring cohort deploy policy (`R11-1`)."""

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    match_mode: PolicyMatchModeLiteral
    match_ref: str
    ring_group_ids: list[uuid.UUID]
    max_parallel: int
    require_sim_passed: bool
    min_soak_s: int
    min_free_heap_bytes: int | None
    enabled: bool
    created_at: dt.datetime
    updated_at: dt.datetime


class DeployPolicyList(BaseModel):
    """Envelope returned by `GET /v1/projects/{slug}/policies`."""

    policies: list[DeployPolicySummary]


class DeployPolicyEvaluateRequest(BaseModel):
    """`POST /v1/projects/{slug}/policies/{policy_id}/evaluate` request body (`R11-1`, `A-22`)."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=64)
    resume: bool = False


class GatewayRelayConfigRequest(BaseModel):
    """`PUT /v1/devices/{gateway_id}/relay` request body (`V3-1`)."""

    model_config = ConfigDict(extra="forbid")

    relay_base_url: str = Field(min_length=7, max_length=256)
    cached_artifacts: list[str] = Field(default_factory=list, max_length=512)


class GatewayArtifactPull(BaseModel):
    """Upstream pull descriptor so a UGV gateway caches an artifact once for its children (`V3-1`)."""

    sha256: str
    size_bytes: int
    url: str


class GatewayRelaySummary(BaseModel):
    """Edge-relay gateway cache state & pending upstream pulls (`V3-1`)."""

    gateway_id: str
    relay_base_url: str
    cached_artifacts: list[str]
    child_device_ids: list[str]
    needed_artifacts: list[GatewayArtifactPull]
    updated_at: dt.datetime


class GatewayChildSyncBundle(BaseModel):
    """Offline `up/*` envelopes buffered by a UGV gateway for one child node (`V3-1`)."""

    model_config = ConfigDict(extra="forbid")

    announce: dict[str, Any] | None = None
    statuses: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    telemetry: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    logs: list[dict[str, Any]] = Field(default_factory=list, max_length=64)


class GatewayRelaySyncRequest(BaseModel):
    """`POST /v1/devices/{gateway_id}/relay-sync` request body (`V3-1`)."""

    model_config = ConfigDict(extra="forbid")

    children: dict[str, GatewayChildSyncBundle] = Field(default_factory=dict)
    cached_artifacts: list[str] | None = Field(default=None, max_length=512)


class GatewayRelaySyncResponse(BaseModel):
    """Summary of child `up/*` messages replayed from a reconnecting UGV gateway (`V3-1`)."""

    gateway_id: str
    synced_children: list[str]
    statuses_recorded: int
    telemetry_batches_recorded: int
    log_batches_recorded: int
    cached_artifacts: list[str]


class CreateDeltaRequest(BaseModel):
    """`POST /v1/artifact/{to_sha256}/deltas` request body (`V3-2`)."""

    model_config = ConfigDict(extra="forbid")

    from_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArtifactDeltaSummary(BaseModel):
    """Precomputed `ffdelta-v1` binary patch metadata (`V3-2`)."""

    from_sha256: str
    to_sha256: str
    patch_sha256: str
    patch_size_bytes: int
    target_size_bytes: int
    savings_pct: float
    format: str
    created_at: dt.datetime


class ArtifactDeltaList(BaseModel):
    """Envelope returned by `GET /v1/projects/{slug}/deltas` (`V3-2`)."""

    deltas: list[ArtifactDeltaSummary]


class SwarmChildNode(BaseModel):
    """One child drone/sensor node attached via `parent_device_id` (`V3-1`)."""

    device_id: str
    name: str | None
    platform_type: str
    fw_version: str | None
    link_type: str
    online: bool
    has_delta_capability: bool


class SwarmGatewayNode(BaseModel):
    """One parent UGV / edge-relay gateway and its attached child swarm (`V3-1`)."""

    gateway_id: str
    name: str | None
    platform_type: str
    fw_version: str | None
    online: bool
    relay_base_url: str | None
    cached_artifacts: list[str]
    children: list[SwarmChildNode]


class SwarmTopologyResponse(BaseModel):
    """`GET /v1/projects/{slug}/swarm` topology + binary delta summary (`V3-1`, `V3-2`)."""

    project: str
    gateways: list[SwarmGatewayNode]
    direct_device_count: int
    deltas: list[ArtifactDeltaSummary]
