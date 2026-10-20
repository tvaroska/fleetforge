"""Admin authentication — `/v1/auth/login`, `/logout`, `/me`.

**No CSRF token, on purpose.** The dashboard and the API are one origin (nginx in
the `frontend` container serves the SPA and proxies `/v1/*` to `api:8000`;
`frontend/vite.config.ts` does the same in dev), so a `SameSite=Strict` cookie is
never attached to a cross-site request and there is nothing for a CSRF token to add.
The corollary is that CORS middleware must never appear — see `api/main.py`.

**Every login mints a row** in `admin_tokens`. That is intended: revocation is
per-session, so each session needs its own revocable row. Purging revoked/expired
rows after 90 days (`spec/prd.md` → *Retention*) is R1 work and does not exist yet.
"""

import datetime as dt
import logging
import re
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.api.deps import (
    COOKIE_NAME,
    FORBIDDEN,
    AdminDep,
    AuthContext,
    FirebaseAuthDep,
    SessionMakerDep,
    SettingsDep,
    bearer_scheme,
    client_key,
    cookie_scheme,
    login_limiter,
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
    FirebaseLoginRequest,
    LoginRequest,
    LoginResponse,
    MeResponse,
    UserProjectSummary,
)
from fleetforge.auth.cache import VerifiedSecretCache
from fleetforge.auth.firebase import (
    FirebaseAuthError,
    FirebaseIdentity,
    FirebaseInvalidCredentials,
)
from fleetforge.auth.hashing import averify_secret, dummy_verify
from fleetforge.auth.ratelimit import FixedWindowLimiter
from fleetforge.auth.tokens import ADMIN_TOKEN_PREFIX, aissue_token
from fleetforge.db.models import (
    AdminToken,
    Project,
    ProjectMember,
    ProjectRole,
    User,
)

logger = logging.getLogger(__name__)

# Set and cleared with EXACTLY these attributes: a `delete_cookie` whose path or
# samesite differs from the `set_cookie` leaves the original cookie in the browser.
#
# `Secure` is unconditional and there is no setting to turn it off. `http://localhost`
# is a secure context in both Chrome and Firefox, so the flag costs nothing in dev —
# and a `cookie_secure` knob is a switch somebody eventually flips in production.
#
# Considered and rejected: the `__Host-` cookie prefix. `Path=/` and `Secure` are
# already fixed and there are no subdomains, so it buys nothing, and its behaviour
# over `http://localhost` differs between browsers. Please do not re-litigate.
COOKIE_KWARGS: dict[str, Any] = {
    "httponly": True,
    "secure": True,
    "samesite": "strict",
    "path": "/",
}

router = APIRouter(prefix="/v1/auth", tags=["auth"])

# Conditional, so a second logout is a no-op rather than a moved timestamp.
REVOKE_SQL = text(
    "UPDATE admin_tokens SET revoked_at = now() WHERE id = :token_id AND revoked_at IS NULL"
)

LimiterDep = Annotated[FixedWindowLimiter, Depends(login_limiter)]
CacheDep = Annotated[VerifiedSecretCache, Depends(token_cache)]
ProjectSlugQuery = Annotated[str | None, Query(pattern=PROJECT_SLUG_PATTERN)]


async def visible_projects_for(
    session: AsyncSession, admin: AuthContext
) -> list[UserProjectSummary]:
    """Return all projects visible to `admin`, ordered by `slug`."""
    if admin.project_id is not None:
        proj = await session.get(Project, admin.project_id)
        if proj is None:
            return []
        return [
            UserProjectSummary(
                id=proj.id,
                slug=proj.slug,
                name=proj.name,
                description=proj.description,
                role="token",
            )
        ]

    if admin.is_superadmin:
        rows = (await session.scalars(select(Project).order_by(Project.slug))).all()
        return [
            UserProjectSummary(
                id=p.id,
                slug=p.slug,
                name=p.name,
                description=p.description,
                role=ProjectRole.OWNER.value,
            )
            for p in rows
        ]

    if admin.user_id is not None:
        stmt = (
            select(Project, ProjectMember.role)
            .join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(ProjectMember.user_id == admin.user_id)
            .order_by(Project.slug)
        )
        member_rows = (await session.execute(stmt)).all()
        return [
            UserProjectSummary(
                id=proj.id,
                slug=proj.slug,
                name=proj.name,
                description=proj.description,
                role=role,
            )
            for proj, role in member_rows
        ]

    rows = (await session.scalars(select(Project).order_by(Project.slug))).all()
    return [
        UserProjectSummary(
            id=p.id,
            slug=p.slug,
            name=p.name,
            description=p.description,
            role="token",
        )
        for p in rows
    ]


def _derive_username_from_email(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    cleaned = re.sub(r"[^a-z0-9._-]", "-", local).lstrip("._-")
    if len(cleaned) < 2:
        cleaned = f"user-{cleaned or 'fb'}"
    return cleaned[:24]


async def _resolve_invited_firebase_user(
    session: AsyncSession,
    identity: FirebaseIdentity,
    superadmin_emails: set[str],
    key: str,
    limiter: FixedWindowLimiter,
) -> User:
    """Resolve an invited `User` row for `identity` (or bootstrap a configured superadmin email).

    An unverified email is refused before anything else: Firebase self sign-up is open
    to anyone with the public web API key, so an unverified identity proves nothing
    about who owns the address (DECISIONS.md, 2026-10-19 S0-sec-1).
    """
    email = identity.email.strip().lower()
    if not identity.email_verified:
        limiter.record_failure(key)
        logger.info("firebase login rejected for unverified email %s from %s", email, key)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this email address has not been verified",
        )

    # uid first, then email: one `uid OR email` query returns an arbitrary row when
    # the two match different users.
    user = await session.scalar(select(User).where(User.firebase_uid == identity.uid))
    if user is None:
        user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        if email in superadmin_emails:
            base_username = _derive_username_from_email(email)
            candidate = base_username
            suffix = 1
            while (
                await session.scalar(select(User.id).where(User.username == candidate)) is not None
            ):
                suffix += 1
                candidate = f"{base_username[:20]}-{suffix}"
            user = User(
                username=candidate,
                email=email,
                firebase_uid=identity.uid,
                display_name=identity.display_name,
                password_hash=None,
                is_superadmin=True,
            )
            session.add(user)
            await session.flush()
        else:
            limiter.record_failure(key)
            logger.info("firebase login rejected for uninvited email %s from %s", email, key)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="this email has not been invited to Fleetforge",
            )

    if user.disabled_at is not None:
        limiter.record_failure(key)
        logger.info("firebase login rejected for disabled user %s from %s", user.username, key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.firebase_uid != identity.uid:
        if user.firebase_uid is not None and user.firebase_uid != identity.uid:
            limiter.record_failure(key)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="account is already bound to a different Firebase identity",
            )
        user.firebase_uid = identity.uid
    if user.display_name is None and identity.display_name:
        user.display_name = identity.display_name
    await session.flush()
    return user


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Exchange operator credentials for a session cookie",
)
async def login(
    request: Request,
    response: Response,
    body: LoginRequest,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
    limiter: LimiterDep,
    firebase: FirebaseAuthDep,
) -> LoginResponse:
    """Verify the operator password and mint an `ffa_` token into an HttpOnly cookie.

    The token is **never** in the response body and never in a log line. It is an
    ordinary admin token: the CLI form of the same credential is
    `Authorization: Bearer <that same string>`.
    """
    key = client_key(request)

    # Before any hashing: a flood must not cost argon2 CPU.
    if not limiter.allow(key):
        retry_after = limiter.retry_after(key)
        logger.warning("login rate-limited for %s", key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many login attempts",
            headers={"Retry-After": str(retry_after)},
        )

    identifier = body.username.strip().lower()
    user_id: uuid.UUID | None = None
    async with sessionmaker() as session:
        user = await session.scalar(
            select(User).where(or_(User.username == identifier, User.email == identifier))
        )

    if user is not None and user.disabled_at is not None:
        await dummy_verify()
        limiter.record_failure(key)
        logger.info("login rejected for disabled user %s from %s", identifier, key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    authenticated_via_local = False
    if user is not None and user.password_hash is not None:
        if await averify_secret(body.password, user.password_hash):
            authenticated_via_local = True
            user_id = user.id
            subject = user.username
        elif not (user.email and settings.firebase_web_api_key):
            limiter.record_failure(key)
            logger.info("login failed for user %s from %s", identifier, key)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

    if not authenticated_via_local:
        firebase_email = (
            user.email
            if (user is not None and user.email)
            else (identifier if "@" in identifier else None)
        )
        if firebase_email is not None and settings.firebase_web_api_key:
            try:
                fb_identity = await firebase.sign_in_with_password(
                    firebase_email,
                    body.password,
                    api_key=settings.firebase_web_api_key,
                    project_id=settings.firebase_project_id,
                )
            except FirebaseInvalidCredentials:
                limiter.record_failure(key)
                logger.info("firebase login failed for %s from %s", firebase_email, key)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="invalid credentials",
                    headers={"WWW-Authenticate": "Bearer"},
                ) from None
            except FirebaseAuthError:
                logger.exception("firebase authentication upstream failure")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Firebase authentication service is unavailable",
                ) from None

            async with sessionmaker() as session:
                resolved = await _resolve_invited_firebase_user(
                    session,
                    fb_identity,
                    settings.firebase_superadmin_email_set(),
                    key,
                    limiter,
                )
                user_id = resolved.id
                subject = resolved.username
                await session.commit()
        elif user is None and identifier == "admin":
            if settings.admin_password_hash is None:
                logger.error("login attempted but ADMIN_PASSWORD_HASH is not set")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="admin login is not configured",
                )
            if not await averify_secret(body.password, settings.admin_password_hash):
                limiter.record_failure(key)
                logger.info("login failed for %s", key)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="invalid credentials",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            subject = "admin"
        else:
            await dummy_verify()
            limiter.record_failure(key)
            logger.info("login failed for unknown user %s from %s", identifier, key)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

    issued = await aissue_token(ADMIN_TOKEN_PREFIX)
    expires_at = now_utc() + dt.timedelta(hours=settings.session_ttl_hours)

    async with sessionmaker() as session:
        session.add(
            AdminToken(
                id=issued.token_id,
                name=f"dashboard session ({key})",
                secret_hash=issued.secret_hash,
                subject=subject,
                scopes=["admin"],
                user_id=user_id,
                expires_at=expires_at,
            )
        )
        await session.commit()

    response.set_cookie(
        COOKIE_NAME,
        issued.token,
        max_age=settings.session_ttl_hours * 3600,
        **COOKIE_KWARGS,
    )
    logger.info("login succeeded for %s (%s), token %s", subject, key, issued.token_id)
    return LoginResponse(expires_at=expires_at)


@router.post(
    "/firebase",
    response_model=LoginResponse,
    summary="Exchange a verified Firebase ID token for an HttpOnly session cookie (invite-only)",
)
async def login_firebase(
    request: Request,
    response: Response,
    body: FirebaseLoginRequest,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
    limiter: LimiterDep,
    firebase: FirebaseAuthDep,
) -> LoginResponse:
    """Verify an RS256 Firebase ID token and mint an `ff_session` cookie for an invited user."""
    key = client_key(request)
    if not limiter.allow(key):
        retry_after = limiter.retry_after(key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many login attempts",
            headers={"Retry-After": str(retry_after)},
        )

    # The web API key is needed for the revocation lookup; without it, fail closed.
    if not settings.firebase_project_id or not settings.firebase_web_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Firebase login is not configured",
        )

    try:
        fb_identity = await firebase.verify_id_token(
            body.id_token,
            settings.firebase_project_id,
            api_key=settings.firebase_web_api_key,
            check_revoked=True,
        )
    except FirebaseInvalidCredentials:
        limiter.record_failure(key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except FirebaseAuthError:
        logger.exception("firebase ID token verification failure")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Firebase authentication service is unavailable",
        ) from None

    async with sessionmaker() as session:
        resolved = await _resolve_invited_firebase_user(
            session,
            fb_identity,
            settings.firebase_superadmin_email_set(),
            key,
            limiter,
        )
        user_id = resolved.id
        subject = resolved.username
        await session.commit()

    issued = await aissue_token(ADMIN_TOKEN_PREFIX)
    expires_at = now_utc() + dt.timedelta(hours=settings.session_ttl_hours)

    async with sessionmaker() as session:
        session.add(
            AdminToken(
                id=issued.token_id,
                name=f"firebase session ({key})",
                secret_hash=issued.secret_hash,
                subject=subject,
                scopes=["admin"],
                user_id=user_id,
                expires_at=expires_at,
            )
        )
        await session.commit()

    response.set_cookie(
        COOKIE_NAME,
        issued.token,
        max_age=settings.session_ttl_hours * 3600,
        **COOKIE_KWARGS,
    )
    logger.info("firebase login succeeded for %s (%s), token %s", subject, key, issued.token_id)
    return LoginResponse(expires_at=expires_at)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the presenting token and clear the cookie",
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)
async def logout(
    response: Response,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
    cache: CacheDep,
) -> None:
    """Revoke this session. The token is dead on the very next request, everywhere."""
    async with sessionmaker() as session:
        await session.execute(REVOKE_SQL, {"token_id": admin.token_id})
        await session.commit()

    cache.forget(admin.token_id)
    response.delete_cookie(COOKIE_NAME, **COOKIE_KWARGS)
    logger.info("logout: token %s revoked", admin.token_id)


@router.get(
    "/me",
    response_model=MeResponse,
    summary="The identity behind the presented credential",
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)
async def me(admin: AdminDep, sessionmaker: SessionMakerDep) -> MeResponse:
    """Echo the authenticated context and visible projects."""
    async with sessionmaker() as session:
        projects = await visible_projects_for(session, admin)
    return MeResponse(
        token_id=admin.token_id,
        subject=admin.subject,
        scopes=list(admin.scopes),
        expires_at=admin.expires_at,
        is_superadmin=admin.is_superadmin,
        project_id=admin.project_id,
        projects=projects,
    )


@router.post(
    "/tokens",
    status_code=status.HTTP_201_CREATED,
    response_model=AdminTokenIssued,
    summary="Mint a named or project-scoped ffa_ token (plaintext returned once)",
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)
async def create_admin_token(
    body: AdminTokenCreate,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> AdminTokenIssued:
    """Mint a new `ffa_...` API token.

    A project-pinned token requires `owner` on that project; an unpinned token
    requires `is_superadmin`. A scoped machine token cannot mint broader tokens.
    """
    if "admin" not in admin.scopes:
        raise FORBIDDEN

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
        if not admin.is_superadmin and row.user_id != admin.user_id:
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
