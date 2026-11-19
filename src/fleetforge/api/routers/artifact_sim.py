"""`POST /v1/artifact/{sha256}/simulate` — admin-authenticated pre-flight simulation route (`R9-2`).

Separated from `routers/artifacts.py` so both modules stay compact (< 700 lines) while
retaining identical bearer + session-cookie authentication dependencies (`bearer_scheme`,
`cookie_scheme`) ahead of the public `routers/artifact_download.py` router.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text

from fleetforge.api.deps import (
    AdminDep,
    ObjectStoreDep,
    SessionMakerDep,
    SimRunnerDep,
    bearer_scheme,
    cookie_scheme,
    resolve_project_for_request,
    visible_project_ids,
)
from fleetforge.api.schemas import PROJECT_SLUG_PATTERN, ArtifactSimulateResponse
from fleetforge.db.models import ProjectRole
from fleetforge.storage.blobs import SHA256_HEX, blob_key
from fleetforge.storage.objectstore import ObjectNotFound, ObjectStoreError

logger = logging.getLogger(__name__)

STORE_UNAVAILABLE = "the artifact store cannot be reached; try again shortly"

_ARTIFACT_SIM_SELECT_SQL = text(
    "SELECT sha256, target, partition_layout, artifact_type FROM artifacts WHERE sha256 = :sha256"
)

_ARTIFACT_SIM_UPDATE_SQL = text(
    """
    UPDATE artifacts
       SET sim_status = :sim_status,
           sim_detail = :sim_detail,
           sim_ran_at = :sim_ran_at
     WHERE sha256 = :sha256
    """
)

router = APIRouter(
    prefix="/v1/artifact",
    tags=["artifacts"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)


@router.post(
    "/{sha256}/simulate",
    response_model=ArtifactSimulateResponse,
    summary="Boot an artifact in the pre-flight simulator and persist its R5 self-test verdict",
)
async def simulate_artifact(
    sha256: str,
    admin: AdminDep,
    store: ObjectStoreDep,
    sessionmaker: SessionMakerDep,
    sim_runner: SimRunnerDep,
    project: Annotated[
        str | None,
        Query(pattern=PROJECT_SLUG_PATTERN, description="Project slug (defaults to token/default)"),
    ] = None,
) -> ArtifactSimulateResponse:
    """Execute `SimRunner.simulate` against `sha256` and store `(sim_status, sim_detail, sim_ran_at)` (`R9-2`)."""
    if not SHA256_HEX.match(sha256):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="sha256 must be 64 lowercase hex characters",
        )
    if not ({"admin", "write:artifacts", "deploy"} & set(admin.scopes)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="insufficient permissions",
        )

    async with sessionmaker() as session:
        if project is not None or admin.project_id is not None:
            target_project = await resolve_project_for_request(
                session,
                admin,
                project_slug=project,
                min_role=ProjectRole.OPERATOR,
            )
            visible_link = await session.scalar(
                text(
                    "SELECT 1 FROM artifact_versions "
                    "WHERE project_id = :pid AND sha256 = :sha256 LIMIT 1"
                ),
                {"pid": target_project.id, "sha256": sha256},
            )
            if visible_link is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"no such artifact: {sha256}",
                )
        else:
            allowed_pids = await visible_project_ids(session, admin)
            if allowed_pids is not None:
                if not allowed_pids:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"no such artifact: {sha256}",
                    )
                visible_link = await session.scalar(
                    text(
                        "SELECT 1 FROM artifact_versions "
                        "WHERE project_id = ANY(:pids) AND sha256 = :sha256 LIMIT 1"
                    ),
                    {"pids": list(allowed_pids), "sha256": sha256},
                )
                if visible_link is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"no such artifact: {sha256}",
                    )

        row = (await session.execute(_ARTIFACT_SIM_SELECT_SQL, {"sha256": sha256})).first()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no such artifact: {sha256}",
            )

    try:
        image = await store.get(blob_key(sha256))
    except ObjectNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"artifact bytes missing from store: {sha256}",
        ) from None
    except ObjectStoreError:
        logger.exception("artifact simulation failed: object store unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=STORE_UNAVAILABLE,
        ) from None

    sim_res = await sim_runner.simulate(
        sha256=sha256,
        target=str(row.target or "esp32"),
        image=image,
        partition_layout=row.partition_layout,
        artifact_type=str(row.artifact_type or "app"),
    )

    async with sessionmaker() as session:
        await session.execute(
            _ARTIFACT_SIM_UPDATE_SQL,
            {
                "sha256": sha256,
                "sim_status": sim_res.status.value,
                "sim_detail": sim_res.detail,
                "sim_ran_at": sim_res.ran_at,
            },
        )
        await session.commit()

    return ArtifactSimulateResponse(
        sha256=sha256,
        target=row.target,
        sim_status=sim_res.status.value,
        sim_detail=sim_res.detail,
        sim_ran_at=sim_res.ran_at,
    )
