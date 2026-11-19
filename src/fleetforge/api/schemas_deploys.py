"""Artifact, deploy orchestration, and partition profile API schemas."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
)

from fleetforge.partition_profiles import MAX_PROFILE_SLOT_SIZE

_CI_OPTIONAL_FIELDS = frozenset(
    {
        "sig",
        "repo_url",
        "branch",
        "commit_sha",
        "tag",
        "build_url",
        "test_passed",
        "auto_deployed_devices",
        "sim_status",
        "sim_detail",
        "sim_ran_at",
    }
)


class ArtifactSimulateResponse(BaseModel):
    """What `POST /v1/artifact/{sha256}/simulate` returns (`R9-2`)."""

    sha256: str
    target: str | None
    sim_status: Literal["pending", "running", "passed", "failed", "skipped"]
    sim_detail: str | None
    sim_ran_at: dt.datetime


class ArtifactUploaded(BaseModel):
    """What `POST /v1/artifact` returns — the digest is the artifact's identity."""

    sha256: str
    size_bytes: int
    target: str
    version: str
    partition_layout: str | None = None
    created: bool
    has_lib_marker: bool | None = None
    artifact_type: str = "app"
    sig: str | None = None
    repo_url: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    tag: str | None = None
    build_url: str | None = None
    test_passed: bool | None = None
    auto_deployed_devices: list[str] | None = None
    sim_status: str | None = None
    sim_detail: str | None = None
    sim_ran_at: dt.datetime | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_ci_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        return {
            k: v
            for k, v in data.items()
            if (k not in _CI_OPTIONAL_FIELDS or v is not None)
            and not (k == "artifact_type" and v in (None, "app"))
        }

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: Any, handler: GetJsonSchemaHandler
    ) -> dict[str, Any]:
        if handler.mode == "serialization":
            return cls.model_json_schema(mode="validation")
        return handler(core_schema)


class ArtifactSummary(BaseModel):
    """One deployable label — a row of `GET /v1/artifact`. R1-fe-1."""

    target: str
    version: str
    sha256: str
    size_bytes: int
    partition_layout: str | None
    kind: str
    created_at: dt.datetime
    artifact_type: str = "app"
    sig: str | None = None
    repo_url: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    tag: str | None = None
    build_url: str | None = None
    test_passed: bool | None = None
    sim_status: str | None = None
    sim_detail: str | None = None
    sim_ran_at: dt.datetime | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_ci_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        return {
            k: v
            for k, v in data.items()
            if (k not in _CI_OPTIONAL_FIELDS or v is not None)
            and not (k == "artifact_type" and v in (None, "app"))
        }

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: Any, handler: GetJsonSchemaHandler
    ) -> dict[str, Any]:
        if handler.mode == "serialization":
            return cls.model_json_schema(mode="validation")
        return handler(core_schema)


class ArtifactList(BaseModel):
    """An envelope, not a bare array — the `DeviceList` rule, so a cursor can be added."""

    artifacts: list[ArtifactSummary]


# The gating warnings an operator may override, one by one (R2b-be-7). A literal, so it
# cannot reference `deploy_precheck.GATING_CODES`; `tests/test_deploy_precheck.py` keeps
# the two equal. An unknown code is a 422.
OverrideCode = Literal[
    "rollback_incapable",
    "no_library_marker",
    "unsigned_artifact",
    "dev_key_ring",
    "sim_failed",
]


class DeployRequest(BaseModel):
    """Deploy one labelled version to one device. Every field is a choice."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=64, description="the label, e.g. 1.5.0")
    apply: Literal["auto", "on_command"] = "auto"
    override: list[OverrideCode] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "gating warning codes the operator explicitly overrides; refusals are never overridable"
        ),
    )


class DeployAccepted(BaseModel):
    """What `POST /v1/devices/{device_id}/deploy` returns with 202."""

    cmd_id: str
    device_id: str
    version: str
    sha256: str
    size_bytes: int
    apply: str
    reused: bool
    device_online: bool


class ProjectDeployRequest(BaseModel):
    """`POST /v1/projects/{slug}/deploy`: deploy one version across compatible project boards."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=64, description="the label, e.g. 1.5.0")
    target_filter: Literal["all", "dev"] = "all"
    group_id: uuid.UUID | None = Field(
        default=None,
        description="Optional device_groups.id cohort filter within the project",
    )
    apply: Literal["auto", "on_command"] = "auto"
    override: list[OverrideCode] = Field(default_factory=list, max_length=8)
    canary: bool = Field(
        default=True,
        description="Stage onto 1 canary board first until a board in the cohort confirms the version",
    )
    max_parallel: int = Field(
        default=5,
        ge=1,
        le=100,
        description="Maximum concurrent in-flight OTA downloads per wave after canary confirmation",
    )


class ProjectDeployAccepted(BaseModel):
    """What `POST /v1/projects/{slug}/deploy` returns with 202."""

    project: str
    version: str
    target_filter: str
    deployed: list[DeployAccepted]
    skipped: dict[str, str] = Field(default_factory=dict)


class CommandAccepted(BaseModel):
    """What the four verb routes return with 202 (R5-be-2)."""

    command_id: str
    device_id: str
    type: str
    cmd_id: str | None
    device_online: bool


class ProjectDeployCancelRequest(BaseModel):
    """`POST /v1/projects/{slug}/deploy/cancel`: cancel in-flight pre-apply deploys in a cohort."""

    model_config = ConfigDict(extra="forbid")

    version: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Optional version filter; when omitted, cancels any in-flight pre-apply deploy",
    )
    target_filter: Literal["all", "dev"] = "all"
    group_id: uuid.UUID | None = Field(
        default=None,
        description="Optional device_groups.id cohort filter within the project",
    )


class ProjectDeployCancelAccepted(BaseModel):
    """What `POST /v1/projects/{slug}/deploy/cancel` returns with 202."""

    project: str
    cancelled: list[CommandAccepted]
    skipped: dict[str, str] = Field(default_factory=dict)


class DeployKpisSummary(BaseModel):
    """`GET /v1/devices/kpis` (R6-BE-1): Delivery success vs. fleet safety over `deploy_events`."""

    total_completed: int
    confirmed: int
    rolled_back: int
    failed: int
    cancelled: int
    in_flight: int
    delivery_success_pct: float | None
    fleet_safety_pct: float | None
    safety_gap_pct: float | None
    delivery_target_pct: float
    safety_target_pct: float
    delivery_target_met: bool | None
    safety_target_met: bool | None


class PrecheckFinding(BaseModel):
    code: str
    message: str
    needs_override: bool = False


class DeployPrecheck(BaseModel):
    """What `POST /v1/devices/{device_id}/deploy/precheck` returns: a dry run of a deploy."""

    device_id: str
    target: str
    version: str
    from_version: str | None
    sha256: str | None
    size_bytes: int | None
    artifact_partition_layout: str | None
    device_partition_layout: str | None
    device_partition_profile: str | None = None
    ota_slot_size: int | None
    power_class: str
    expected_wake_interval_s: int | None
    device_online: bool
    confirm_timeout_s: int
    deployable: bool
    refusals: list[PrecheckFinding]
    warnings: list[PrecheckFinding]


PROFILE_LAYOUT_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,31}$"
PROFILE_SHA256_PATTERN = r"^[0-9a-f]{64}$"
RESERVED_LAYOUT_ID = "unknown"
ProfileOrigin = Literal["builtin", "user", "detected"]


def _not_reserved(value: str) -> str:
    if value == RESERVED_LAYOUT_ID:
        raise ValueError(
            "unknown is reserved: it is what a board announces when its map has no name"
        )
    return value


class PartitionProfileSummary(BaseModel):
    """One flash map the server knows, as `GET /v1/partition-profiles` lists it."""

    partition_table_sha256: str
    layout_id: str | None
    origin: ProfileOrigin
    deployable: bool
    ota_slot_size: int | None
    flash_chip_size: int | None
    detected_device_id: str | None
    device_ids: list[str]
    created_at: dt.datetime
    adopted_at: dt.datetime | None


class PartitionProfileList(BaseModel):
    """An envelope, not a bare array — the `DeviceList` rule."""

    profiles: list[PartitionProfileSummary]


class PartitionProfileCreate(BaseModel):
    """`POST /v1/partition-profiles`: an operator profile, named and deployable at once."""

    model_config = ConfigDict(extra="forbid")

    layout_id: str = Field(pattern=PROFILE_LAYOUT_ID_PATTERN)
    partition_table_sha256: str = Field(pattern=PROFILE_SHA256_PATTERN)
    ota_slot_size: int = Field(gt=0, le=MAX_PROFILE_SLOT_SIZE)

    @field_validator("layout_id")
    @classmethod
    def _layout_id_not_reserved(cls, value: str) -> str:
        return _not_reserved(value)


class PartitionProfileAdopt(BaseModel):
    """`PATCH /v1/partition-profiles/{sha}`: name a detected profile, which adopts it."""

    model_config = ConfigDict(extra="forbid")

    layout_id: str = Field(pattern=PROFILE_LAYOUT_ID_PATTERN)
    ota_slot_size: int | None = Field(default=None, gt=0, le=MAX_PROFILE_SLOT_SIZE)

    @field_validator("layout_id")
    @classmethod
    def _layout_id_not_reserved(cls, value: str) -> str:
        return _not_reserved(value)
