"""Admin API token management — `/v1/auth/tokens` and `/v1/auth/tokens/{id}/revoke`."""

import datetime as dt
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, text

from fleetforge.api.deps import (
    FORBIDDEN,
    AdminDep,
    SessionMakerDep,
    UnpinnedAdminDep,
    bearer_scheme,
    cookie_scheme,
    now_utc,
    resolve_project_for_request,
    token_cache,
)
from fleetforge.api.schemas import (
    PROJECT_SLUG_PATTERN,
    AdminTokenCreate,
    AdminTokenIssued,
    AdminTokenList,
    AdminTokenSummary,
)
from fleetforge.auth.cache import VerifiedSecretCache
from fleetforge.auth.tokens import ADMIN_TOKEN_PREFIX, aissue_token
from fleetforge.db.models import AdminToken, Project, ProjectMember, ProjectRole

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

REVOKE_SQL = text(
    "UPDATE admin_tokens SET revoked_at = now() WHERE id = :token_id AND revoked_at IS NULL"
)

CacheDep = Annotated[VerifiedSecretCache, Depends(token_cache)]
ProjectSlugQuery = Annotated[str | None, Query(pattern=PROJECT_SLUG_PATTERN)]


@router.post(
    "/tokens",
    status_code=status.HTTP_201_CREATED,
    response_model=AdminTokenIssued,
    summary="Mint a named or project-scoped ffa_ token (plaintext returned once)",
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)
async def create_admin_token(
    body: AdminTokenCreate,
    admin: UnpinnedAdminDep,
    sessionmaker: SessionMakerDep,
) -> AdminTokenIssued:
    """Mint a new `ffa_...` API token.

    - the caller must be unpinned: a project-pinned token never mints (S0-sec-2), so a
      CI token cannot leave children behind that outlive its own revocation. Sessions
      (password and Firebase login) are always unpinned, so the dashboard still mints;
    - minting a token pinned to a project requires `owner` on that project;
    - minting an unpinned token requires `is_superadmin`.

    A scoped (non-`admin`) machine token cannot mint at all.
    """
    now = now_utc()
    expires_at = (
        now + dt.timedelta(days=body.expires_in_days) if body.expires_in_days is not None else None
    )

    async with sessionmaker() as session:
        target_project: Project | None = None
        if body.project is not None:
            target_project = await resolve_project_for_request(
                session,
                admin,
                body.project,
                min_role=ProjectRole.OWNER,
            )
        elif not admin.is_superadmin:
            raise FORBIDDEN

        issued = await aissue_token(ADMIN_TOKEN_PREFIX)
        row = AdminToken(
            id=issued.token_id,
            name=body.name,
            secret_hash=issued.secret_hash,
            subject=admin.subject,
            scopes=[str(s) for s in body.scopes],
            user_id=admin.user_id,
            project_id=target_project.id if target_project is not None else None,
            created_at=now,
            expires_at=expires_at,
        )
        session.add(row)
        await session.commit()

    return AdminTokenIssued(
        id=issued.token_id,
        token=issued.token,
        name=body.name,
        subject=admin.subject,
        scopes=[str(s) for s in body.scopes],
        project_id=target_project.id if target_project is not None else None,
        project_slug=target_project.slug if target_project is not None else None,
        created_at=now,
        expires_at=expires_at,
    )


@router.get(
    "/tokens",
    response_model=AdminTokenList,
    summary="List issued ffa_ tokens (never includes plaintext or secret_hash)",
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)
async def list_admin_tokens(
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
    project: ProjectSlugQuery = None,
) -> AdminTokenList:
    """List tokens for a project (requires `owner`) or for the current caller."""
    async with sessionmaker() as session:
        if project is not None:
            target_project = await resolve_project_for_request(
                session, admin, project, min_role=ProjectRole.OWNER
            )
            stmt = (
                select(AdminToken, Project.slug)
                .outerjoin(Project, Project.id == AdminToken.project_id)
                .where(AdminToken.project_id == target_project.id)
                .order_by(AdminToken.created_at.desc())
                .limit(200)
            )
        elif admin.is_superadmin:
            stmt = (
                select(AdminToken, Project.slug)
                .outerjoin(Project, Project.id == AdminToken.project_id)
                .order_by(AdminToken.created_at.desc())
                .limit(200)
            )
        elif admin.user_id is not None:
            stmt = (
                select(AdminToken, Project.slug)
                .outerjoin(Project, Project.id == AdminToken.project_id)
                .where(AdminToken.user_id == admin.user_id)
                .order_by(AdminToken.created_at.desc())
                .limit(200)
            )
        else:
            stmt = (
                select(AdminToken, Project.slug)
                .outerjoin(Project, Project.id == AdminToken.project_id)
                .where(AdminToken.id == admin.token_id)
            )

        rows = (await session.execute(stmt)).all()

    return AdminTokenList(
        tokens=[
            AdminTokenSummary(
                id=tok.id,
                name=tok.name,
                subject=tok.subject,
                scopes=list(tok.scopes),
                project_id=tok.project_id,
                project_slug=slug,
                created_at=tok.created_at,
                last_used_at=tok.last_used_at,
                expires_at=tok.expires_at,
                revoked_at=tok.revoked_at,
            )
            for tok, slug in rows
        ]
    )


@router.post(
    "/tokens/{token_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an ffa_ token by id",
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)
async def revoke_admin_token(
    token_id: uuid.UUID,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
    cache: CacheDep,
) -> None:
    """Revoke a token immediately and evict it from the verification cache."""
    if "admin" not in admin.scopes:
        raise FORBIDDEN

    async with sessionmaker() as session:
        row = await session.get(AdminToken, token_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="token not found",
            )
        own = row.id == admin.token_id or (
            admin.user_id is not None and row.user_id == admin.user_id
        )
        if (
            not admin.is_superadmin
            and admin.project_id is not None
            and row.project_id != admin.project_id
        ):
            raise FORBIDDEN
        if not admin.is_superadmin and not own:
            if row.project_id is None:
                raise FORBIDDEN
            member = (
                await session.get(ProjectMember, (row.project_id, admin.user_id))
                if admin.user_id is not None
                else None
            )
            if member is None or member.role != ProjectRole.OWNER.value:
                raise FORBIDDEN

        await session.execute(REVOKE_SQL, {"token_id": token_id})
        await session.commit()

    cache.forget(token_id)
    logger.info("admin token %s revoked by %s", token_id, admin.subject)
