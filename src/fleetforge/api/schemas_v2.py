"""V2 & V3 API request/response schemas (`R10` builds, `R11` policies, `V3` swarm/delta)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fleetforge.api.schemas_deploys import DeployAccepted

BuildJobStatusLiteral = Literal["queued", "running", "succeeded", "failed", "cancelled"]
BuildTargetLiteral = Literal["esp32", "esp32s3", "esp32c3", "esp32c6"]
PolicyMatchModeLiteral = Literal["branch", "tag"]
PolicyEvalStatusLiteral = Literal["in_progress", "soaking", "halted", "completed", "skipped"]


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
    """`POST /v1/projects/{slug}/policies/{policy_id}/evaluate` request body (`R11-1`)."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=64)


class PolicyRingStatus(BaseModel):
    """Status of one group cohort ring within a `DeployPolicyEvaluation` (`R11-1`)."""

    ring_index: int
    group_id: uuid.UUID
    status: Literal["pending", "in_progress", "soaking", "promoted", "halted", "empty"]
    total_devices: int
    confirmed_devices: int
    in_flight_devices: int


class DeployPolicyEvaluation(BaseModel):
    """Result of evaluating or promoting a `DeployPolicy` on a firmware version (`R11-1`)."""

    policy_id: uuid.UUID
    version: str
    status: PolicyEvalStatusLiteral
    halt_reason: str | None
    rings: list[PolicyRingStatus]
    deployed: list[DeployAccepted]
