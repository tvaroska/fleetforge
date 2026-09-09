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
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import text

from fleetforge.api.deps import (
    COOKIE_NAME,
    AdminDep,
    SessionMakerDep,
    SettingsDep,
    bearer_scheme,
    client_key,
    cookie_scheme,
    login_limiter,
    now_utc,
    token_cache,
)
from fleetforge.api.schemas import LoginRequest, LoginResponse, MeResponse
from fleetforge.auth.cache import VerifiedSecretCache
from fleetforge.auth.hashing import averify_secret
from fleetforge.auth.ratelimit import FixedWindowLimiter
from fleetforge.auth.tokens import ADMIN_TOKEN_PREFIX, aissue_token
from fleetforge.db.models import AdminToken

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


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Exchange the admin password for a session cookie",
)
async def login(
    request: Request,
    response: Response,
    body: LoginRequest,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
    limiter: LimiterDep,
) -> LoginResponse:
    """Verify the admin password and mint an `ffa_` token into an HttpOnly cookie.

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

    issued = await aissue_token(ADMIN_TOKEN_PREFIX)
    expires_at = now_utc() + dt.timedelta(hours=settings.session_ttl_hours)

    async with sessionmaker() as session:
        session.add(
            AdminToken(
                id=issued.token_id,
                name=f"dashboard session ({key})",
                secret_hash=issued.secret_hash,
                # The v1 constants, written explicitly rather than left to the
                # server defaults, so the reserved extension points are visible here.
                subject="admin",
                scopes=["admin"],
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
    logger.info("login succeeded for %s, token %s", key, issued.token_id)
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
async def me(admin: AdminDep) -> MeResponse:
    """Echo the authenticated context.

    The dashboard (`R0-fe-1`) calls this on load to choose between the login form and
    the fleet view, and it is the template every later protected route copies.
    """
    return MeResponse(
        token_id=admin.token_id,
        subject=admin.subject,
        scopes=list(admin.scopes),
        expires_at=admin.expires_at,
    )
