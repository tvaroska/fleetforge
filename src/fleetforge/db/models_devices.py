"""Device registry, enrollment, partition profile, telemetry, command, and relay ORM models (`A-11`)."""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any, TypeAlias

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Double,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from fleetforge.db.base import Base
from fleetforge.db.models_core import _DEFAULT_PROJECT_UUID_SQL, TimestampTZ


class PowerClass(StrEnum):
    """How a device is expected to be reachable (`always_on` or `sleepy`). Enforced by a DB CHECK."""

    ALWAYS_ON = "always_on"
    SLEEPY = "sleepy"


class LinkType(StrEnum):
    """Advisory known link types — column is plain `TEXT` so future agents never fail ingest."""

    WIFI = "wifi"
    ETHERNET = "ethernet"
    CELLULAR = "cellular"
    THREAD = "thread"


class CommandType(StrEnum):
    """Server-authored verbs sent besides `stage` (`R5-spec-1` Patch A2)."""

    APPLY = "apply"
    CANCEL = "cancel"
    ROLLBACK = "rollback"
    REBOOT = "reboot"


class AckResult(StrEnum):
    """`up/ack.result` advisory vocabulary (`R5-spec-1` Patch A5)."""

    ACCEPTED = "accepted"
    DEFERRED = "deferred"
    REFUSED = "refused"


class ProgressStage(StrEnum):
    """Boot/enrollment stages an agent reports over HTTPS (`S0-fw-1`, advisory `TEXT`)."""

    LINK_UP = "link_up"
    TIME_SYNCED = "time_synced"
    ENROLLING = "enrolling"
    ENROLLED = "enrolled"
    MQTT_CONNECTED = "mqtt_connected"
    MQTT_REFUSED = "mqtt_refused"
    HALTED = "halted"
    BROWNOUT = "brownout"


class DeviceGroup(Base):
    """A named set of devices within a project (`device_groups`)."""

    __tablename__ = "device_groups"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        server_default=_DEFAULT_PROJECT_UUID_SQL,
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()"), onupdate=text("now()")
    )


class Device(Base):
    """An enrolled board keyed by lowercase 12-hex eFuse MAC (`device_id`, also the MQTT username)."""

    __tablename__ = "devices"
    __table_args__ = (
        CheckConstraint("device_id ~ '^[0-9a-f]{12}$'", name="device_id_format"),
        CheckConstraint("power_class IN ('always_on', 'sleepy')", name="power_class"),
        CheckConstraint(
            "power_class <> 'sleepy' "
            "OR (expected_wake_interval_s IS NOT NULL AND expected_wake_interval_s > 0)",
            name="sleepy_wake_interval",
        ),
    )

    device_id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
        server_default=_DEFAULT_PROJECT_UUID_SQL,
        index=True,
    )
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform_type: Mapped[str] = mapped_column(Text, nullable=False)
    proto: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    fw_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    link_type: Mapped[str] = mapped_column(Text, nullable=False)
    ssid: Mapped[str | None] = mapped_column(Text, nullable=True)
    known_networks: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    power_class: Mapped[str] = mapped_column(Text, nullable=False)
    expected_wake_interval_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parent_device_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("devices.device_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    partition_layout: Mapped[str | None] = mapped_column(Text, nullable=True)
    ota_slot_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    flash_chip_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    partition_table_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    rollback_capable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    last_reset_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    health_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    uptime_s: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rssi: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    free_heap: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    min_free_heap: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    boot_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_dev: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    stay_awake: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    secrets_rev: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sub_versions: Mapped[dict[str, str] | None] = mapped_column(JSONB, nullable=True)
    safe_mode: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    mqtt_password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("device_groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    last_seen: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True, index=True)
    presence_reported: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    broker_provisioned_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    enrolled_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    decommissioned_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()"), onupdate=text("now()")
    )


class EnrollmentToken(Base):
    """Short-lived, group-scoped, single-use enrollment credential (`ffe_{uuid}.{secret}`). **CRITICAL.**

    Atomic single-statement burn lives in `fleetforge.auth.enrollment.BURN_SQL`; never implement
    token consumption as SELECT-then-check-then-write.
    """

    __tablename__ = "enrollment_tokens"
    __table_args__ = (
        CheckConstraint(
            "used_by_device_id IS NULL OR used_at IS NOT NULL",
            name="used_consistency",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        server_default=_DEFAULT_PROJECT_UUID_SQL,
        index=True,
    )
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("device_groups.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[dt.datetime] = mapped_column(TimestampTZ, nullable=False, index=True)
    used_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    used_by_device_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("devices.device_id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)


class DeviceProgress(Base):
    """Append-only boot/enrollment stage reports before a board reaches MQTT (`S0-fw-1`)."""

    __tablename__ = "device_progress"
    __table_args__ = (Index("ix_device_progress_device_id_at", "device_id", text("at DESC")),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    device_id: Mapped[str] = mapped_column(Text, nullable=False)
    token_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


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


class DeviceFault(Base):
    """One boot that ended in a fault reset: `panic`, `task_wdt`, or `brownout` (`R4-be-1`)."""

    __tablename__ = "device_faults"
    __table_args__ = (Index("ix_device_faults_device_id_at", "device_id", text("at DESC")),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        Text, ForeignKey("devices.device_id", ondelete="CASCADE"), nullable=False
    )
    at: Mapped[dt.datetime] = mapped_column(TimestampTZ, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class DeviceLog(Base):
    """Firmware log lines from `up/log` (`R4-be-1`)."""

    __tablename__ = "device_logs"
    __table_args__ = (
        CheckConstraint("dropped >= 0", name="dropped_non_negative"),
        Index(
            "ix_device_logs_device_id_logged_at",
            "device_id",
            text("logged_at DESC"),
            text("id DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        Text, ForeignKey("devices.device_id", ondelete="CASCADE"), nullable=False
    )
    logged_at: Mapped[dt.datetime] = mapped_column(TimestampTZ, nullable=False)
    received_at: Mapped[dt.datetime] = mapped_column(TimestampTZ, nullable=False)
    level: Mapped[str] = mapped_column(Text, nullable=False)
    dropped: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Declared last because the attribute shadows `sqlalchemy.text` inside this class body.
    text: Mapped[str | None] = mapped_column(Text, nullable=True)


class DeviceTelemetry(Base):
    """`up/telemetry` samples (`R4-be-1`)."""

    __tablename__ = "device_telemetry"
    __table_args__ = (
        Index(
            "ix_device_telemetry_device_id_name_sample_at",
            "device_id",
            "name",
            text("sample_at DESC"),
        ),
        Index("ix_device_telemetry_sample_at", "sample_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        Text, ForeignKey("devices.device_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[float] = mapped_column(Double, nullable=False)
    sample_at: Mapped[dt.datetime] = mapped_column(TimestampTZ, nullable=False)


class DeviceMetric(Base):
    """Latest value of each metric of each device (`R4-be-1`)."""

    __tablename__ = "device_metrics"

    device_id: Mapped[str] = mapped_column(
        Text, ForeignKey("devices.device_id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[float] = mapped_column(Double, nullable=False)
    sample_at: Mapped[dt.datetime] = mapped_column(TimestampTZ, nullable=False)


class _DeviceCommandRow(Base):
    """One verb the server published to a board and the board's first `up/ack` answer (`R5-be-2`)."""

    __tablename__ = "device_commands"
    __table_args__ = (
        Index("ix_device_commands_device_id_sent_at", "device_id", text("sent_at DESC")),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    device_id: Mapped[str] = mapped_column(
        Text, ForeignKey("devices.device_id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    cmd_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
    sent_by: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    publish_failed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    answered_at: Mapped[dt.datetime | None] = mapped_column(TimestampTZ, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_cmd_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_detail: Mapped[str | None] = mapped_column(Text, nullable=True)


_DeviceCommandRow.__name__ = "DeviceCommand"
DeviceCommand: TypeAlias = _DeviceCommandRow  # noqa: UP040


class GatewayRelayState(Base):
    """Edge artifact cache state for a parent UGV gateway device (`V3-1`)."""

    __tablename__ = "gateway_relays"

    gateway_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("devices.device_id", ondelete="CASCADE"),
        primary_key=True,
    )
    relay_base_url: Mapped[str] = mapped_column(Text, nullable=False)
    cached_artifacts: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, server_default=text("now()")
    )
