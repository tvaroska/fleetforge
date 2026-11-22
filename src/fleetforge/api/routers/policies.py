"""Multi-ring cohort deploy policy CRUD & evaluation routes (`R11-1`, closing `V2`).

Mounted at `/v1/projects/{slug}/policies`:
- `GET /v1/projects/{slug}/policies` — list project deploy policies.
- `POST /v1/projects/{slug}/policies` — create a multi-ring cohort deploy policy.
- `PATCH /v1/projects/{slug}/policies/{policy_id}` — update rings, match ref, or health gates.
- `DELETE /v1/projects/{slug}/policies/{policy_id}` — remove a deploy policy.
- `POST /v1/projects/{slug}/policies/{policy_id}/evaluate` — evaluate or advance ring promotion
  for a given firmware `version` and inspect per-ring status / auto-halt reasons.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.api.deps import (
    AdminDep,
    CommandPublisherDep,
    SessionMakerDep,
    SettingsDep,
    bearer_scheme,
    cookie_scheme,
    resolve_project_for_request,
)
from fleetforge.api.routers.artifacts import VERSION_PATTERN
from fleetforge.api.schemas import PROJECT_SLUG_PATTERN
from fleetforge.api.schemas_v2 import (
    DeployPolicyCreate,
    DeployPolicyEvaluateRequest,
    DeployPolicyEvaluation,
    DeployPolicyList,
    DeployPolicySummary,
    DeployPolicyUpdate,
)
from fleetforge.clock import now_utc
from fleetforge.db.models import DeployPolicy, DeviceGroup, ProjectRole
from fleetforge.deploy_policies import evaluate_policy_for_version

router = APIRouter(
    prefix="/v1/projects/{slug}/policies",
    tags=["policies"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)


def _to_summary(row: DeployPolicy) -> DeployPolicySummary:
    return DeployPolicySummary(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        match_mode=row.match_mode,  # type: ignore[arg-type]
        match_ref=row.match_ref,
        ring_group_ids=list(row.ring_group_ids),
        max_parallel=row.max_parallel,
        require_sim_passed=row.require_sim_passed,
        min_soak_s=row.min_soak_s,
        min_free_heap_bytes=row.min_free_heap_bytes,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _validate_ring_groups(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    ring_group_ids: list[uuid.UUID],
) -> list[uuid.UUID]:
    deduped: list[uuid.UUID] = []
    for gid in ring_group_ids:
        if gid in deduped:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="ring_group_ids must not repeat the same group_id twice",
            )
        deduped.append(gid)
    found_ids = set(
        (
            await session.scalars(
                select(DeviceGroup.id).where(
                    DeviceGroup.project_id == project_id,
                    DeviceGroup.id.in_(deduped),
                )
            )
        ).all()
    )
    missing = [str(gid) for gid in deduped if gid not in found_ids]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown group_id(s) for this project: {', '.join(missing)}",
        )
    return deduped


@router.get("", response_model=DeployPolicyList, summary="List cohort deploy policies")
async def list_policies(
    slug: Annotated[str, Path(pattern=PROJECT_SLUG_PATTERN)],
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> DeployPolicyList:
    async with sessionmaker() as session:
        project = await resolve_project_for_request(
            session,
            admin,
            project_slug=slug,
            min_role=ProjectRole.VIEWER,
            required_scope="read",
        )
        rows = (
            await session.scalars(
                select(DeployPolicy)
                .where(DeployPolicy.project_id == project.id)
                .order_by(DeployPolicy.created_at.asc(), DeployPolicy.id.asc())
            )
        ).all()
    return DeployPolicyList(policies=[_to_summary(row) for row in rows])


@router.post(
    "",
    response_model=DeployPolicySummary,
    status_code=status.HTTP_201_CREATED,
    summary="Create a multi-ring cohort deploy policy",
)
async def create_policy(
    slug: Annotated[str, Path(pattern=PROJECT_SLUG_PATTERN)],
    body: DeployPolicyCreate,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> DeployPolicySummary:
    name = body.name.strip()
    match_ref = body.match_ref.strip()
    if not name or not match_ref:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="name and match_ref must not be blank",
        )

    async with sessionmaker() as session:
        project = await resolve_project_for_request(
            session,
            admin,
            project_slug=slug,
            min_role=ProjectRole.OPERATOR,
            required_scope="deploy",
        )
        rings = await _validate_ring_groups(
            session,
            project_id=project.id,
            ring_group_ids=body.ring_group_ids,
        )
        row = DeployPolicy(
            project_id=project.id,
            name=name,
            match_mode=body.match_mode,
            match_ref=match_ref,
            ring_group_ids=rings,
            max_parallel=body.max_parallel,
            require_sim_passed=body.require_sim_passed,
            min_soak_s=body.min_soak_s,
            min_free_heap_bytes=body.min_free_heap_bytes,
            enabled=body.enabled,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"a deploy policy named {name!r} already exists in this project",
            ) from None
        await session.refresh(row)
        return _to_summary(row)


@router.patch(
    "/{policy_id}",
    response_model=DeployPolicySummary,
    summary="Update a cohort deploy policy",
)
async def update_policy(
    slug: Annotated[str, Path(pattern=PROJECT_SLUG_PATTERN)],
    policy_id: uuid.UUID,
    body: DeployPolicyUpdate,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> DeployPolicySummary:
    async with sessionmaker() as session:
        project = await resolve_project_for_request(
            session,
            admin,
            project_slug=slug,
            min_role=ProjectRole.OPERATOR,
            required_scope="deploy",
        )
        row = await session.get(DeployPolicy, policy_id)
        if row is None or row.project_id != project.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="deploy policy not found"
            )
        fields_set = body.model_fields_set
        if "name" in fields_set and body.name is not None:
            cleaned_name = body.name.strip()
            if not cleaned_name:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="name must not be blank"
                )
            row.name = cleaned_name
        if "match_mode" in fields_set and body.match_mode is not None:
            row.match_mode = body.match_mode
        if "match_ref" in fields_set and body.match_ref is not None:
            cleaned_ref = body.match_ref.strip()
            if not cleaned_ref:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="match_ref must not be blank"
                )
            row.match_ref = cleaned_ref
        if "ring_group_ids" in fields_set and body.ring_group_ids is not None:
            row.ring_group_ids = await _validate_ring_groups(
                session,
                project_id=project.id,
                ring_group_ids=body.ring_group_ids,
            )
        if "max_parallel" in fields_set and body.max_parallel is not None:
            row.max_parallel = body.max_parallel
        if "require_sim_passed" in fields_set and body.require_sim_passed is not None:
            row.require_sim_passed = body.require_sim_passed
        if "min_soak_s" in fields_set and body.min_soak_s is not None:
            row.min_soak_s = body.min_soak_s
        if "min_free_heap_bytes" in fields_set:
            row.min_free_heap_bytes = body.min_free_heap_bytes
        if "enabled" in fields_set and body.enabled is not None:
            row.enabled = body.enabled
        row.updated_at = now_utc()

        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="a deploy policy with this name already exists in this project",
            ) from None
        await session.refresh(row)
        return _to_summary(row)


@router.delete(
    "/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a cohort deploy policy",
)
async def delete_policy(
    slug: Annotated[str, Path(pattern=PROJECT_SLUG_PATTERN)],
    policy_id: uuid.UUID,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> Response:
    async with sessionmaker() as session:
        project = await resolve_project_for_request(
            session,
            admin,
            project_slug=slug,
            min_role=ProjectRole.OPERATOR,
            required_scope="deploy",
        )
        row = await session.get(DeployPolicy, policy_id)
        if row is None or row.project_id != project.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="deploy policy not found"
            )
        await session.delete(row)
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{policy_id}/evaluate",
    response_model=DeployPolicyEvaluation,
    summary="Evaluate or advance multi-ring cohort promotion for a firmware version",
)
async def evaluate_policy(
    slug: Annotated[str, Path(pattern=PROJECT_SLUG_PATTERN)],
    policy_id: uuid.UUID,
    body: DeployPolicyEvaluateRequest,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
    settings: SettingsDep,
    publisher: CommandPublisherDep,
) -> DeployPolicyEvaluation:
    if not VERSION_PATTERN.match(body.version):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "version must be 1-64 characters of letters, digits, '.', '_', '+' or "
                "'-', starting with a letter or digit"
            ),
        )
    async with sessionmaker() as session:
        project = await resolve_project_for_request(
            session,
            admin,
            project_slug=slug,
            min_role=ProjectRole.OPERATOR,
            required_scope="deploy",
        )
        row = await session.get(DeployPolicy, policy_id)
        if row is None or row.project_id != project.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="deploy policy not found"
            )
        return await evaluate_policy_for_version(
            session,
            policy=row,
            version=body.version,
            settings=settings,
            publisher=publisher,
            admin=admin,
            republish_in_flight=True,
            resume=body.resume,
        )
