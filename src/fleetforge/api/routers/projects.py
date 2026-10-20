"""Project and project-membership endpoints — `/v1/projects`.

Isolates boards, groups, enrollment tokens, and version labels per project
(`docs/features/projects-and-users.md`).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from fleetforge.api.deps import (
    FORBIDDEN,
    AdminDep,
    SessionMakerDep,
    bearer_scheme,
    cookie_scheme,
    now_utc,
    resolve_project_for_request,
)
from fleetforge.api.schemas import (
    PROJECT_SLUG_PATTERN,
    USERNAME_PATTERN,
    ProjectCreate,
    ProjectList,
    ProjectMemberList,
    ProjectMemberSummary,
    ProjectMemberUpsert,
    ProjectSummary,
    ProjectUpdate,
)
from fleetforge.db.models import Project, ProjectMember, ProjectRole, User

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/projects",
    tags=["projects"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)


def _project_summary(proj: Project, role: str = ProjectRole.OWNER.value) -> ProjectSummary:
    return ProjectSummary(
        id=proj.id,
        slug=proj.slug,
        name=proj.name,
        description=proj.description,
        role=role,
        created_at=proj.created_at,
        updated_at=proj.updated_at,
    )


@router.get(
    "",
    response_model=ProjectList,
    summary="List projects visible to the caller",
)
async def list_projects(admin: AdminDep, sessionmaker: SessionMakerDep) -> ProjectList:
    """List projects visible to `admin`, ordered by `slug`."""
    async with sessionmaker() as session:
        if admin.project_id is not None:
            proj = await session.get(Project, admin.project_id)
            return ProjectList(
                projects=[_project_summary(proj, role="token")] if proj is not None else []
            )

        if admin.is_superadmin:
            rows = (await session.scalars(select(Project).order_by(Project.slug))).all()
            return ProjectList(
                projects=[_project_summary(p, role=ProjectRole.OWNER.value) for p in rows]
            )

        if admin.user_id is not None:
            stmt = (
                select(Project, ProjectMember.role)
                .join(ProjectMember, ProjectMember.project_id == Project.id)
                .where(ProjectMember.user_id == admin.user_id)
                .order_by(Project.slug)
            )
            member_rows = (await session.execute(stmt)).all()
            return ProjectList(
                projects=[_project_summary(proj, role=role) for proj, role in member_rows]
            )

        rows = (await session.scalars(select(Project).order_by(Project.slug))).all()
        return ProjectList(projects=[_project_summary(p, role="token") for p in rows])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ProjectSummary,
    summary="Create a new project (superadmin only)",
)
async def create_project(
    body: ProjectCreate,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> ProjectSummary:
    """Create a project (`slug` is immutable once created)."""
    if not admin.is_superadmin or "admin" not in admin.scopes:
        raise FORBIDDEN

    now = now_utc()
    proj = Project(
        slug=body.slug,
        name=body.name,
        description=body.description,
        created_at=now,
        updated_at=now,
    )
    async with sessionmaker() as session:
        session.add(proj)
        try:
            await session.flush()
            if admin.user_id is not None:
                session.add(
                    ProjectMember(
                        project_id=proj.id,
                        user_id=admin.user_id,
                        role=ProjectRole.OWNER.value,
                        created_at=now,
                    )
                )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"project {body.slug!r} already exists",
            ) from exc

    logger.info("project created: %s (%s) by %s", proj.slug, proj.id, admin.subject)
    return _project_summary(proj, role=ProjectRole.OWNER.value)


@router.patch(
    "/{slug}",
    response_model=ProjectSummary,
    summary="Update a project's display name or description",
)
async def update_project(
    slug: str = Path(pattern=PROJECT_SLUG_PATTERN),
    *,
    body: ProjectUpdate,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> ProjectSummary:
    """Update `name` or `description` on `slug` (requires project `owner` or superadmin)."""
    if "admin" not in admin.scopes:
        raise FORBIDDEN

    async with sessionmaker() as session:
        proj = await resolve_project_for_request(session, admin, slug, min_role=ProjectRole.OWNER)
        if "name" in body.model_fields_set and body.name is not None:
            proj.name = body.name
        if "description" in body.model_fields_set:
            proj.description = body.description
        proj.updated_at = now_utc()
        await session.commit()
        return _project_summary(proj, role=ProjectRole.OWNER.value)


@router.get(
    "/{slug}/members",
    response_model=ProjectMemberList,
    summary="List members of a project",
)
async def list_project_members(
    slug: str = Path(pattern=PROJECT_SLUG_PATTERN),
    *,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> ProjectMemberList:
    """List human members and their roles on `slug`."""
    async with sessionmaker() as session:
        proj = await resolve_project_for_request(
            session, admin, slug, min_role=ProjectRole.VIEWER, required_scope="read"
        )
        stmt = (
            select(ProjectMember, User)
            .join(User, User.id == ProjectMember.user_id)
            .where(ProjectMember.project_id == proj.id)
            .order_by(User.username)
        )
        rows = (await session.execute(stmt)).all()

    return ProjectMemberList(
        members=[
            ProjectMemberSummary(
                user_id=user.id,
                username=user.username,
                display_name=user.display_name,
                role=member.role,
                created_at=member.created_at,
            )
            for member, user in rows
        ]
    )


@router.put(
    "/{slug}/members/{username}",
    response_model=ProjectMemberSummary,
    summary="Add or update a user's role on a project",
)
async def upsert_project_member(
    slug: str = Path(pattern=PROJECT_SLUG_PATTERN),
    username: str = Path(pattern=USERNAME_PATTERN),
    *,
    body: ProjectMemberUpsert,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> ProjectMemberSummary:
    """Assign `role` (`owner`, `operator`, `viewer`) to `username` on `slug`."""
    if "admin" not in admin.scopes:
        raise FORBIDDEN

    async with sessionmaker() as session:
        proj = await resolve_project_for_request(session, admin, slug, min_role=ProjectRole.OWNER)
        user = await session.scalar(select(User).where(User.username == username))
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"user {username!r} not found",
            )
        existing = await session.get(ProjectMember, (proj.id, user.id))
        if existing is not None:
            if existing.role == ProjectRole.OWNER.value and body.role != ProjectRole.OWNER:
                owner_count = await session.scalar(
                    select(func.count())
                    .select_from(ProjectMember)
                    .where(
                        ProjectMember.project_id == proj.id,
                        ProjectMember.role == ProjectRole.OWNER.value,
                    )
                )
                if (owner_count or 0) <= 1:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="cannot demote the last owner of a project",
                    )
            existing.role = body.role.value
            member = existing
        else:
            member = ProjectMember(
                project_id=proj.id,
                user_id=user.id,
                role=body.role.value,
                created_at=now_utc(),
            )
            session.add(member)
        await session.commit()

    return ProjectMemberSummary(
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=member.role,
        created_at=member.created_at,
    )


@router.delete(
    "/{slug}/members/{username}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a user from a project",
)
async def remove_project_member(
    slug: str = Path(pattern=PROJECT_SLUG_PATTERN),
    username: str = Path(pattern=USERNAME_PATTERN),
    *,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> None:
    """Remove `username` from `slug` (refuses removing the last `owner`)."""
    if "admin" not in admin.scopes:
        raise FORBIDDEN

    async with sessionmaker() as session:
        proj = await resolve_project_for_request(session, admin, slug, min_role=ProjectRole.OWNER)
        user = await session.scalar(select(User).where(User.username == username))
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"user {username!r} not found",
            )
        existing = await session.get(ProjectMember, (proj.id, user.id))
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"user {username!r} is not a member of project {slug!r}",
            )
        if existing.role == ProjectRole.OWNER.value:
            owner_count = await session.scalar(
                select(func.count())
                .select_from(ProjectMember)
                .where(
                    ProjectMember.project_id == proj.id,
                    ProjectMember.role == ProjectRole.OWNER.value,
                )
            )
            if (owner_count or 0) <= 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="cannot remove the last owner of a project",
                )
        await session.delete(existing)
        await session.commit()
