"""Shared FastAPI dependencies — and the one admin-credential verification path.

`design/architecture.md` → *v1 admin auth — one credential type, two transports*:
the dashboard's cookie holds **the same `ffa_` token** a CLI would send in
`Authorization: Bearer`, and both converge on `require_admin` below. One path means
one place where revocation, expiry and hashing are enforced.

Every protected route added later (`R0-be-2`, `R0-be-5`, `R0-fe-*`'s backends) hangs
off `AdminDep` and copies nothing.
"""

import datetime as dt
import ipaddress
import logging
import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyCookie, HTTPBearer
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fleetforge.auth.cache import VerifiedSecretCache
from fleetforge.auth.hashing import averify_secret, dummy_verify
from fleetforge.auth.ratelimit import FixedWindowLimiter
from fleetforge.auth.tokens import ADMIN_TOKEN_PREFIX, parse_token
from fleetforge.config import Settings, get_settings
from fleetforge.db.base import get_sessionmaker
from fleetforge.db.models import AdminToken

logger = logging.getLogger(__name__)

COOKIE_NAME = "ff_session"

# Every rejection looks like this one. Nothing in the response says whether the
# token was unknown, malformed, expired or revoked — the distinction goes to the log
# with the token *id* only, never the secret.
UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="invalid credentials",
    headers={"WWW-Authenticate": "Bearer"},
)

SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionMakerDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]

# Declared only so `/v1/docs` renders an Authorize button for both transports; every
# protected router lists them in `dependencies=[...]`. They live here rather than in
# a router because there is one credential and there must be one declaration of it.
# `auto_error=False` on both is essential: with the default, FastAPI would 403 before
# `require_admin` ever runs, and the actual extraction must stay in one place.
bearer_scheme = HTTPBearer(auto_error=False, description="Admin token, `ffa_…`")
cookie_scheme = APIKeyCookie(name=COOKIE_NAME, auto_error=False, description="Login cookie")

# One conditional UPDATE, no read-then-write: `last_used_at` is telemetry, and
# writing it on every request would turn every authenticated read into a write.
TOUCH_LAST_USED_SQL = text(
    "UPDATE admin_tokens SET last_used_at = now() "
    "WHERE id = :token_id "
    "AND (last_used_at IS NULL OR last_used_at < now() - make_interval(secs => :throttle))"
)


def now_utc() -> dt.datetime:
    """Timezone-aware UTC. Every timestamp column is TIMESTAMPTZ; naive comparison raises."""
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True, slots=True)
class AuthContext:
    """Who the caller is. `subject`/`scopes` are reserved — always admin in v1."""

    token_id: uuid.UUID
    subject: str
    scopes: tuple[str, ...]
    expires_at: dt.datetime | None


def client_key(request: Request) -> str:
    """A rate-limiting key for the caller.

    The API never sees the client directly: the only path in is Traefik → nginx →
    `api:8000`, and nginx sets `X-Forwarded-For $proxy_add_x_forwarded_for`, so the
    **leftmost** entry is the client IP as Traefik saw it. Traefik *appends* rather
    than replaces, which makes that entry client-spoofable — hence the global
    backstop bucket in `auth/ratelimit.py`. The value is validated as an IP address
    before use: an unvalidated header is unbounded key cardinality, i.e. a memory
    leak in the limiter.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    candidate = forwarded.split(",")[0].strip()
    if candidate:
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            logger.debug("ignoring unparseable X-Forwarded-For entry")
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


def bearer_token(authorization: str | None) -> str | None:
    """Extract the credential from an `Authorization: Bearer <token>` header."""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def token_cache(request: Request) -> VerifiedSecretCache:
    """The per-app verification cache, created in `create_app()`."""
    cache: VerifiedSecretCache = request.app.state.token_cache
    return cache


def login_limiter(request: Request) -> FixedWindowLimiter:
    """The per-app login rate limiter, created in `create_app()`."""
    limiter: FixedWindowLimiter = request.app.state.login_limiter
    return limiter


async def _touch_last_used(
    sessionmaker: async_sessionmaker[AsyncSession], token_id: uuid.UUID, throttle_s: int
) -> None:
    """Best-effort, throttled `last_used_at` update. Never fails the request."""
    try:
        async with sessionmaker() as session:
            await session.execute(
                TOUCH_LAST_USED_SQL, {"token_id": token_id, "throttle": throttle_s}
            )
            await session.commit()
    except (SQLAlchemyError, OSError) as exc:  # pragma: no cover - telemetry, not authorization
        logger.warning("could not update last_used_at for token %s: %s", token_id, exc)


async def require_admin(
    request: Request,
    sessionmaker: SessionMakerDep,
    settings: SettingsDep,
) -> AuthContext:
    """Authenticate an admin credential from the header or the session cookie.

    The order of checks is load-bearing:
    parse → row → `revoked_at`/`expires_at` → secret verify. Revocation is checked
    **before** the argon2 verification, so a revoked token is both instantly dead and
    unable to burn CPU. The verification cache short-circuits only the hash
    comparison — the row is read, and its state re-checked, on every request.
    """
    raw = bearer_token(request.headers.get("authorization")) or request.cookies.get(COOKIE_NAME)
    parts = parse_token(raw, ADMIN_TOKEN_PREFIX)
    if parts is None:
        logger.info("auth rejected: no usable admin credential presented")
        raise UNAUTHORIZED

    async with sessionmaker() as session:
        row = await session.get(AdminToken, parts.token_id)

        if row is None:
            # Burn a verification so an unknown id costs about what a wrong secret does.
            await dummy_verify()
            logger.info("auth rejected: unknown token id %s", parts.token_id)
            raise UNAUTHORIZED

        if row.revoked_at is not None:
            logger.info("auth rejected: token %s is revoked", row.id)
            raise UNAUTHORIZED

        if row.expires_at is not None and row.expires_at <= now_utc():
            logger.info("auth rejected: token %s is expired", row.id)
            raise UNAUTHORIZED

        context = AuthContext(
            token_id=row.id,
            subject=row.subject,
            scopes=tuple(row.scopes),
            expires_at=row.expires_at,
        )
        secret_hash = row.secret_hash

    cache = token_cache(request)
    if not cache.check(parts.token_id, parts.secret):
        if not await averify_secret(parts.secret, secret_hash):
            logger.info("auth rejected: bad secret for token %s", parts.token_id)
            raise UNAUTHORIZED
        cache.remember(parts.token_id, parts.secret, ttl=settings.verify_cache_ttl_s)

    await _touch_last_used(sessionmaker, parts.token_id, settings.last_used_throttle_s)
    return context


AdminDep = Annotated[AuthContext, Depends(require_admin)]
