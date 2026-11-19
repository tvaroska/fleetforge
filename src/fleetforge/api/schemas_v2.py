"""V2 & V3 API request/response schemas (`R10` builds, `R11` policies, `V3` swarm/delta)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BuildJobStatusLiteral = Literal["queued", "running", "succeeded", "failed", "cancelled"]
BuildTargetLiteral = Literal["esp32", "esp32s3", "esp32c3", "esp32c6"]


class BuildJobCreate(BaseModel):
    """`POST /v1/projects/{slug}/builds` request body (`R10-1`, `R10-2`)."""

    model_config = ConfigDict(extra="forbid")

    repo_url: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="Git repository URL (defaults to project.repo_url when omitted)",
    )
    git_ref: str = Field(
        min_length=1,
        max_length=128,
        description="Branch, tag, or commit SHA to build",
    )
    target: BuildTargetLiteral
    version: str = Field(min_length=1, max_length=64)
    partition_layout: str = Field(default="ab-4m-v1", min_length=1, max_length=32)


class BuildJobSummary(BaseModel):
    """One server-side build job without full log text (`R10-2`)."""

    id: uuid.UUID
    project_id: uuid.UUID
    repo_url: str
    git_ref: str
    commit_sha: str | None
    target: str
    version: str
    partition_layout: str
    idf_image: str
    cache_key: str | None
    status: BuildJobStatusLiteral
    cache_hit: bool
    artifact_sha256: str | None
    error_detail: str | None
    triggered_by: str | None
    created_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None


class BuildJobDetail(BuildJobSummary):
    """Single build job including captured `log_text` (`GET /v1/projects/{slug}/builds/{id}`)."""

    log_text: str


class BuildJobList(BaseModel):
    """Envelope returned by `GET /v1/projects/{slug}/builds`."""

    builds: list[BuildJobSummary]
