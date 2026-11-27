"""Deploy event history, project rollout intent, and cohort deploy policy ORM models (`A-11`)."""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from fleetforge.db.base import Base
from fleetforge.db.models_core import TimestampTZ


class DeployState(StrEnum):
    """The `up/status` state machine from `spec/device-protocol.md`, plus server-authored `REQUESTED`."""

    REQUESTED = "requested"
    IDLE = "idle"
    STAGING = "staging"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    STAGED = "staged"
    AWAITING_SAFE_WINDOW = "awaiting_safe_window"
    APPLYING = "applying"
    REBOOTING = "rebooting"
    CONFIRMING = "confirming"
    CONFIRMED = "confirmed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_DEPLOY_STATES = frozenset(
    {DeployState.CONFIRMED, DeployState.ROLLED_BACK, DeployState.FAILED, DeployState.CANCELLED}
)


class DeployEvent(Base):
    """Append-only log of observed deploy state transitions — the KPI source (`deploys.py` is sole writer)."""

    __tablename__ = "deploy_events"
    __table_args__ = (
        Index("ix_deploy_events_device_id_at", "device_id", text("at DESC")),
        Index("ix_deploy_events_cmd_id", "cmd_id"),
        Index(
            "ix_deploy_events_terminal",
            "device_id",
            text("at DESC"),
            postgresql_where=text("is_terminal"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    device_id: Mapped[str] = mapped_column(
        Text, ForeignKey("devices.device_id", ondelete="RESTRICT"), nullable=False
    )
    cmd_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    is_terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    from_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class ProjectRollout(Base):
    """Persisted cohort rollout intent closed on completion, halt, supersession, or cancel (`A-21`)."""

    __tablename__ = "project_rollouts"
    __table_args__ = (
        Index(
            "ix_project_rollouts_project_status_updated",
            "project_id",
            "status",
            text("updated_at DESC"),
        ),
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
    version: Mapped[str] = mapped_column(Text, nullable=False)
    target_filter: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'all'"))
    target_chip: Mapped[str | None] = mapped_column(Text, nullable=True)
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("device_groups.id", ondelete="SET NULL"),
        nullable=True,
    )
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("deploy_policies.id", ondelete="SET NULL"),
        nullable=True,
    )
    apply: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'auto'"))
    canary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    max_parallel: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    override: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    sent_by: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    close_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    closed_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)


class DeployPolicy(Base):
    """Multi-ring cohort deploy policy (`R11-1`)."""

    __tablename__ = "deploy_policies"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    match_mode: Mapped[str] = mapped_column(Text, nullable=False)
    match_ref: Mapped[str] = mapped_column(Text, nullable=False)
    ring_group_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), nullable=False)
    max_parallel: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    require_sim_passed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    min_soak_s: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    min_free_heap_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )


class DeployPolicyRun(Base):
    """Persisted per-`(policy_id, version)` execution, halt memory, and soak anchors (`A-22`)."""

    __tablename__ = "deploy_policy_runs"

    policy_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("deploy_policies.id", ondelete="CASCADE"),
        primary_key=True,
    )
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    halted_ring_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    halt_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    halted_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    ring_completed_at: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    resumed_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    resumed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
