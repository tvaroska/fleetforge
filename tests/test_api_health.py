"""The API's two probes, and the one-origin invariant.

These run against the ASGI app in-process (`httpx.ASGITransport`) — no uvicorn, no
network. `test_healthz_ok` deliberately does **not** request the `engine` fixture:
liveness must answer with the database stopped, and depending on the fixture here
would hide a `/v1/healthz` that had quietly grown an I/O call.
"""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from fleetforge import __version__
from fleetforge.api.main import create_app
from fleetforge.db.base import get_sessionmaker


def client_for(app: FastAPI) -> httpx.AsyncClient:
    """An httpx client bound straight to the ASGI app."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


class FailingSession:
    """A session whose every statement fails the way a dead database does."""

    async def __aenter__(self) -> "FailingSession":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def execute(self, *args: Any, **kwargs: Any) -> None:
        raise OperationalError("SELECT 1", {}, OSError("connection refused"))


@pytest.fixture
def app_no_db() -> FastAPI:
    """The app with nothing overridden — usable without a database."""
    return create_app()


@pytest.fixture
def app_with_db(engine: AsyncEngine) -> FastAPI:
    """The app bound to the migrated test database.

    A dependency override, not an environment mutation: `get_settings` and
    `get_engine` are both `lru_cache`d, so poking `DATABASE_URL` into `os.environ`
    would either do nothing or poison every later test in the session.
    """
    app = create_app()
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    return app


async def test_healthz_ok(app_no_db: FastAPI) -> None:
    """Liveness answers 200 with the version, and touches no I/O."""
    async with client_for(app_no_db) as client:
        response = await client.get("/v1/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


async def test_readyz_ok(app_with_db: FastAPI) -> None:
    """Readiness answers 200 once the database is reachable."""
    async with client_for(app_with_db) as client:
        response = await client.get("/v1/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_readyz_is_503_when_database_is_unreachable(app_no_db: FastAPI) -> None:
    """An unreachable database is a 503 with a reason, never a bare 500."""
    app_no_db.dependency_overrides[get_sessionmaker] = lambda: FailingSession
    async with client_for(app_no_db) as client:
        response = await client.get("/v1/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "not-ready"


async def test_no_cors_headers(app_no_db: FastAPI) -> None:
    """The one-origin invariant.

    The dashboard is served from the same origin as the API (nginx proxies `/v1/*`
    to `api:8000`), so CORS must not exist. If this test ever fails because someone
    added `CORSMiddleware` to fix a browser error, the bug is in the nginx proxy —
    do not "fix" it here.
    """
    async with client_for(app_no_db) as client:
        response = await client.get("/v1/healthz", headers={"Origin": "https://evil.example.com"})
    lowered = {key.lower() for key in response.headers}
    assert "access-control-allow-origin" not in lowered
    assert "access-control-allow-credentials" not in lowered


async def test_openapi_is_versioned(app_no_db: FastAPI) -> None:
    """Every path lives under `/v1` — including the schema itself."""
    async with client_for(app_no_db) as client:
        response = await client.get("/v1/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert paths
    assert all(path.startswith("/v1") for path in paths), sorted(paths)
