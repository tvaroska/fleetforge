"""Content-addressed artifact, version label, delta patch, and build pipeline ORM models (`A-11`)."""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from fleetforge.db.base import Base
from fleetforge.db.models_core import _DEFAULT_PROJECT_UUID_SQL, TimestampTZ


class ArtifactKind(StrEnum):
    """Advisory classification of stored blobs (`artifacts.kind` is plain `TEXT`)."""

    AGENT_APP = "agent_app"
    AGENT_BOOTLOADER = "agent_bootloader"
    AGENT_PARTITION_TABLE = "agent_partition_table"
    AGENT_OTA_DATA = "agent_ota_data"
    USER_FIRMWARE = "user_firmware"


class ArtifactType(StrEnum):
    """Payload cargo type carried in `stage.artifact.type` (`spec/device-protocol.md`)."""

    APP = "app"
    MCU_FW = "mcu_fw"
    MODEL_WEIGHTS = "model_weights"
    FPGA_BITSTREAM = "fpga_bitstream"
    DATA_PARTITION = "data_partition"


class Artifact(Base):
    """One row per content digest — the metadata the bytes cannot carry (`S0-infra-4`)."""

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


class ArtifactDelta(Base):
    """Content-addressed `ffdelta-v1` binary patch between two firmware digests (`V3-2`)."""

    __tablename__ = "artifact_deltas"

    from_sha256: Mapped[str] = mapped_column(
        Text,
        ForeignKey("artifacts.sha256", ondelete="CASCADE"),
        primary_key=True,
    )
    to_sha256: Mapped[str] = mapped_column(
        Text,
        ForeignKey("artifacts.sha256", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    patch_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    patch_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    format: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ffdelta-v1'"))
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


class BuildJob(Base):
    """One queued or completed server-side firmware build job (`R10-1`, `R10-2`)."""

    __tablename__ = "build_jobs"
    __table_args__ = (
        Index("ix_build_jobs_project_created_at", "project_id", text("created_at DESC")),
        Index("ix_build_jobs_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    repo_url: Mapped[str] = mapped_column(Text, nullable=False)
    git_ref: Mapped[str] = mapped_column(Text, nullable=False)
    commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    partition_layout: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'ab-4m-v1'")
    )
    idf_image: Mapped[str] = mapped_column(Text, nullable=False)
    cache_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    artifact_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_text: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
