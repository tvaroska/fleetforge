"""Request/response models for the API.

Kept separate from the routers so R0-be-2/4/5 add theirs here rather than growing a
schema section inside each router.
"""

import datetime as dt
import re
import unicodedata
import uuid
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from fleetforge.announce_fields import (
    normalize_flash_chip_size,
    normalize_known_networks,
    normalize_partition_table_sha256,
    normalize_rollback_capable,
    normalize_safe_mode,
    normalize_ssid,
    normalize_sub_versions,
)
from fleetforge.api.schemas_deploys import (
    PROFILE_LAYOUT_ID_PATTERN as PROFILE_LAYOUT_ID_PATTERN,
)
from fleetforge.api.schemas_deploys import (
    PROFILE_SHA256_PATTERN as PROFILE_SHA256_PATTERN,
)
from fleetforge.api.schemas_deploys import (
    RESERVED_LAYOUT_ID as RESERVED_LAYOUT_ID,
)
from fleetforge.api.schemas_deploys import (
    ArtifactList as ArtifactList,
)
from fleetforge.api.schemas_deploys import (
    ArtifactSimulateResponse as ArtifactSimulateResponse,
)
from fleetforge.api.schemas_deploys import (
    ArtifactSummary as ArtifactSummary,
)
from fleetforge.api.schemas_deploys import (
    ArtifactUploaded as ArtifactUploaded,
)
from fleetforge.api.schemas_deploys import (
    CommandAccepted as CommandAccepted,
)
from fleetforge.api.schemas_deploys import (
    DeployAccepted as DeployAccepted,
)
from fleetforge.api.schemas_deploys import (
    DeployKpisSummary as DeployKpisSummary,
)
from fleetforge.api.schemas_deploys import (
    DeployPrecheck as DeployPrecheck,
)
from fleetforge.api.schemas_deploys import (
    DeployRequest as DeployRequest,
)
from fleetforge.api.schemas_deploys import (
    OverrideCode as OverrideCode,
)
from fleetforge.api.schemas_deploys import (
    PartitionProfileAdopt as PartitionProfileAdopt,
)
from fleetforge.api.schemas_deploys import (
    PartitionProfileCreate as PartitionProfileCreate,
)
from fleetforge.api.schemas_deploys import (
    PartitionProfileList as PartitionProfileList,
)
from fleetforge.api.schemas_deploys import (
    PartitionProfileSummary as PartitionProfileSummary,
)
from fleetforge.api.schemas_deploys import (
    PrecheckFinding as PrecheckFinding,
)
from fleetforge.api.schemas_deploys import (
    ProfileOrigin as ProfileOrigin,
)
from fleetforge.api.schemas_deploys import (
    ProjectDeployAccepted as ProjectDeployAccepted,
)
from fleetforge.api.schemas_deploys import (
    ProjectDeployCancelAccepted as ProjectDeployCancelAccepted,
)
from fleetforge.api.schemas_deploys import (
    ProjectDeployCancelRequest as ProjectDeployCancelRequest,
)
from fleetforge.api.schemas_deploys import (
    ProjectDeployRequest as ProjectDeployRequest,
)
from fleetforge.api.schemas_iam import (
    EMAIL_PATTERN as EMAIL_PATTERN,
)
from fleetforge.api.schemas_iam import (
    GROUP_NAME_PATTERN as GROUP_NAME_PATTERN,
)
from fleetforge.api.schemas_iam import (
    LOGIN_IDENTIFIER_PATTERN as LOGIN_IDENTIFIER_PATTERN,
)
from fleetforge.api.schemas_iam import (
    PROJECT_SLUG_PATTERN as PROJECT_SLUG_PATTERN,
)
from fleetforge.api.schemas_iam import (
    SECRET_KEY_PATTERN as SECRET_KEY_PATTERN,
)
from fleetforge.api.schemas_iam import (
    USERNAME_PATTERN as USERNAME_PATTERN,
)
from fleetforge.api.schemas_iam import (
    AdminTokenCreate as AdminTokenCreate,
)
from fleetforge.api.schemas_iam import (
    AdminTokenIssued as AdminTokenIssued,
)
from fleetforge.api.schemas_iam import (
    AdminTokenList as AdminTokenList,
)
from fleetforge.api.schemas_iam import (
    AdminTokenSummary as AdminTokenSummary,
)
from fleetforge.api.schemas_iam import (
    AllowedScope as AllowedScope,
)
from fleetforge.api.schemas_iam import (
    AutoDeployTarget as AutoDeployTarget,
)
from fleetforge.api.schemas_iam import (
    DeviceGroupCreate as DeviceGroupCreate,
)
from fleetforge.api.schemas_iam import (
    DeviceGroupList as DeviceGroupList,
)
from fleetforge.api.schemas_iam import (
    DeviceGroupSummary as DeviceGroupSummary,
)
from fleetforge.api.schemas_iam import (
    DeviceGroupUpdate as DeviceGroupUpdate,
)
from fleetforge.api.schemas_iam import (
    DeviceSecretsResponse as DeviceSecretsResponse,
)
from fleetforge.api.schemas_iam import (
    EmailDelivery as EmailDelivery,
)
from fleetforge.api.schemas_iam import (
    EnrollmentTokenCreate as EnrollmentTokenCreate,
)
from fleetforge.api.schemas_iam import (
    EnrollmentTokenIssued as EnrollmentTokenIssued,
)
from fleetforge.api.schemas_iam import (
    EnrollmentTokenList as EnrollmentTokenList,
)
from fleetforge.api.schemas_iam import (
    EnrollmentTokenSummary as EnrollmentTokenSummary,
)
from fleetforge.api.schemas_iam import (
    FirebaseLoginRequest as FirebaseLoginRequest,
)
from fleetforge.api.schemas_iam import (
    LoginRequest as LoginRequest,
)
from fleetforge.api.schemas_iam import (
    LoginResponse as LoginResponse,
)
from fleetforge.api.schemas_iam import (
    MeResponse as MeResponse,
)
from fleetforge.api.schemas_iam import (
    PasswordResetRequest as PasswordResetRequest,
)
from fleetforge.api.schemas_iam import (
    ProjectCreate as ProjectCreate,
)
from fleetforge.api.schemas_iam import (
    ProjectList as ProjectList,
)
from fleetforge.api.schemas_iam import (
    ProjectMemberList as ProjectMemberList,
)
from fleetforge.api.schemas_iam import (
    ProjectMemberSummary as ProjectMemberSummary,
)
from fleetforge.api.schemas_iam import (
    ProjectMemberUpsert as ProjectMemberUpsert,
)
from fleetforge.api.schemas_iam import (
    ProjectSecretList as ProjectSecretList,
)
from fleetforge.api.schemas_iam import (
    ProjectSecretSummary as ProjectSecretSummary,
)
from fleetforge.api.schemas_iam import (
    ProjectSecretUpsert as ProjectSecretUpsert,
)
from fleetforge.api.schemas_iam import (
    ProjectSummary as ProjectSummary,
)
from fleetforge.api.schemas_iam import (
    ProjectUpdate as ProjectUpdate,
)
from fleetforge.api.schemas_iam import (
    SelfProfileUpdate as SelfProfileUpdate,
)
from fleetforge.api.schemas_iam import (
    TrackMode as TrackMode,
)
from fleetforge.api.schemas_iam import (
    UserCreate as UserCreate,
)
from fleetforge.api.schemas_iam import (
    UserList as UserList,
)
from fleetforge.api.schemas_iam import (
    UserProjectRole as UserProjectRole,
)
from fleetforge.api.schemas_iam import (
    UserProjectSummary as UserProjectSummary,
)
from fleetforge.api.schemas_iam import (
    UserSummary as UserSummary,
)
from fleetforge.api.schemas_iam import (
    UserUpdate as UserUpdate,
)
from fleetforge.api.schemas_v2 import (
    BuildJobCreate as BuildJobCreate,
)
from fleetforge.api.schemas_v2 import (
    BuildJobDetail as BuildJobDetail,
)
from fleetforge.api.schemas_v2 import (
    BuildJobList as BuildJobList,
)
from fleetforge.api.schemas_v2 import (
    BuildJobSummary as BuildJobSummary,
)
from fleetforge.auth.tokens import MAX_TOKEN_LENGTH
from fleetforge.db.models import PowerClass
from fleetforge.identity import is_valid_device_id


def _logged_device_id(info: ValidationInfo) -> str | None:
    """The body's `device_id` for a log line, only once it has passed its own check.

    `device_id` is declared before `ssid`, so a valid one is already in `info.data`; an
    invalid one is not, and the request is about to be refused for it anyway.
    """
    value = info.data.get("device_id")
    return value if isinstance(value, str) else None


class EnrollRequest(BaseModel):
    """The device-facing enrollment body. **Flat, and it stays flat forever.**"""

    model_config = ConfigDict(extra="ignore")

    token: str = Field(min_length=1, max_length=MAX_TOKEN_LENGTH)

    device_id: str
    platform_type: str = Field(min_length=1, max_length=64)
    link_type: str = Field(min_length=1, max_length=32)
    ssid: str | None = None
    known_networks: int | None = None
    power_class: str
    proto: int = 1
    fw_version: str | None = Field(default=None, max_length=64)
    agent_version: str | None = Field(default=None, max_length=64)
    expected_wake_interval_s: int | None = Field(default=None, gt=0)
    parent_device_id: str | None = None
    partition_layout: str | None = Field(default=None, max_length=64)
    ota_slot_size: int | None = Field(default=None, gt=0)
    flash_chip_size: int | None = None
    partition_table_sha256: str | None = None
    rollback_capable: bool | None = None
    capabilities: list[str] = Field(default_factory=list, max_length=32)
    sub_versions: dict[str, str] | None = None
    safe_mode: bool | None = None

    @field_validator("device_id", "parent_device_id")
    @classmethod
    def _canonical_device_id(cls, value: str | None) -> str | None:
        if value is None or is_valid_device_id(value):
            return value
        raise ValueError("device_id must be 12 lowercase hex digits (the eFuse MAC)")

    @field_validator("ssid", mode="before")
    @classmethod
    def _usable_ssid(cls, value: object, info: ValidationInfo) -> str | None:
        return normalize_ssid(value, device_id=_logged_device_id(info))

    @field_validator("known_networks", mode="before")
    @classmethod
    def _usable_known_networks(cls, value: object, info: ValidationInfo) -> int | None:
        return normalize_known_networks(value, device_id=_logged_device_id(info))

    @field_validator("flash_chip_size", mode="before")
    @classmethod
    def _usable_flash_chip_size(cls, value: object, info: ValidationInfo) -> int | None:
        return normalize_flash_chip_size(value, device_id=_logged_device_id(info))

    @field_validator("partition_table_sha256", mode="before")
    @classmethod
    def _usable_partition_table_sha256(cls, value: object, info: ValidationInfo) -> str | None:
        return normalize_partition_table_sha256(value, device_id=_logged_device_id(info))

    @field_validator("rollback_capable", mode="before")
    @classmethod
    def _usable_rollback_capable(cls, value: object, info: ValidationInfo) -> bool | None:
        return normalize_rollback_capable(value, device_id=_logged_device_id(info))

    @field_validator("sub_versions", mode="before")
    @classmethod
    def _usable_sub_versions(cls, value: object, info: ValidationInfo) -> dict[str, str] | None:
        return normalize_sub_versions(value, device_id=_logged_device_id(info))

    @field_validator("safe_mode", mode="before")
    @classmethod
    def _usable_safe_mode(cls, value: object, info: ValidationInfo) -> bool | None:
        return normalize_safe_mode(value, device_id=_logged_device_id(info))

    @field_validator("power_class")
    @classmethod
    def _known_power_class(cls, value: str) -> str:
        if value not in set(PowerClass):
            raise ValueError(f"power_class must be one of {sorted(PowerClass)}")
        return value

    @model_validator(mode="after")
    def _sleepy_needs_an_interval(self) -> Self:
        if self.power_class == PowerClass.SLEEPY and self.expected_wake_interval_s is None:
            raise ValueError("power_class=sleepy requires a positive expected_wake_interval_s")
        return self


class EnrollResponse(BaseModel):
    """Exactly the three fields `spec/device-protocol.md` step 4 promises."""

    device_id: str
    mqtt_username: str
    mqtt_password: str


NAME_MAX_LENGTH = 64


class DeviceUpdate(BaseModel):
    """`PATCH /v1/devices/{device_id}`: the operator-set facts, and nothing else."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    group_id: uuid.UUID | None = None
    project: str | None = Field(default=None, pattern=PROJECT_SLUG_PATTERN)
    is_dev: bool | None = None
    stay_awake: bool | None = None

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if len(stripped) > NAME_MAX_LENGTH:
            raise ValueError(f"a name is at most {NAME_MAX_LENGTH} characters")
        if any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in stripped):
            raise ValueError("a name cannot contain control characters")
        if is_valid_device_id(stripped.lower()):
            raise ValueError("a name cannot look like a device id (12 hex characters)")
        return stripped


class DeviceSummary(BaseModel):
    """One enrolled board, as the fleet view sees it."""

    device_id: str
    name: str | None
    group_id: uuid.UUID | None
    platform_type: str
    fw_version: str | None
    agent_version: str | None
    link_type: str
    ssid: str | None = None
    known_networks: int | None = None
    power_class: str
    expected_wake_interval_s: int | None
    parent_device_id: str | None
    partition_layout: str | None
    ota_slot_size: int | None
    flash_chip_size: int | None = None
    partition_table_sha256: str | None = None
    rollback_capable: bool | None = None
    capabilities: list[str]
    last_seen: dt.datetime | None
    enrolled_at: dt.datetime
    broker_provisioned_at: dt.datetime | None
    online: bool
    last_reset_reason: str | None = None
    fault_resets: "FaultResets | None" = None
    health: "HealthSnapshot | None" = None
    metrics: list["MetricValue"] = Field(default_factory=list)
    last_command: "CommandSummary | None" = None
    is_dev: bool = False
    stay_awake: bool = False
    secrets_rev: int | None = None
    sub_versions: dict[str, str] | None = None
    safe_mode: bool = False
    deploy: "DeploySummary | None" = None


class HealthSnapshot(BaseModel):
    """The latest live heartbeat's health (R4-be-1). Device-reported; NULL = not reported."""

    at: dt.datetime
    uptime_s: int | None
    rssi: int | None
    free_heap: int | None
    min_free_heap: int | None
    boot_ok: bool | None


class FaultResets(BaseModel):
    """Boots that ended in a fault reset inside the last `window_s` seconds (R4-be-1)."""

    window_s: int
    panic: int = 0
    task_wdt: int = 0
    brownout: int = 0


class MetricValue(BaseModel):
    """The latest value of one `up/telemetry` metric (R4-be-1). `at` = receipt - age_ms."""

    name: str
    value: float
    at: dt.datetime


class DeviceLogLine(BaseModel):
    """One stored `up/log` line (R4-be-1)."""

    id: int
    logged_at: dt.datetime
    received_at: dt.datetime
    level: str
    text: str | None
    dropped: int
    context: dict[str, str | int | float | bool] | None = None


class DeviceLogTail(BaseModel):
    """A device's newest log lines, newest first (R4-be-1)."""

    device_id: str
    lines: list[DeviceLogLine]


class TelemetryPoint(BaseModel):
    at: dt.datetime
    value: float


class TelemetrySeries(BaseModel):
    """One metric's newest samples, newest first (R4-be-1)."""

    device_id: str
    name: str
    samples: list[TelemetryPoint]


class DockSyncRequest(BaseModel):
    """`POST /v1/devices/{device_id}/dock-sync` (CUJ-3): USB-C dock offline bundle sync."""

    model_config = ConfigDict(extra="forbid")

    announce: dict[str, Any] | None = None
    statuses: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    telemetry: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    logs: list[dict[str, Any]] = Field(default_factory=list, max_length=64)


class DockSyncResponse(BaseModel):
    """Summary of what `POST /v1/devices/{device_id}/dock-sync` ingested."""

    device_id: str
    link_type: str
    fw_version: str | None
    sub_versions: dict[str, str] | None = None
    statuses_recorded: int
    telemetry_batches_recorded: int
    log_batches_recorded: int
    device: DeviceSummary


class DeployStep(BaseModel):
    """One transition of a board's current deploy transaction. R2b-fe-9."""

    state: str
    at: dt.datetime


class DeploySender(BaseModel):
    """Who opened this deploy transaction (R2b-be-4)."""

    subject: str
    credential: str | None


class DeploySelfTest(BaseModel):
    """The custom self-test's outcome (R5-be-1), parsed by the server from `detail`."""

    outcome: Literal["passed", "failed", "timed_out"]
    reason: str | None
    returned_to: str | None


class DeploySummary(BaseModel):
    """A board's newest deploy state, as the fleet view sees it. R1-fe-1."""

    cmd_id: str | None
    state: str
    at: dt.datetime
    is_terminal: bool
    artifact_version: str | None
    from_version: str | None
    pct: int | None
    detail: str | None
    steps: list[DeployStep]
    confirm_timeout_s: int
    sent_by: DeploySender | None = None
    selftest: DeploySelfTest | None = None


class CommandSummary(BaseModel):
    """The newest verb sent to a board and its first answer (R5-be-2), on `GET /v1/devices`."""

    command_id: str
    type: str
    cmd_id: str | None
    sent_at: dt.datetime
    sent_by: DeploySender | None
    result: str | None
    detail: str | None
    answered_at: dt.datetime | None


class CommandRecord(CommandSummary):
    """`GET /v1/devices/{device_id}/commands/{command_id}` (R5-be-2)."""

    device_id: str
    publish_failed: bool


class ArrivalSummary(BaseModel):
    """A board that is *arriving* — it has reported a boot stage but is not in the fleet yet."""

    device_id: str
    stage: str
    detail: str | None
    at: dt.datetime
    stalled: bool


class DeviceList(BaseModel):
    """An envelope, not a bare array, so a cursor can be added without a break."""

    devices: list[DeviceSummary]
    arrivals: list[ArrivalSummary] = Field(default_factory=list)


MAX_PROGRESS_DETAIL = 200
STAGE_PATTERN = re.compile(r"\A[a-z][a-z0-9_]{0,31}\Z")


class ProgressReport(BaseModel):
    """The body of `POST /v1/device-progress`. Flat, and it stays flat, like `EnrollRequest`."""

    model_config = ConfigDict(extra="ignore")

    token: str = Field(min_length=1, max_length=MAX_TOKEN_LENGTH)
    device_id: str
    stage: str = Field(min_length=1, max_length=32)
    detail: str | None = Field(default=None, max_length=MAX_PROGRESS_DETAIL)

    @field_validator("device_id")
    @classmethod
    def _canonical_device_id(cls, value: str) -> str:
        if is_valid_device_id(value):
            return value
        raise ValueError("device_id must be 12 lowercase hex digits (the eFuse MAC)")

    @field_validator("stage")
    @classmethod
    def _stage_shape(cls, value: str) -> str:
        if STAGE_PATTERN.match(value):
            return value
        raise ValueError("stage must match [a-z][a-z0-9_]{0,31}")

    @field_validator("detail")
    @classmethod
    def _printable_detail(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if any(ch < " " or ch == "\x7f" for ch in value):
            raise ValueError("detail must not contain control characters")
        return value
