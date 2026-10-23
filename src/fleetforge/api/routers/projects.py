"""Project and project-membership endpoints — `/v1/projects`.

Isolates boards, groups, enrollment tokens, and version labels per project
(`docs/features/projects-and-users.md`).
"""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from fleetforge.api.deps import (
    FORBIDDEN,
    AdminDep,
    CommandPublisherDep,
    SessionMakerDep,
    SettingsDep,
    bearer_scheme,
    cookie_scheme,
    now_utc,
    resolve_project_for_request,
)
from fleetforge.api.routers.deploys import (
    deploy_version_to_project_devices,
    matches_project_tracking,
)
from fleetforge.api.schemas import (
    PROJECT_SLUG_PATTERN,
    SECRET_KEY_PATTERN,
    USERNAME_PATTERN,
    ProjectCreate,
    ProjectDeployAccepted,
    ProjectDeployRequest,
    ProjectList,
    ProjectMemberList,
    ProjectMemberSummary,
    ProjectMemberUpsert,
    ProjectSecretList,
    ProjectSecretSummary,
    ProjectSecretUpsert,
    ProjectSummary,
    ProjectUpdate,
)
from fleetforge.broker import CommandPublisher, CommandPublishError, cfg_payload
from fleetforge.db.models import (
    ArtifactVersion,
    Device,
    Project,
    ProjectMember,
    ProjectRole,
    ProjectSecret,
    User,
)
from fleetforge.secrets_crypto import encrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/projects",
    tags=["projects"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)


def _project_summary(
    proj: Project,
    role: str = ProjectRole.OWNER.value,
    *,
    auto_deployed_devices: list[str] | None = None,
    ci_webhook_triggered: bool | None = None,
) -> ProjectSummary:
    return ProjectSummary(
        id=proj.id,
        slug=proj.slug,
        name=proj.name,
        description=proj.description,
        role=role,
        repo_url=proj.repo_url,
        track_mode=proj.track_mode,
        track_ref=proj.track_ref,
        auto_deploy_target=proj.auto_deploy_target,
        ci_webhook_url=proj.ci_webhook_url,
        has_ci_webhook_header=bool(proj.ci_webhook_header),
        secrets_rev=proj.secrets_rev,
        otel_endpoint=proj.otel_endpoint,
        has_otel_headers=bool(proj.otel_headers),
        auto_deployed_devices=auto_deployed_devices,
        ci_webhook_triggered=ci_webhook_triggered,
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
    settings: SettingsDep,
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
        repo_url=body.repo_url,
        track_mode=body.track_mode,
        track_ref=body.track_ref,
        auto_deploy_target=body.auto_deploy_target,
        ci_webhook_url=body.ci_webhook_url,
        ci_webhook_header=body.ci_webhook_header,
        otel_endpoint=body.otel_endpoint,
        otel_headers=encrypt_secret(body.otel_headers, settings) if body.otel_headers else None,
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


async def _trigger_ci_webhook(proj: Project) -> bool:
    """Best-effort webhook POST to trigger an external CI build when switching refs."""
    if not proj.ci_webhook_url:
        return False
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if proj.ci_webhook_header:
        headers["Authorization"] = proj.ci_webhook_header
    payload = {
        "project": proj.slug,
        "repo_url": proj.repo_url,
        "track_mode": proj.track_mode,
        "track_ref": proj.track_ref,
        "auto_deploy_target": proj.auto_deploy_target,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(proj.ci_webhook_url, json=payload, headers=headers)
            return 200 <= resp.status_code < 300
    except httpx.HTTPError:
        logger.warning("CI webhook dispatch failed for project %s", proj.slug, exc_info=True)
        return False


@router.patch(
    "/{slug}",
    response_model=ProjectSummary,
    summary="Update a project's display name, description, or continuous deployment settings",
)
async def update_project(
    slug: str = Path(pattern=PROJECT_SLUG_PATTERN),
    *,
    body: ProjectUpdate,
    admin: AdminDep,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
    publisher: CommandPublisherDep,
) -> ProjectSummary:
    """Update metadata or CD settings on `slug` (requires project `owner` or superadmin)."""
    if "admin" not in admin.scopes:
        raise FORBIDDEN

    fields = body.model_fields_set
    cd_changed = bool(fields & {"repo_url", "track_mode", "track_ref", "auto_deploy_target"})

    async with sessionmaker() as session:
        proj = await resolve_project_for_request(session, admin, slug, min_role=ProjectRole.OWNER)
        if "name" in fields and body.name is not None:
            proj.name = body.name
        if "description" in fields:
            proj.description = body.description
        if "repo_url" in fields:
            proj.repo_url = body.repo_url
        if "track_mode" in fields and body.track_mode is not None:
            proj.track_mode = body.track_mode
        if "track_ref" in fields:
            proj.track_ref = body.track_ref
        if "auto_deploy_target" in fields and body.auto_deploy_target is not None:
            proj.auto_deploy_target = body.auto_deploy_target
        if "ci_webhook_url" in fields:
            proj.ci_webhook_url = body.ci_webhook_url
        if "ci_webhook_header" in fields:
            proj.ci_webhook_header = body.ci_webhook_header
        if "otel_endpoint" in fields:
            proj.otel_endpoint = body.otel_endpoint
        if "otel_headers" in fields:
            proj.otel_headers = (
                encrypt_secret(body.otel_headers, settings) if body.otel_headers else None
            )
        proj.updated_at = now_utc()
        await session.commit()

        auto_deployed: list[str] | None = None
        webhook_triggered: bool | None = None
        if (
            cd_changed
            and proj.track_mode != "off"
            and proj.auto_deploy_target in ("dev", "all")
            and proj.track_ref
        ):
            versions = (
                await session.scalars(
                    select(ArtifactVersion)
                    .where(ArtifactVersion.project_id == proj.id)
                    .order_by(ArtifactVersion.created_at.desc())
                )
            ).all()
            matched_by_target: dict[str, str] = {}
            for ver in versions:
                if ver.target in matched_by_target:
                    continue
                if matches_project_tracking(
                    proj,
                    repo_url=ver.repo_url,
                    branch=ver.branch,
                    commit_sha=ver.commit_sha,
                    tag=ver.tag,
                    test_passed=ver.test_passed,
                ):
                    matched_by_target[ver.target] = ver.version

            if matched_by_target:
                deployed_ids: list[str] = []
                for target_chip, ver_str in matched_by_target.items():
                    deployed_list, _ = await deploy_version_to_project_devices(
                        session,
                        project_id=proj.id,
                        version=ver_str,
                        target_filter=proj.auto_deploy_target,
                        apply="auto",
                        override=(),
                        admin=admin,
                        settings=settings,
                        publisher=publisher,
                        target_chip=target_chip,
                        best_effort_broker=True,
                    )
                    deployed_ids.extend(d.device_id for d in deployed_list)
                auto_deployed = deployed_ids
            elif proj.ci_webhook_url:
                webhook_triggered = await _trigger_ci_webhook(proj)

        return _project_summary(
            proj,
            role=ProjectRole.OWNER.value,
            auto_deployed_devices=auto_deployed,
            ci_webhook_triggered=webhook_triggered,
        )


@router.post(
    "/{slug}/deploy",
    response_model=ProjectDeployAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Stage a firmware version across all or dev compatible devices in a project",
)
async def deploy_project(
    slug: str = Path(pattern=PROJECT_SLUG_PATTERN),
    *,
    body: ProjectDeployRequest,
    admin: AdminDep,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
    publisher: CommandPublisherDep,
) -> ProjectDeployAccepted:
    """Stage `body.version` onto all (or `dev`-only) compatible boards in `slug`."""
    async with sessionmaker() as session:
        proj = await resolve_project_for_request(
            session,
            admin,
            slug,
            min_role=ProjectRole.OPERATOR,
            required_scope="deploy",
        )
        deployed, skipped = await deploy_version_to_project_devices(
            session,
            project_id=proj.id,
            version=body.version,
            target_filter=body.target_filter,
            apply=body.apply,
            override=body.override,
            admin=admin,
            settings=settings,
            publisher=publisher,
            best_effort_broker=False,
            canary=body.canary,
            max_parallel=body.max_parallel,
        )

    return ProjectDeployAccepted(
        project=slug,
        version=body.version,
        target_filter=body.target_filter,
        deployed=deployed,
        skipped=skipped,
    )


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


async def _publish_project_cfg(
    sessionmaker: SessionMakerDep,
    publisher: CommandPublisher,
    project_id: object,
    secrets_rev: int,
) -> None:
    """Best-effort publish retained `dn/cfg` to all active boards in `project_id`."""
    async with sessionmaker() as session:
        devices = (
            await session.scalars(
                select(Device).where(
                    Device.project_id == project_id,
                    Device.decommissioned_at.is_(None),
                )
            )
        ).all()
    for dev in devices:
        try:
            await publisher.publish_cfg(
                dev.device_id,
                cfg_payload(secrets_rev=secrets_rev, stay_awake=dev.stay_awake),
            )
        except CommandPublishError:
            logger.warning(
                "dn/cfg publish skipped or failed for %s (secrets_rev=%d)",
                dev.device_id,
                secrets_rev,
            )


@router.get(
    "/{slug}/secrets",
    response_model=ProjectSecretList,
    summary="List rotating secret keys configured on a project (values are never returned)",
)
async def list_project_secrets(
    slug: str = Path(pattern=PROJECT_SLUG_PATTERN),
    *,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> ProjectSecretList:
    """List secret keys and timestamps on `slug` (requires project `viewer` or higher)."""
    async with sessionmaker() as session:
        proj = await resolve_project_for_request(
            session, admin, slug, min_role=ProjectRole.VIEWER, required_scope="read"
        )
        rows = (
            await session.scalars(
                select(ProjectSecret)
                .where(ProjectSecret.project_id == proj.id)
                .order_by(ProjectSecret.key)
            )
        ).all()
        return ProjectSecretList(
            secrets_rev=proj.secrets_rev,
            secrets=[ProjectSecretSummary(key=r.key, updated_at=r.updated_at) for r in rows],
        )


@router.put(
    "/{slug}/secrets/{key}",
    response_model=ProjectSecretList,
    summary="Create or rotate a project secret and broadcast the bumped secrets_rev on dn/cfg",
)
async def upsert_project_secret(
    slug: str = Path(pattern=PROJECT_SLUG_PATTERN),
    key: str = Path(pattern=SECRET_KEY_PATTERN),
    *,
    body: ProjectSecretUpsert,
    admin: AdminDep,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
    publisher: CommandPublisherDep,
) -> ProjectSecretList:
    """Encrypt and store `key` on `slug`, bump `secrets_rev`, and publish retained `dn/cfg`."""
    async with sessionmaker() as session:
        proj = await resolve_project_for_request(
            session,
            admin,
            slug,
            min_role=ProjectRole.OPERATOR,
            required_scope="deploy",
        )
        now = now_utc()
        ciphertext = encrypt_secret(body.value, settings)
        existing = await session.get(ProjectSecret, (proj.id, key))
        if existing is not None:
            existing.ciphertext = ciphertext
            existing.updated_at = now
        else:
            session.add(
                ProjectSecret(
                    project_id=proj.id,
                    key=key,
                    ciphertext=ciphertext,
                    updated_at=now,
                )
            )
        proj.secrets_rev = (proj.secrets_rev or 0) + 1
        proj.updated_at = now
        await session.commit()

        rows = (
            await session.scalars(
                select(ProjectSecret)
                .where(ProjectSecret.project_id == proj.id)
                .order_by(ProjectSecret.key)
            )
        ).all()
        rev = proj.secrets_rev
        pid = proj.id
        summary = ProjectSecretList(
            secrets_rev=rev,
            secrets=[ProjectSecretSummary(key=r.key, updated_at=r.updated_at) for r in rows],
        )

    await _publish_project_cfg(sessionmaker, publisher, pid, rev)
    return summary


@router.delete(
    "/{slug}/secrets/{key}",
    response_model=ProjectSecretList,
    summary="Delete a project secret and broadcast the bumped secrets_rev on dn/cfg",
)
async def delete_project_secret(
    slug: str = Path(pattern=PROJECT_SLUG_PATTERN),
    key: str = Path(pattern=SECRET_KEY_PATTERN),
    *,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
    publisher: CommandPublisherDep,
) -> ProjectSecretList:
    """Remove `key` from `slug`, bump `secrets_rev`, and publish retained `dn/cfg`."""
    async with sessionmaker() as session:
        proj = await resolve_project_for_request(
            session,
            admin,
            slug,
            min_role=ProjectRole.OPERATOR,
            required_scope="deploy",
        )
        existing = await session.get(ProjectSecret, (proj.id, key))
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"secret {key!r} not found in project {slug!r}",
            )
        now = now_utc()
        await session.delete(existing)
        proj.secrets_rev = (proj.secrets_rev or 0) + 1
        proj.updated_at = now
        await session.commit()

        rows = (
            await session.scalars(
                select(ProjectSecret)
                .where(ProjectSecret.project_id == proj.id)
                .order_by(ProjectSecret.key)
            )
        ).all()
        rev = proj.secrets_rev
        pid = proj.id
        summary = ProjectSecretList(
            secrets_rev=rev,
            secrets=[ProjectSecretSummary(key=r.key, updated_at=r.updated_at) for r in rows],
        )

    await _publish_project_cfg(sessionmaker, publisher, pid, rev)
    return summary
