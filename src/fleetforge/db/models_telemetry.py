"""Device health, fault, log, telemetry, metric, and command ORM models (`R4`/`R5`).

Extracted from `fleetforge.db.models` (`A-11`) and re-exported there so `Base.metadata`
and existing imports remain unchanged while keeping each schema module well below 800 lines.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Double,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from fleetforge.db.base import Base

TimestampTZ = TIMESTAMP(timezone=True)


class DeviceFault(Base):
    """One boot that ended in a fault reset: `panic`, `task_wdt` or `brownout`. R4-be-1."""

    __tablename__ = "device_faults"
    __table_args__ = (Index("ix_device_faults_device_id_at", "device_id", text("at DESC")),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        Text, ForeignKey("devices.device_id", ondelete="CASCADE"), nullable=False
    )
    at: Mapped[dt.datetime] = mapped_column(TimestampTZ, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class DeviceLog(Base):
    """Firmware log lines from `up/log`. R4-be-1."""

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
    # Declared last: the attribute shadows `sqlalchemy.text` inside this class body.
    text: Mapped[str | None] = mapped_column(Text, nullable=True)


class DeviceTelemetry(Base):
    """`up/telemetry` samples. R4-be-1."""

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
    """The latest value of each metric of each device. R4-be-1."""

    __tablename__ = "device_metrics"

    device_id: Mapped[str] = mapped_column(
        Text, ForeignKey("devices.device_id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[float] = mapped_column(Double, nullable=False)
    sample_at: Mapped[dt.datetime] = mapped_column(TimestampTZ, nullable=False)


class DeviceCommand(Base):
    """One verb the server published to a board (`apply`, `cancel`, `rollback`, `reboot`)
    and the board's first `up/ack` answer to it (`R5-be-2`)."""

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
