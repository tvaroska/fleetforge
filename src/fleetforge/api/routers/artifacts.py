"""`GET /v1/artifact` — list deployable firmware artifacts (`R1-fe-1`, `A-11`).

Upload orchestration (`POST /v1/artifact`) lives in `routers/artifact_upload.py`. Both routers
carry admin authentication dependencies and stay separate from the public signature-authorized
`routers/artifact_download.py` router so read/write routes never share a router with unauthenticated
board downloads (`tests/test_api_artifact_list.py`).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from fleetforge.api.deps import (
    AdminDep,
    SessionMakerDep,
    bearer_scheme,
    cookie_scheme,
    resolve_project_for_request,
)
from fleetforge.api.schemas import (
    PROJECT_SLUG_PATTERN,
    ArtifactList,
    ArtifactSummary,
)
from fleetforge.db.models import ProjectRole

router = APIRouter(
    prefix="/v1/artifact",
    tags=["artifacts"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)

LIST_LIMIT = 500

_LIST_SQL = text(
    """
    SELECT v.target, v.version, v.sha256, v.created_at,
           v.repo_url, v.branch, v.commit_sha, v.tag, v.build_url, v.test_passed,
           a.size_bytes, a.partition_layout, a.kind, a.artifact_type, a.sig,
           a.sim_status, a.sim_detail, a.sim_runner, a.sim_ran_at
      FROM artifact_versions AS v
      JOIN artifacts AS a ON a.sha256 = v.sha256
     WHERE v.project_id = :project_id
     ORDER BY v.target, v.created_at DESC, v.version
     LIMIT :limit
    """
)


@router.get("", response_model=ArtifactList, summary="Deployable firmware artifacts")
async def list_artifacts(
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
    project: Annotated[
        str | None,
        Query(pattern=PROJECT_SLUG_PATTERN, description="Project slug (defaults to token/default)"),
    ] = None,
) -> ArtifactList:
    """Every labelled artifact in the project, grouped by target and newest first within a target."""
    async with sessionmaker() as session:
        target_project = await resolve_project_for_request(
            session,
            admin,
            project_slug=project,
            min_role=ProjectRole.VIEWER,
            required_scope="read",
        )
        rows = (
            await session.execute(_LIST_SQL, {"project_id": target_project.id, "limit": LIST_LIMIT})
        ).all()

    return ArtifactList(
        artifacts=[
            ArtifactSummary(
                target=row.target,
                version=row.version,
                sha256=row.sha256,
                size_bytes=row.size_bytes,
                partition_layout=row.partition_layout,
                kind=row.kind,
                created_at=row.created_at,
                artifact_type=getattr(row, "artifact_type", "app"),
                sig=row.sig,
                repo_url=row.repo_url,
                branch=row.branch,
                commit_sha=row.commit_sha,
                tag=row.tag,
                build_url=row.build_url,
                test_passed=row.test_passed,
                sim_status=getattr(row, "sim_status", None),
                sim_detail=getattr(row, "sim_detail", None),
                sim_runner=getattr(row, "sim_runner", None),
                sim_ran_at=getattr(row, "sim_ran_at", None),
            )
            for row in rows
        ]
    )
