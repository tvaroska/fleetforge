"""Content-addressed artifact, version label, build cache, and partition profile ORM models.

Extracted from `fleetforge.db.models` (`A-11`) and re-exported there so `Base.metadata`
and existing imports remain unchanged while keeping each schema module well below 800 lines.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from fleetforge.db.base import Base

TimestampTZ = TIMESTAMP(timezone=True)
_DEFAULT_PROJECT_UUID_SQL = text("'00000000-0000-4000-8000-000000000001'::uuid")


class Artifact(Base):
    """One row per content digest — the metadata the bytes cannot carry. S0-infra-4."""

    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        Index("ix_artifacts_kind_target", "kind", "target"),
        Index("ix_artifacts_sim_status", "sim_status"),
    )

    sha256: Mapped[str] = mapped_column(Text, primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'app'"))
    target: Mapped[str | None] = mapped_column(Text, nullable=True)
    partition_layout: Mapped[str | None] = mapped_column(Text, nullable=True)
    has_lib_marker: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sig: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    sim_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    sim_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    sim_runner: Mapped[str | None] = mapped_column(Text, nullable=True)
    sim_ran_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    sim_lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    sim_lease_expires_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )


class ArtifactVersion(Base):
    """A human version label pointing at a digest — `(project_id, target, version) -> sha256`."""

    __tablename__ = "artifact_versions"
    __table_args__ = (
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
        Index(
            "ix_artifact_versions_project_target_created_at",
            "project_id",
            "target",
            "created_at",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("projects.id", ondelete="RESTRICT"),
        primary_key=True,
        server_default=_DEFAULT_PROJECT_UUID_SQL,
    )
    target: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    sha256: Mapped[str] = mapped_column(
        Text, ForeignKey("artifacts.sha256", ondelete="RESTRICT"), nullable=False
    )
    repo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    branch: Mapped[str | None] = mapped_column(Text, nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    tag: Mapped[str | None] = mapped_column(Text, nullable=True)
    build_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    test_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )


class Build(Base):
    """A build-cache entry: cache key -> the artifacts that build produced (`S0-infra-4`, `R10`)."""

    __tablename__ = "builds"
    __table_args__ = (
        CheckConstraint("cache_key ~ '^[0-9a-f]{64}$'", name="cache_key_format"),
        CheckConstraint("jsonb_typeof(key_inputs) = 'object'", name="key_inputs_object"),
        CheckConstraint("jsonb_typeof(outputs) = 'object'", name="outputs_object"),
        Index("ix_builds_target_partition_layout", "target", "partition_layout"),
    )

    cache_key: Mapped[str] = mapped_column(Text, primary_key=True)
    key_inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    outputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    target: Mapped[str | None] = mapped_column(Text, nullable=True)
    partition_layout: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )


class PartitionProfile(Base):
    """A flash map the server knows: its fingerprint, its name, its OTA slot (`R3-be-2`)."""

    __tablename__ = "partition_profiles"
    __table_args__ = (
        CheckConstraint("partition_table_sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
        CheckConstraint("origin IN ('builtin', 'user', 'detected')", name="origin"),
        CheckConstraint(
            "layout_id IS NULL OR "
            "(layout_id ~ '^[a-z0-9][a-z0-9-]{0,31}$' AND layout_id <> 'unknown')",
            name="layout_id_format",
        ),
        CheckConstraint("(layout_id IS NULL) = (adopted_at IS NULL)", name="adopted_is_named"),
        CheckConstraint(
            "origin = 'detected' OR adopted_at IS NOT NULL", name="only_detected_pending"
        ),
        CheckConstraint("adopted_at IS NULL OR ota_slot_size IS NOT NULL", name="adopted_has_slot"),
        CheckConstraint("ota_slot_size IS NULL OR ota_slot_size > 0", name="slot_positive"),
        UniqueConstraint("layout_id"),
    )

    partition_table_sha256: Mapped[str] = mapped_column(Text, primary_key=True)
    layout_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    ota_slot_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    flash_chip_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    detected_device_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    adopted_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
