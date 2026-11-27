"""Core identity, multi-project RBAC, admin token, and encrypted project secret ORM models (`A-11`)."""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from fleetforge.db.base import Base

TimestampTZ = TIMESTAMP(timezone=True)

DEFAULT_PROJECT_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
DEFAULT_PROJECT_SLUG = "default"
_DEFAULT_PROJECT_UUID_SQL = text("'00000000-0000-4000-8000-000000000001'::uuid")


class ProjectRole(StrEnum):
    """Per-project RBAC roles (`docs/features/projects-and-users.md`). Enforced by a DB CHECK."""

    OWNER = "owner"
    OPERATOR = "operator"
    VIEWER = "viewer"


class Project(Base):
    """A logical fleet boundary — isolates boards, groups, enrollment tokens, and version labels."""

    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{0,31}$'", name="slug_format"),
        UniqueConstraint("slug"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    repo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    track_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'off'"))
    track_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_deploy_target: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'off'")
    )
    ci_webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    ci_webhook_header: Mapped[str | None] = mapped_column(Text, nullable=True)
    secrets_rev: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    otel_endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    otel_headers: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()"), onupdate=text("now()")
    )


class User(Base):
    """A human operator account (`docs/features/projects-and-users.md`)."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("username ~ '^[a-z0-9][a-z0-9._-]{1,31}$'", name="username_format"),
        CheckConstraint(
            "email IS NULL OR email ~ '^[a-z0-9._%+-]+@[a-z0-9.-]+\\.[a-z]{2,}$'",
            name="email_format",
        ),
        CheckConstraint(
            "firebase_uid IS NULL OR length(firebase_uid) BETWEEN 1 AND 128",
            name="firebase_uid_nonempty",
        ),
        UniqueConstraint("username"),
        UniqueConstraint("email"),
        UniqueConstraint("firebase_uid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    username: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    firebase_uid: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_superadmin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    disabled_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()"), onupdate=text("now()")
    )


class ProjectMember(Base):
    """Membership and role of a `User` in a `Project`."""

    __tablename__ = "project_members"
    __table_args__ = (CheckConstraint("role IN ('owner', 'operator', 'viewer')", name="role"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )


class AdminToken(Base):
    """An operator credential for the control plane (`ffa_{uuid-hex}.{secret-b64url}`). **CRITICAL.**"""

    __tablename__ = "admin_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'admin'"))
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{admin}'::text[]")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    last_used_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)


class ProjectSecret(Base):
    """Encrypted key-value secret scoped to a `Project` (`SEC-be-1` / CUJ-2). **CRITICAL.**"""

    __tablename__ = "project_secrets"
    __table_args__ = (CheckConstraint("key ~ '^[a-zA-Z0-9_.-]{1,15}$'", name="secret_key_format"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()"), onupdate=text("now()")
    )
