"""Admin auth over HTTP — the CRITICAL.md surface.

Run against the real ASGI app and the real (migrated) test database, because the
properties that matter are exactly the ones a mocked session would hide: instant
revocation, expiry, and the fact that both transports take the same code path.

`base_url="https://testserver"` throughout: the session cookie is `Secure`, and
`http.cookiejar` drops a `Secure` cookie on a plain-http origin. The cookie flags
are the thing under test, so the test's URL scheme is what bends.
"""

import datetime as dt
import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fleetforge.api.deps import COOKIE_NAME
from fleetforge.auth.ratelimit import FixedWindowLimiter
from fleetforge.auth.tokens import ADMIN_TOKEN_PREFIX, issue_token
from fleetforge.config import get_settings
from fleetforge.db.models import AdminToken
from tests.conftest import TEST_PASSWORD, client_for, login_admin, settings_for_tests

BASE_URL = "https://testserver"


def _client(app: FastAPI) -> httpx.AsyncClient:
    return client_for(app, base_url=BASE_URL)


async def _login(app: FastAPI, password: str = TEST_PASSWORD) -> str:
    """Log in and return the raw token out of the `Set-Cookie` header."""
    return await login_admin(app, password)


def _sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


async def test_login_sets_cookie_with_exact_flags(admin_app: FastAPI) -> None:
    """The cookie is HttpOnly, Secure, SameSite=Strict, Path=/ — all four, always."""
    async with _client(admin_app) as client:
        response = await client.post("/v1/auth/login", json={"password": TEST_PASSWORD})

    assert response.status_code == 200
    raw = response.headers["set-cookie"]
    assert raw.startswith(f"{COOKIE_NAME}=ffa_")
    lowered = raw.lower()
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=strict" in lowered
    assert "path=/" in lowered
    assert "max-age=604800" in lowered  # 168 h, the configured session TTL


async def test_login_body_has_no_token(admin_app: FastAPI) -> None:
    """The token lives in the cookie *instead of* the body, so XSS cannot read it."""
    async with _client(admin_app) as client:
        response = await client.post("/v1/auth/login", json={"password": TEST_PASSWORD})

    body = response.json()
    assert "expires_at" in body
    assert "ffa_" not in response.text


async def test_login_wrong_password_401(admin_app: FastAPI) -> None:
    """A wrong password is a uniform 401 that sets no cookie."""
    async with _client(admin_app) as client:
        response = await client.post("/v1/auth/login", json={"password": "not-the-password"})

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid credentials"}
    assert "set-cookie" not in response.headers


async def test_login_unconfigured_503(app_with_db: FastAPI) -> None:
    """No ADMIN_PASSWORD_HASH is a 503, not a 500 and not an open door."""
    settings = settings_for_tests(admin_password_hash=None)
    app_with_db.dependency_overrides[get_settings] = lambda: settings

    async with _client(app_with_db) as client:
        response = await client.post("/v1/auth/login", json={"password": TEST_PASSWORD})

    assert response.status_code == 503
    assert response.json() == {"detail": "admin login is not configured"}


async def test_login_rate_limited(admin_app: FastAPI) -> None:
    """Failures inside the window refuse even the CORRECT password."""
    admin_app.state.login_limiter = FixedWindowLimiter(per_key=3, per_global=30, window_s=60)

    async with _client(admin_app) as client:
        for _ in range(3):
            failed = await client.post("/v1/auth/login", json={"password": "nope"})
            assert failed.status_code == 401

        response = await client.post("/v1/auth/login", json={"password": TEST_PASSWORD})

    assert response.status_code == 429
    assert int(response.headers["retry-after"]) >= 1


async def test_secret_is_not_stored_in_plaintext(admin_app: FastAPI, engine: AsyncEngine) -> None:
    """What lands in `admin_tokens` is an argon2id hash, never the secret."""
    token = await _login(admin_app)
    secret = token.split(".", 1)[1]

    async with _sessionmaker(engine)() as session:
        rows = (await session.execute(select(AdminToken.secret_hash))).scalars().all()

    assert rows
    for stored in rows:
        assert stored.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
        assert secret not in stored


# ---------------------------------------------------------------------------
# /me — one credential, two transports
# ---------------------------------------------------------------------------


async def test_me_requires_credential(admin_app: FastAPI) -> None:
    """No credential is a 401 that tells the client how to authenticate."""
    async with _client(admin_app) as client:
        response = await client.get("/v1/auth/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "invalid credentials"}


async def test_me_via_cookie_and_bearer_are_identical(admin_app: FastAPI) -> None:
    """`design/architecture.md`: one credential type, two transports, one code path."""
    async with _client(admin_app) as client:
        login = await client.post("/v1/auth/login", json={"password": TEST_PASSWORD})
        assert login.status_code == 200
        # The client jar now holds the cookie, exactly as a browser would.
        via_cookie = await client.get("/v1/auth/me")

    token = login.cookies[COOKIE_NAME]
    async with _client(admin_app) as client:
        via_bearer = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert via_cookie.status_code == 200
    assert via_bearer.status_code == 200
    assert via_cookie.json() == via_bearer.json()

    body = via_cookie.json()
    assert body["subject"] == "admin"
    assert body["scopes"] == ["admin"]
    assert uuid.UUID(body["token_id"])
    assert body["expires_at"] is not None


async def test_bearer_wins_over_cookie(admin_app: FastAPI) -> None:
    """A valid header authenticates even alongside a stale cookie."""
    good = await _login(admin_app)

    async with _client(admin_app) as client:
        client.cookies.set(COOKIE_NAME, "ffa_" + "0" * 32 + ".garbage", domain="testserver")
        response = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {good}"})

    assert response.status_code == 200


@pytest.mark.parametrize(
    "credential",
    [
        "garbage",
        "ffa_not-a-uuid.secret",
        f"ffa_{uuid.uuid4().hex}.{'x' * 43}",  # well-formed, unknown id
    ],
)
async def test_unusable_credentials_401(admin_app: FastAPI, credential: str) -> None:
    """Malformed and unknown credentials are the same uniform 401."""
    async with _client(admin_app) as client:
        response = await client.get(
            "/v1/auth/me", headers={"Authorization": f"Bearer {credential}"}
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid credentials"}


async def test_tampered_secret_401(admin_app: FastAPI) -> None:
    """One flipped character in the secret half is a 401."""
    token = await _login(admin_app)
    tampered = token[:-1] + ("X" if token[-1] != "X" else "Y")

    async with _client(admin_app) as client:
        response = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {tampered}"})

    assert response.status_code == 401


async def test_enrollment_prefix_rejected(admin_app: FastAPI) -> None:
    """The same bytes under the `ffe_` prefix must not authenticate an admin request.

    An enrollment token is a row of the same shape; the prefix is the namespace that
    keeps the two credential kinds apart (`DECISIONS.md` → token wire format).
    """
    token = await _login(admin_app)
    disguised = "ffe_" + token.removeprefix("ffa_")

    async with _client(admin_app) as client:
        response = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {disguised}"})

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Revocation and expiry — the load-bearing properties
# ---------------------------------------------------------------------------


async def test_revoked_token_401_immediately(admin_app: FastAPI, engine: AsyncEngine) -> None:
    """THE critical test: revocation takes effect on the very next request.

    The first request warms the verification cache. If the cache ever memoized the
    authorization rather than the hash comparison, the second request would still be
    a 200 — and `design/architecture.md`'s reason for rejecting JWT would be void.
    """
    token = await _login(admin_app)
    headers = {"Authorization": f"Bearer {token}"}

    async with _client(admin_app) as client:
        assert (await client.get("/v1/auth/me", headers=headers)).status_code == 200

        token_id = uuid.UUID(hex=token.removeprefix("ffa_").split(".", 1)[0])
        async with _sessionmaker(engine)() as session:
            await session.execute(
                text("UPDATE admin_tokens SET revoked_at = now() WHERE id = :id"),
                {"id": token_id},
            )
            await session.commit()

        after = await client.get("/v1/auth/me", headers=headers)

    assert after.status_code == 401


async def test_expired_token_401(admin_app: FastAPI, engine: AsyncEngine) -> None:
    """An expired row is rejected even though its secret is still correct."""
    issued = issue_token(ADMIN_TOKEN_PREFIX)
    async with _sessionmaker(engine)() as session:
        session.add(
            AdminToken(
                id=issued.token_id,
                name="expired",
                secret_hash=issued.secret_hash,
                subject="admin",
                scopes=["admin"],
                expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
            )
        )
        await session.commit()

    async with _client(admin_app) as client:
        response = await client.get(
            "/v1/auth/me", headers={"Authorization": f"Bearer {issued.token}"}
        )

    assert response.status_code == 401


async def test_logout_revokes_and_clears_cookie(admin_app: FastAPI, engine: AsyncEngine) -> None:
    """Logout revokes the row, clears the cookie with matching attributes, and sticks."""
    async with _client(admin_app) as client:
        login = await client.post("/v1/auth/login", json={"password": TEST_PASSWORD})
        assert login.status_code == 200
        token_id = uuid.UUID(hex=login.cookies[COOKIE_NAME].removeprefix("ffa_").split(".", 1)[0])

        logout = await client.post("/v1/auth/logout")
        assert logout.status_code == 204

        cleared = logout.headers["set-cookie"].lower()
        assert cleared.startswith(f"{COOKIE_NAME}=")
        for attribute in ("httponly", "secure", "samesite=strict", "path=/"):
            assert attribute in cleared

        # The httpx jar has dropped the cookie, so this is the "no credential" path;
        # the bearer form below proves the token itself is dead.
        assert (await client.get("/v1/auth/me")).status_code == 401

    async with _sessionmaker(engine)() as session:
        row = await session.get(AdminToken, token_id)
        assert row is not None
        assert row.revoked_at is not None


async def test_logout_kills_the_bearer_form_too(admin_app: FastAPI) -> None:
    """Revoking through the cookie transport also kills the header transport."""
    token = await _login(admin_app)
    headers = {"Authorization": f"Bearer {token}"}

    async with _client(admin_app) as client:
        assert (await client.post("/v1/auth/logout", headers=headers)).status_code == 204
        assert (await client.get("/v1/auth/me", headers=headers)).status_code == 401


# ---------------------------------------------------------------------------
# Telemetry and the one-origin invariant
# ---------------------------------------------------------------------------


async def test_last_used_at_throttled(admin_app: FastAPI, engine: AsyncEngine) -> None:
    """`last_used_at` is written at most once per throttle window, never per request."""
    token = await _login(admin_app)
    token_id = uuid.UUID(hex=token.removeprefix("ffa_").split(".", 1)[0])
    headers = {"Authorization": f"Bearer {token}"}
    sessionmaker = _sessionmaker(engine)

    async def read_last_used() -> dt.datetime | None:
        async with sessionmaker() as session:
            row = await session.get(AdminToken, token_id)
            assert row is not None
            await session.refresh(row)
            return row.last_used_at

    async with _client(admin_app) as client:
        await client.get("/v1/auth/me", headers=headers)
        first = await read_last_used()
        await client.get("/v1/auth/me", headers=headers)
        second = await read_last_used()

        assert first is not None
        assert second == first  # inside the 60 s throttle: no second write

        async with sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE admin_tokens SET last_used_at = now() - interval '1 hour' "
                    "WHERE id = :id"
                ),
                {"id": token_id},
            )
            await session.commit()

        await client.get("/v1/auth/me", headers=headers)

    third = await read_last_used()
    assert third is not None
    assert third > first - dt.timedelta(seconds=1)
    assert third != first


async def test_no_cors_headers_on_auth(admin_app: FastAPI) -> None:
    """The one-origin invariant holds on the cookie endpoints too.

    Cookie auth across origins is exactly the temptation `CORSMiddleware` exists for;
    the dashboard is same-origin by construction, so there is nothing to allow.
    """
    async with _client(admin_app) as client:
        response = await client.get("/v1/auth/me", headers={"Origin": "https://evil.example.com"})

    lowered = {key.lower() for key in response.headers}
    assert "access-control-allow-origin" not in lowered
    assert "access-control-allow-credentials" not in lowered


async def test_auth_routes_are_versioned(admin_app: FastAPI) -> None:
    """The three endpoints exist, under `/v1`, in the published schema."""
    async with _client(admin_app) as client:
        response = await client.get("/v1/openapi.json")

    paths = response.json()["paths"]
    assert {"/v1/auth/login", "/v1/auth/logout", "/v1/auth/me"} <= set(paths)
