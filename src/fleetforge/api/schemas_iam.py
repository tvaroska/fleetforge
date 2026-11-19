"""Identity, authentication, project, group, user, and enrollment-token API schemas."""

import datetime as dt
import re
import uuid
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fleetforge.auth.enrollment import EnrollmentTokenStatus
from fleetforge.db.models import ProjectRole
from fleetforge.url_safety import validate_public_https_url

USERNAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,31}$"
EMAIL_PATTERN = r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$"
LOGIN_IDENTIFIER_PATTERN = (
    r"^([a-z0-9][a-z0-9._-]{1,31}|[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})$"
)
PROJECT_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,31}$"
AllowedScope = Literal["admin", "read", "write:artifacts", "deploy", "enroll"]


class LoginRequest(BaseModel):
    """The operator username/email and password. Password is never logged, never echoed."""

    username: str = Field(default="admin", pattern=LOGIN_IDENTIFIER_PATTERN)
    password: str = Field(min_length=1, max_length=1024)


class FirebaseLoginRequest(BaseModel):
    """Exchange a verified Firebase ID token for an HttpOnly `ff_session` cookie."""

    model_config = ConfigDict(extra="forbid")

    id_token: str = Field(min_length=16, max_length=8192)


class PasswordResetRequest(BaseModel):
    """Redeem a Firebase `PASSWORD_RESET` `oobCode`, set a new password, and sign in."""

    model_config = ConfigDict(extra="forbid")

    oob_code: str = Field(min_length=8, max_length=2048)
    new_password: str = Field(min_length=8, max_length=1024)


class LoginResponse(BaseModel):
    """What login returns — deliberately **not** the token."""

    expires_at: dt.datetime


class UserProjectSummary(BaseModel):
    """A project visible to the caller, as returned on `GET /v1/auth/me`."""

    id: uuid.UUID
    slug: str
    name: str
    description: str | None = None
    role: str


class MeResponse(BaseModel):
    """The identity behind the presented credential."""

    token_id: uuid.UUID
    subject: str
    scopes: list[str]
    expires_at: dt.datetime | None
    is_superadmin: bool = False
    project_id: uuid.UUID | None = None
    projects: list[UserProjectSummary] = Field(default_factory=list)
    user_id: uuid.UUID | None = None
    email: str | None = None
    display_name: str | None = None
    has_local_password: bool = False


class SelfProfileUpdate(BaseModel):
    """`PATCH /v1/auth/me`: update the signed-in human user's profile or password."""

    model_config = ConfigDict(extra="forbid")

    email: str | None = Field(default=None, max_length=254)
    display_name: str | None = Field(default=None, max_length=64)
    current_password: str | None = Field(default=None, min_length=1, max_length=1024)
    password: str | None = Field(default=None, min_length=8, max_length=1024)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        low = v.strip().lower()
        if not low:
            return None
        if not re.match(EMAIL_PATTERN, low):
            raise ValueError("email must be a valid email address")
        return low

    @field_validator("display_name")
    @classmethod
    def _normalize_display_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


def _default_scopes() -> list[AllowedScope]:
    return ["admin"]


class AdminTokenCreate(BaseModel):
    """`POST /v1/auth/tokens`: mint a named `ffa_...` token (optionally pinned to a project)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    project: str | None = Field(default=None, pattern=PROJECT_SLUG_PATTERN)
    scopes: list[AllowedScope] = Field(default_factory=_default_scopes, min_length=1, max_length=8)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class AdminTokenIssued(BaseModel):
    """The issuance response — the **only** place the plaintext `token` ever exists."""

    id: uuid.UUID
    token: str
    name: str
    subject: str
    scopes: list[str]
    project_id: uuid.UUID | None = None
    project_slug: str | None = None
    created_at: dt.datetime
    expires_at: dt.datetime | None


class AdminTokenSummary(BaseModel):
    """One row of `GET /v1/auth/tokens` — never contains `token` or `secret_hash`."""

    id: uuid.UUID
    name: str
    subject: str
    scopes: list[str]
    project_id: uuid.UUID | None = None
    project_slug: str | None = None
    created_at: dt.datetime
    last_used_at: dt.datetime | None
    expires_at: dt.datetime | None
    revoked_at: dt.datetime | None


class AdminTokenList(BaseModel):
    tokens: list[AdminTokenSummary]


TrackMode = Literal["off", "branch", "tag", "commit"]
AutoDeployTarget = Literal["off", "dev", "all"]
SECRET_KEY_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,14}$"  # noqa: S105 — regex pattern, not a secret


def _normalize_outbound_url(v: str | None, field_name: str) -> str | None:
    if v is None:
        return None
    stripped = v.strip()
    if not stripped:
        return None
    return validate_public_https_url(stripped, field_name=field_name)


class ProjectCreate(BaseModel):
    """`POST /v1/projects`: create a new project."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=PROJECT_SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=256)
    repo_url: str | None = Field(default=None, max_length=512)
    track_mode: TrackMode = "off"
    track_ref: str | None = Field(default=None, max_length=128)
    auto_deploy_target: AutoDeployTarget = "off"
    ci_webhook_url: str | None = Field(default=None, max_length=512)
    ci_webhook_header: str | None = Field(default=None, max_length=512)
    otel_endpoint: str | None = Field(default=None, max_length=512)
    otel_headers: str | None = Field(default=None, max_length=2048)

    @field_validator("ci_webhook_url")
    @classmethod
    def _validate_webhook_url(cls, v: str | None) -> str | None:
        return _normalize_outbound_url(v, "ci_webhook_url")

    @field_validator("otel_endpoint")
    @classmethod
    def _validate_otel_endpoint(cls, v: str | None) -> str | None:
        return _normalize_outbound_url(v, "otel_endpoint")


class ProjectUpdate(BaseModel):
    """`PATCH /v1/projects/{slug}`: update display name, description, CD, or OTel settings."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=256)
    repo_url: str | None = Field(default=None, max_length=512)
    track_mode: TrackMode | None = None
    track_ref: str | None = Field(default=None, max_length=128)
    auto_deploy_target: AutoDeployTarget | None = None
    ci_webhook_url: str | None = Field(default=None, max_length=512)
    ci_webhook_header: str | None = Field(default=None, max_length=512)
    otel_endpoint: str | None = Field(default=None, max_length=512)
    otel_headers: str | None = Field(default=None, max_length=2048)

    @field_validator("ci_webhook_url")
    @classmethod
    def _validate_webhook_url(cls, v: str | None) -> str | None:
        return _normalize_outbound_url(v, "ci_webhook_url")

    @field_validator("otel_endpoint")
    @classmethod
    def _validate_otel_endpoint(cls, v: str | None) -> str | None:
        return _normalize_outbound_url(v, "otel_endpoint")


class ProjectSummary(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    role: str = "owner"
    repo_url: str | None = None
    track_mode: str = "off"
    track_ref: str | None = None
    auto_deploy_target: str = "off"
    ci_webhook_url: str | None = None
    has_ci_webhook_header: bool = False
    secrets_rev: int = 0
    otel_endpoint: str | None = None
    has_otel_headers: bool = False
    auto_deployed_devices: list[str] | None = None
    ci_webhook_triggered: bool | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class ProjectSecretUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=512)


class ProjectSecretSummary(BaseModel):
    key: str
    updated_at: dt.datetime


class ProjectSecretList(BaseModel):
    secrets_rev: int
    secrets: list[ProjectSecretSummary]


class DeviceSecretsResponse(BaseModel):
    secrets_rev: int
    secrets: dict[str, str]


class ProjectList(BaseModel):
    projects: list[ProjectSummary]


class ProjectMemberUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: ProjectRole


class ProjectMemberSummary(BaseModel):
    user_id: uuid.UUID
    username: str
    display_name: str | None
    email: str | None = None
    is_superadmin: bool = False
    role: str
    created_at: dt.datetime


class ProjectMemberList(BaseModel):
    members: list[ProjectMemberSummary]


GROUP_NAME_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9 _.-]{0,63}$"


class DeviceGroupCreate(BaseModel):
    """`POST /v1/groups`: create a named device group within a project."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=GROUP_NAME_PATTERN)
    description: str | None = Field(default=None, max_length=256)


class DeviceGroupUpdate(BaseModel):
    """`PATCH /v1/groups/{group_id}`: rename or update description of a device group."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, pattern=GROUP_NAME_PATTERN)
    description: str | None = Field(default=None, max_length=256)


class DeviceGroupSummary(BaseModel):
    """One row of `GET /v1/groups`."""

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class DeviceGroupList(BaseModel):
    groups: list[DeviceGroupSummary]


class UserProjectRole(BaseModel):
    project_slug: str
    role: str


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(pattern=USERNAME_PATTERN)
    email: str | None = Field(default=None, max_length=254)
    display_name: str | None = Field(default=None, max_length=64)
    password: str | None = Field(default=None, min_length=8, max_length=1024)
    is_superadmin: bool = False
    send_invite_email: bool = False
    project: str | None = Field(default=None, pattern=PROJECT_SLUG_PATTERN)
    role: ProjectRole | None = None

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        low = v.strip().lower()
        if not low:
            return None
        if not re.match(EMAIL_PATTERN, low):
            raise ValueError("email must be a valid email address")
        return low

    @field_validator("display_name")
    @classmethod
    def _normalize_display_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @model_validator(mode="after")
    def _require_email_or_password(self) -> Self:
        if self.email is None and self.password is None:
            raise ValueError("either email (for Firebase invite) or password must be provided")
        return self


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str | None = Field(default=None, max_length=254)
    display_name: str | None = Field(default=None, max_length=64)
    password: str | None = Field(default=None, min_length=8, max_length=1024)
    is_superadmin: bool | None = None
    disabled: bool | None = None
    send_password_reset: bool = False

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        low = v.strip().lower()
        if not low:
            return None
        if not re.match(EMAIL_PATTERN, low):
            raise ValueError("email must be a valid email address")
        return low

    @field_validator("display_name")
    @classmethod
    def _normalize_display_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


EmailDelivery = Literal["sent", "failed", "not_configured"]


class UserSummary(BaseModel):
    id: uuid.UUID
    username: str
    email: str | None = None
    firebase_uid: str | None = None
    has_local_password: bool = False
    display_name: str | None
    is_superadmin: bool
    disabled_at: dt.datetime | None
    created_at: dt.datetime
    email_delivery: EmailDelivery | None = None
    projects: list[UserProjectRole] = Field(default_factory=list)


class UserList(BaseModel):
    users: list[UserSummary]


class EnrollmentTokenCreate(BaseModel):
    """What an operator may choose when issuing a token."""

    group_id: uuid.UUID | None = None


class EnrollmentTokenIssued(BaseModel):
    """The issuance response — the **only** place the plaintext ever exists."""

    id: uuid.UUID
    token: str
    group_id: uuid.UUID | None
    expires_at: dt.datetime
    created_at: dt.datetime


class EnrollmentTokenSummary(BaseModel):
    """One row of the issuance history (`token` and `secret_hash` never included)."""

    id: uuid.UUID
    group_id: uuid.UUID | None
    status: EnrollmentTokenStatus
    created_at: dt.datetime
    expires_at: dt.datetime
    used_at: dt.datetime | None
    used_by_device_id: str | None
    revoked_at: dt.datetime | None


class EnrollmentTokenList(BaseModel):
    """An envelope, not a bare array, so a cursor can be added without a break."""

    tokens: list[EnrollmentTokenSummary]
