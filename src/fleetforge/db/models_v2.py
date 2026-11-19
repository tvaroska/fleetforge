"""V2 & V3 ORM models (`R10` build jobs, `R11` deploy policies, `V3` delta OTA).

Kept in a dedicated module (`A-11`) and re-exported from `fleetforge.db.models` so
`Base.metadata` and `alembic check` see every table while keeping every schema file
well below 800 non-blank lines.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, ForeignKey, Index, Text, Uuid, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from fleetforge.db.base import Base

TimestampTZ = TIMESTAMP(timezone=True)


class BuildJob(Base):
    """One queued or completed server-side firmware build job (`R10-1`, `R10-2`).

    Vocabulary for `status` (`queued | running | succeeded | failed | cancelled`) is
    advisory (`TEXT` with no DB CHECK, convention 1). Exact content-addressed cache
    entries live in `builds` (`Build` in `models_artifacts.py`).
    """

    __tablename__ = "build_jobs"
    __table_args__ = (
        Index("ix_build_jobs_project_created_at", "project_id", text("created_at DESC")),
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
