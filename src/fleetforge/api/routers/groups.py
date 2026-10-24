"""Device group CRUD endpoints — `/v1/groups`.

Groups scope enrollment tokens (`enrollment_tokens.group_id`) and organize devices within
a project (`devices.group_id`, `UNIQUE (project_id, name)`). See
`docs/features/groups-deploy.md` and `docs/features/projects-and-users.md`.
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from fleetforge.api.deps import (
    FORBIDDEN,
    AdminDep,
    SessionMakerDep,
    bearer_scheme,
    check_project_access,
    cookie_scheme,
    now_utc,
    resolve_project_for_request,
)
from fleetforge.api.schemas import (
    PROJECT_SLUG_PATTERN,
    DeviceGroupCreate,
    DeviceGroupList,
    DeviceGroupSummary,
    DeviceGroupUpdate,
)
from fleetforge.db.models import DeviceGroup, ProjectRole

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/groups",
    tags=["groups"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)

UNKNOWN_GROUP = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown group")


def _group_summary(row: DeviceGroup) -> DeviceGroupSummary:
    return DeviceGroupSummary(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        description=row.description,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get(
    "",
    response_model=DeviceGroupList,
    summary="List device groups in a project",
)
async def list_groups(
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
    project: Annotated[
        str | None,
        Query(pattern=PROJECT_SLUG_PATTERN, description="Project slug (defaults to token/default)"),
    ] = None,
) -> DeviceGroupList:
    """List groups in `project` ordered by `name` (requires `viewer` or higher)."""
    async with sessionmaker() as session:
        target_project = await resolve_project_for_request(
            session,
            admin,
            project_slug=project,
            min_role=ProjectRole.VIEWER,
            required_scope="read",
        )
        rows = (
            await session.scalars(
                select(DeviceGroup)
                .where(DeviceGroup.project_id == target_project.id)
                .order_by(DeviceGroup.name, DeviceGroup.id)
            )
        ).all()

    return DeviceGroupList(groups=[_group_summary(row) for row in rows])


@router.post(
    "",
    response_model=DeviceGroupSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Create a device group in a project (project owner or superadmin)",
)
async def create_group(
    body: DeviceGroupCreate,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
    project: Annotated[
        str | None,
        Query(pattern=PROJECT_SLUG_PATTERN, description="Project slug (defaults to token/default)"),
    ] = None,
) -> DeviceGroupSummary:
    """Create a named group in `project` (`owner` or `is_superadmin`)."""
    if "admin" not in admin.scopes:
        raise FORBIDDEN

    now = now_utc()
    name = body.name.strip()
    async with sessionmaker() as session:
        target_project = await resolve_project_for_request(
            session,
            admin,
            project_slug=project,
            min_role=ProjectRole.OWNER,
            required_scope="admin",
        )
        row = DeviceGroup(
            project_id=target_project.id,
            name=name,
            description=body.description,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"group {name!r} already exists in project {target_project.slug!r}",
            ) from exc

    logger.info("device group created: %s (%s) by %s", row.name, row.id, admin.subject)
    return _group_summary(row)


@router.patch(
    "/{group_id}",
    response_model=DeviceGroupSummary,
    summary="Rename or update description of a device group (project owner or superadmin)",
)
async def update_group(
    group_id: uuid.UUID,
    body: DeviceGroupUpdate,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> DeviceGroupSummary:
    """Update `name` or `description` on `group_id` (`owner` or `is_superadmin`)."""
    if "admin" not in admin.scopes:
        raise FORBIDDEN

    fields = body.model_fields_set
    async with sessionmaker() as session:
        row = await session.get(DeviceGroup, group_id)
        if row is None:
            raise UNKNOWN_GROUP
        await check_project_access(
            session,
            admin,
            row.project_id,
            min_role=ProjectRole.OWNER,
            required_scope="admin",
            not_found=UNKNOWN_GROUP,
        )
        if "name" in fields and body.name is not None:
            row.name = body.name.strip()
        if "description" in fields:
            row.description = body.description
        row.updated_at = now_utc()
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"group {row.name!r} already exists in this project",
            ) from exc

    return _group_summary(row)


@router.delete(
    "/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a device group (project owner or superadmin; devices become ungrouped)",
)
async def delete_group(
    group_id: uuid.UUID,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> None:
    """Delete `group_id`; member devices and tokens have `group_id` set to `NULL`."""
    if "admin" not in admin.scopes:
        raise FORBIDDEN

    async with sessionmaker() as session:
        row = await session.get(DeviceGroup, group_id)
        if row is None:
            raise UNKNOWN_GROUP
        await check_project_access(
            session,
            admin,
            row.project_id,
            min_role=ProjectRole.OWNER,
            required_scope="admin",
            not_found=UNKNOWN_GROUP,
        )
        await session.delete(row)
        await session.commit()

    logger.info("device group deleted: %s by %s", group_id, admin.subject)
