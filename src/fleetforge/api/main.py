"""FastAPI application.

R0-infra-1 ships only what the Compose stack needs to prove itself alive:
the app factory, `GET /v1/healthz` (liveness) and `GET /v1/readyz` (readiness).
R0-be-1 adds admin auth, `/v1/auth/login` and the real routers; R0-be-5 adds SSE.
Keep the app factory — the tests and R0-be-1 both build on it.

**There is no CORS middleware here, and there must never be one.** The dashboard
and the API are served from a single origin: nginx in the `frontend` container
serves the SPA and proxies `/v1/*` to `api:8000` (`design/architecture.md` →
"Serve the dashboard and the API from one origin … no CORS configuration"; the
topology is in `design/production.md` → *Same origin, two backends*). A CORS error
in a browser therefore means the nginx proxy is misconfigured, **not** that CORS
middleware is missing. `tests/test_api_health.py::test_no_cors_headers` guards this.
"""

import logging
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fleetforge import __version__
from fleetforge.db.base import get_sessionmaker

logger = logging.getLogger(__name__)

SessionMaker = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]


def configure_logging(level: int = logging.INFO) -> None:
    """Send uvicorn's and the app's logs to stdout in one line-per-event shape.

    Containers log to stdout; the platform (Docker, later Loki) owns rotation and
    aggregation. Called from `create_app()` so both `uvicorn fleetforge.api.main:app`
    and the tests get the same configuration.
    """
    logging.basicConfig(
        level=level,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        force=True,
    )


def create_app() -> FastAPI:
    """Build the ASGI application.

    Everything is mounted under `/v1` — including the docs and the OpenAPI schema —
    because `spec/device-protocol.md` versions the wire contract and the dashboard,
    the CLI and the agent all resolve against the same prefix.
    """
    configure_logging()

    app = FastAPI(
        title="Fleetforge",
        version=__version__,
        summary="OTA firmware management for embedded fleets",
        docs_url="/v1/docs",
        redoc_url=None,
        openapi_url="/v1/openapi.json",
    )

    @app.get("/v1/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness: is the process running?

        **No I/O.** A liveness probe that touches the database restarts the API
        every time the database blips, which turns a recoverable outage into a
        crash loop. Readiness is what depends on the database — see `/v1/readyz`.
        """
        return {"status": "ok", "version": __version__}

    @app.get("/v1/readyz", tags=["health"])
    async def readyz(sessionmaker: SessionMaker) -> Any:
        """Readiness: can the process serve traffic (i.e. reach its database)?

        Returns 503 with a reason rather than raising — a bare 500 from a probe
        tells an operator nothing and looks like an application bug.
        """
        try:
            async with sessionmaker() as session:
                await session.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError) as exc:
            logger.warning("readyz: database unreachable: %s", exc.__class__.__name__)
            return JSONResponse(
                status_code=503,
                content={"status": "not-ready", "detail": "database unreachable"},
            )
        return {"status": "ready"}

    return app


app = create_app()
