"""Project and project-membership endpoints — `/v1/projects`.

Isolates boards, groups, enrollment tokens, and version labels per project
(`docs/features/projects-and-users.md`).
"""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.api.deps import (
    FORBIDDEN,
    AdminDep,
    AuthContext,
    CommandPublisherDep,
    SessionMakerDep,
    SettingsDep,
    bearer_scheme,
    cookie_scheme,
    now_utc,
    resolve_project_for_request,
    visible_projects_with_roles,
)
from fleetforge.api.router_helpers import resolve_deploy_sender as _sender
from fleetforge.api.schemas import (
    PROJECT_SLUG_PATTERN,
    USERNAME_PATTERN,
    CommandAccepted,
    ProjectCreate,
    ProjectDeployAccepted,
    ProjectDeployCancelAccepted,
    ProjectDeployCancelRequest,
    ProjectDeployRequest,
    ProjectList,
    ProjectMemberList,
    ProjectMemberSummary,
    ProjectMemberUpsert,
    ProjectSummary,
    ProjectUpdate,
)
from fleetforge.broker import (
    CommandPublisher,
    CommandPublishError,
    new_command_id,
    verb_payload,
)
from fleetforge.config import Settings
from fleetforge.db.models import (
    ArtifactVersion,
    CommandType,
    DeployState,
    Device,
    Project,
    ProjectMember,
    ProjectRole,
    User,
)
from fleetforge.deploy_urls import ArtifactUrlNotConfiguredError
from fleetforge.deploys import latest_deploys
from fleetforge.device_commands import mark_publish_failed, record_sent
from fleetforge.presence import is_online
from fleetforge.rollout import (
    close_matching_rollouts,
    deploy_version_to_project_devices,
    matches_project_tracking,
)
from fleetforge.rollout_types import RolloutBrokerError, RolloutValidationError
from fleetforge.secrets_crypto import decrypt_secret, encrypt_secret
from fleetforge.url_safety import (
    public_https_transport,
    resolve_and_validate_public_https_url,
)

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
        track_mode=proj.track_mode,  # type: ignore[arg-type]
        track_ref=proj.track_ref,
        auto_deploy_target=proj.auto_deploy_target,  # type: ignore[arg-type]
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
        rows = await visible_projects_with_roles(session, admin)
        return ProjectList(projects=[_project_summary(proj, role=role) for proj, role in rows])


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
        ci_webhook_header=(
            encrypt_secret(body.ci_webhook_header, settings) if body.ci_webhook_header else None
        ),
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


async def _trigger_ci_webhook(proj: Project, settings: Settings | None = None) -> bool:
    """Best-effort webhook POST to trigger an external CI build when switching refs."""
    if not proj.ci_webhook_url:
        return False
    try:
        webhook_url = await resolve_and_validate_public_https_url(
            proj.ci_webhook_url, field_name="ci_webhook_url"
        )
    except ValueError as exc:
        logger.warning("CI webhook blocked for project %s: %s", proj.slug, exc)
        return False
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if proj.ci_webhook_header:
        headers["Authorization"] = decrypt_secret(proj.ci_webhook_header, settings)
    payload = {
        "project": proj.slug,
        "repo_url": proj.repo_url,
        "track_mode": proj.track_mode,
        "track_ref": proj.track_ref,
        "auto_deploy_target": proj.auto_deploy_target,
    }
    try:
        transport = public_https_transport(field_name="ci_webhook_url")
        async with httpx.AsyncClient(
            timeout=5.0, follow_redirects=False, trust_env=False, transport=transport
        ) as client:
            resp = await client.post(webhook_url, json=payload, headers=headers)
            return 200 <= resp.status_code < 300
    except httpx.HTTPError:
        logger.warning("CI webhook dispatch failed for project %s", proj.slug, exc_info=True)
        return False


def _apply_project_patch_fields(
    proj: Project,
    body: ProjectUpdate,
    fields: set[str],
    settings: Settings,
) -> None:
    """Apply non-null/nullable patch fields onto `proj` in one place (`A-32`)."""
    for attr in ("name", "track_mode", "auto_deploy_target"):
        if attr in fields:
            val = getattr(body, attr)
            if val is not None:
                setattr(proj, attr, val)
    for attr in ("description", "repo_url", "track_ref", "ci_webhook_url", "otel_endpoint"):
        if attr in fields:
            setattr(proj, attr, getattr(body, attr))
    if "ci_webhook_header" in fields:
        proj.ci_webhook_header = (
            encrypt_secret(body.ci_webhook_header, settings) if body.ci_webhook_header else None
        )
    if "otel_headers" in fields:
        proj.otel_headers = (
            encrypt_secret(body.otel_headers, settings) if body.otel_headers else None
        )
    proj.updated_at = now_utc()


async def _trigger_cd_after_update(
    session: AsyncSession,
    *,
    proj: Project,
    admin: AuthContext,
    settings: Settings,
    publisher: CommandPublisher,
) -> tuple[list[str] | None, bool | None]:
    """Deploy matching artifacts or dispatch the project CI webhook after a CD tracking update."""
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
        return deployed_ids, None
    if proj.ci_webhook_url:
        return None, await _trigger_ci_webhook(proj, settings=settings)
    return None, None


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
    """Update metadata or CD settings on `slug` (`deploy` scope + `operator` for CD tracking fields; `admin` scope + `owner` for metadata/webhooks/OTEL)."""
    fields = body.model_fields_set
    cd_fields = {"repo_url", "track_mode", "track_ref", "auto_deploy_target"}
    cd_changed = bool(fields & cd_fields)
    admin_fields_changed = not fields or bool(fields - cd_fields)

    if admin_fields_changed and "admin" not in admin.scopes:
        raise FORBIDDEN

    async with sessionmaker() as session:
        proj = await resolve_project_for_request(
            session,
            admin,
            slug,
            min_role=ProjectRole.OWNER if admin_fields_changed else ProjectRole.OPERATOR,
            required_scope="admin" if admin_fields_changed else "deploy",
        )
        _apply_project_patch_fields(proj, body, fields, settings)
        await session.commit()

        auto_deployed: list[str] | None = None
        webhook_triggered: bool | None = None
        if (
            cd_changed
            and proj.track_mode != "off"
            and proj.auto_deploy_target in ("dev", "all")
            and proj.track_ref
        ):
            try:
                auto_deployed, webhook_triggered = await _trigger_cd_after_update(
                    session, proj=proj, admin=admin, settings=settings, publisher=publisher
                )
            except RolloutValidationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
                ) from None
            except (RolloutBrokerError, ArtifactUrlNotConfiguredError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
                ) from None

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
        try:
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
                group_id=body.group_id,
                best_effort_broker=False,
                canary=body.canary,
                max_parallel=body.max_parallel,
            )
        except RolloutValidationError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
        except (RolloutBrokerError, ArtifactUrlNotConfiguredError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from None

    return ProjectDeployAccepted(
        project=slug,
        version=body.version,
        target_filter=body.target_filter,
        deployed=deployed,
        skipped=skipped,
    )


_PRE_APPLY_CANCEL_STATES = frozenset(
    {
        DeployState.REQUESTED.value,
        DeployState.STAGING.value,
        DeployState.DOWNLOADING.value,
        DeployState.VERIFYING.value,
        DeployState.STAGED.value,
        DeployState.AWAITING_SAFE_WINDOW.value,
    }
)


def _cancel_skip_reason(
    device: Device,
    snap: object,
    version: str | None,
) -> str | None:
    """Return why `device` cannot be cohort-cancelled, or `None` if eligible."""
    if snap is None or getattr(snap, "is_terminal", True) or getattr(snap, "cmd_id", None) is None:
        return "no in-flight transaction"
    artifact_version = getattr(snap, "artifact_version", None)
    if version is not None and artifact_version != version:
        return f"in-flight version is {artifact_version}, not {version}"
    state = getattr(snap, "state", None)
    if state not in _PRE_APPLY_CANCEL_STATES:
        return f"transaction is at {state}, past the pre-apply cancel window"
    if CommandType.CANCEL.value not in (device.capabilities or ()):
        return "device does not announce cancel capability"
    return None


async def _publish_cohort_cancels(
    session: AsyncSession,
    *,
    publisher: CommandPublisher,
    pending_publishes: list[tuple[Device, str, str, bool]],
    skipped: dict[str, str],
) -> list[CommandAccepted]:
    if not pending_publishes:
        return []
    messages = [
        (
            device.device_id,
            verb_payload(command_id=command_id, type=CommandType.CANCEL, cmd_id=cmd_id),
        )
        for device, command_id, cmd_id, _ in pending_publishes
    ]
    try:
        await publisher.publish_many(messages)
    except CommandPublishError:
        logger.exception("cohort cancel batch (%d devices) could not be published", len(messages))
        for device, command_id, _, _ in pending_publishes:
            await mark_publish_failed(session, command_id=command_id)
            skipped[device.device_id] = "broker publish failed"
        await session.commit()
        return []
    return [
        CommandAccepted(
            command_id=command_id,
            device_id=device.device_id,
            type=CommandType.CANCEL.value,
            cmd_id=cmd_id,
            device_online=online,
        )
        for device, command_id, cmd_id, online in pending_publishes
    ]


@router.post(
    "/{slug}/deploy/cancel",
    response_model=ProjectDeployCancelAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Cancel in-flight pre-apply deploys across a project or group cohort",
)
async def cancel_project_deploy(
    slug: str = Path(pattern=PROJECT_SLUG_PATTERN),
    *,
    body: ProjectDeployCancelRequest,
    admin: AdminDep,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
    publisher: CommandPublisherDep,
) -> ProjectDeployCancelAccepted:
    """Publish `cancel` commands to all cohort boards with an in-flight pre-apply deploy."""
    async with sessionmaker() as session:
        proj = await resolve_project_for_request(
            session,
            admin,
            slug,
            min_role=ProjectRole.OPERATOR,
            required_scope="deploy",
        )
        stmt = (
            select(Device)
            .where(Device.project_id == proj.id, Device.decommissioned_at.is_(None))
            .order_by(Device.device_id)
        )
        if body.group_id is not None:
            stmt = stmt.where(Device.group_id == body.group_id)
        all_devices = list((await session.scalars(stmt)).all())
        devices = (
            [d for d in all_devices if d.is_dev] if body.target_filter == "dev" else all_devices
        )
        if not devices:
            return ProjectDeployCancelAccepted(project=slug, cancelled=[], skipped={})

        snapshots = await latest_deploys(session, device_ids=[d.device_id for d in devices])
        sent_by = await _sender(session, admin)
        now = now_utc()

        skipped: dict[str, str] = {}
        pending_publishes: list[tuple[Device, str, str, bool]] = []
        for device in devices:
            snap = snapshots.get(device.device_id)
            reason = _cancel_skip_reason(device, snap, body.version)
            if reason is not None or snap is None or snap.cmd_id is None:
                skipped[device.device_id] = reason or "no in-flight transaction"
                continue
            command_id = new_command_id()
            await record_sent(
                session,
                command_id=command_id,
                device_id=device.device_id,
                type=CommandType.CANCEL.value,
                cmd_id=snap.cmd_id,
                sent_by=sent_by,
            )
            online = is_online(device, now=now, tolerance=settings.presence_tolerance)
            pending_publishes.append((device, command_id, snap.cmd_id, online))

        closed = await close_matching_rollouts(
            session,
            project_id=proj.id,
            version=body.version,
            target_filter=body.target_filter,
            target_chip=None,
            group_id=body.group_id,
            status_value="cancelled",
            reason=f"cancelled by {admin.subject}",
        )
        if not pending_publishes:
            if closed:
                await session.commit()
            return ProjectDeployCancelAccepted(project=slug, cancelled=[], skipped=skipped)

        await session.commit()
        cancelled = await _publish_cohort_cancels(
            session, publisher=publisher, pending_publishes=pending_publishes, skipped=skipped
        )

    return ProjectDeployCancelAccepted(
        project=slug,
        cancelled=cancelled,
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
                email=user.email,
                is_superadmin=user.is_superadmin,
                role=member.role,
                created_at=member.created_at,
            )
            for member, user in rows
        ]
    )


async def _owner_count(session: AsyncSession, project_id: object) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(ProjectMember)
        .where(
            ProjectMember.project_id == project_id,
            ProjectMember.role == ProjectRole.OWNER.value,
        )
    )
    return int(count or 0)


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
                if await _owner_count(session, proj.id) <= 1:
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
        email=user.email,
        is_superadmin=user.is_superadmin,
        role=member.role,  # type: ignore[arg-type]
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
        if existing.role == ProjectRole.OWNER.value and await _owner_count(session, proj.id) <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="cannot remove the last owner of a project",
            )
        await session.delete(existing)
        await session.commit()
