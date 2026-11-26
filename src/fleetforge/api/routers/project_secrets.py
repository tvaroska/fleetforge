"""Project rotating secrets endpoints — `/v1/projects/{slug}/secrets` (`SEC-be-1`, `T-24`).

Split from `routers/projects.py` so both routers stay well below `MAX_SOURCE_LINES` while
sharing the same `/v1/projects` prefix and bearer/cookie auth dependencies.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.api.deps import (
    AdminDep,
    CommandPublisherDep,
    SessionMakerDep,
    SettingsDep,
    bearer_scheme,
    cookie_scheme,
    now_utc,
    resolve_project_for_request,
)
from fleetforge.api.schemas import (
    PROJECT_SLUG_PATTERN,
    SECRET_KEY_PATTERN,
    ProjectSecretList,
    ProjectSecretSummary,
    ProjectSecretUpsert,
)
from fleetforge.broker import CommandPublisher, CommandPublishError, cfg_payload
from fleetforge.db.models import Device, Project, ProjectRole, ProjectSecret
from fleetforge.secrets_crypto import encrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/projects",
    tags=["projects"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)


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
    if not devices:
        return
    items = [
        (
            dev.device_id,
            cfg_payload(secrets_rev=secrets_rev, stay_awake=dev.stay_awake),
        )
        for dev in devices
    ]
    try:
        await publisher.publish_cfg_many(items)
    except CommandPublishError:
        for dev_id, payload in items:
            try:
                await publisher.publish_cfg(dev_id, payload)
            except CommandPublishError:
                logger.warning(
                    "dn/cfg publish skipped or failed for %s (secrets_rev=%d)",
                    dev_id,
                    secrets_rev,
                )


async def _project_secret_list(session: AsyncSession, proj: Project) -> ProjectSecretList:
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
        return await _project_secret_list(session, proj)


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
        rev, pid = proj.secrets_rev, proj.id
        summary = await _project_secret_list(session, proj)

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
        rev, pid = proj.secrets_rev, proj.id
        summary = await _project_secret_list(session, proj)

    await _publish_project_cfg(sessionmaker, publisher, pid, rev)
    return summary
