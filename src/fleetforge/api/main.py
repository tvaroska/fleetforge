"""FastAPI application.

R0-infra-1 shipped the app factory plus `GET /v1/healthz` (liveness) and
`GET /v1/readyz` (readiness). R0-be-1 added admin auth (`/v1/auth/*`) and the
router/dependency layout every later endpoint copies; R0-be-2 added enrollment token
issuance (`/v1/enrollment-tokens`); R0-be-4 added the device-facing `POST /v1/enroll`
— the one **unauthenticated write** endpoint, whose credential is the token in its
body; R0-be-5 added the SSE event stream (`GET /v1/events`) and the fleet read model
(`GET /v1/devices`) it tells clients to re-read. R0-be-6 added the artifact object-store
seam (`ObjectStoreDep`). R0-infra-2 added the prebuilt agent images (`/v1/agent/*`) and
S0-infra-6 moved them into that same store: `app.state.agent_catalog` is a lazy
`CatalogCache`, not a directory scan, so the object store is now what **onboarding**
depends on as well as R1 — hence the startup WARNING when neither backend is configured.
R1-be-1 added `POST /v1/artifact`, the first endpoint that **writes** to that store and
the first writer of the `artifacts` table. R1-be-2 added
`POST /v1/devices/{device_id}/deploy` — the first endpoint that **publishes** to the
broker (`CommandPublisherDep`) and the only writer of `deploy_events` (`deploys.py`),
hence the fourth startup WARNING.

**The lifespan owns one background task**: the `ff_events` `LISTEN` connection
(`api/eventstream.py::PostgresEventListener`), one per API process, feeding the
per-app `EventHub`. It starts only when settings exist, because `create_app()` must
stay constructible with no environment at all — see `_settings_or_none()`.

**There is no CORS middleware here, and there must never be one.** The dashboard
and the API are served from a single origin: nginx in the `frontend` container
serves the SPA and proxies `/v1/*` to `api:8000` (`design/architecture.md` →
"Serve the dashboard and the API from one origin … no CORS configuration"; the
topology is in `design/production.md` → *Same origin, two backends*). A CORS error
in a browser therefore means the nginx proxy is misconfigured, **not** that CORS
middleware is missing. `tests/test_api_health.py::test_no_cors_headers` guards this.
The login cookie is same-origin by construction, which is also why
`SameSite=Strict` alone is sufficient against CSRF and no CSRF token exists.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fleetforge import __version__
from fleetforge.api.deps import dynsec_configured, mqtt_command_configured
from fleetforge.api.eventstream import EventHub, PostgresEventListener
from fleetforge.api.routers import (
    agent,
    artifacts,
    auth,
    deploys,
    devices,
    enroll,
    enrollment,
    events,
    progress,
)
from fleetforge.auth.cache import VerifiedSecretCache
from fleetforge.auth.ratelimit import FixedWindowLimiter
from fleetforge.config import Settings, get_settings
from fleetforge.db.base import asyncpg_dsn, get_sessionmaker
from fleetforge.firmware import DEFAULT_INDEX_KEY, CatalogCache
from fleetforge.storage.factory import object_store_configured

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


def _settings_or_none() -> Settings | None:
    """Settings if the environment provides them, else `None`.

    `create_app()` must stay constructible with no environment at all — the health
    tests build the app without a database, and a factory that raises on import is
    a container that cannot even serve `/v1/healthz` to say why.
    """
    try:
        return get_settings()
    except ValidationError as exc:
        logger.warning("settings unavailable (%s); falling back to defaults", exc.error_count())
        return None


def create_app() -> FastAPI:
    """Build the ASGI application.

    Everything is mounted under `/v1` — including the docs and the OpenAPI schema —
    because `spec/device-protocol.md` versions the wire contract and the dashboard,
    the CLI and the agent all resolve against the same prefix.
    """
    configure_logging()

    # Per-app, never module-level: each test app gets a fresh cache and a fresh
    # limiter, so no auth state leaks between tests (and `dependency_overrides` on
    # `get_settings` cannot be undermined by a shared singleton).
    settings = _settings_or_none()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Own the one `ff_events` LISTEN connection for this process.

        `httpx`'s `ASGITransport` never runs a lifespan, which is deliberate here:
        test apps get a hub and no listener, so nothing dials
        `settings_for_tests()`'s intentionally unusable DSN. The listener has its own
        integration test against the real test database.
        """
        hub: EventHub = app.state.event_hub
        task: asyncio.Task[None] | None = None
        if settings is not None:
            listener = PostgresEventListener(
                asyncpg_dsn(settings.database_url),
                hub,
                ping_s=settings.events_listener_ping_s,
            )
            app.state.event_listener = listener
            task = asyncio.create_task(listener.run(), name="ff-events-listener")
        else:
            logger.warning(
                "no settings: the ff_events listener is not running, so /v1/events would "
                "carry no device events. Same cause as the DATABASE_URL warning above."
            )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            hub.close_all()

    app = FastAPI(
        title="Fleetforge",
        version=__version__,
        summary="OTA firmware management for embedded fleets",
        docs_url="/v1/docs",
        redoc_url=None,
        openapi_url="/v1/openapi.json",
        lifespan=lifespan,
    )

    app.state.token_cache = VerifiedSecretCache()
    app.state.login_limiter = FixedWindowLimiter(
        per_key=settings.login_rate_limit_per_ip if settings else 5,
        per_global=settings.login_rate_limit_global if settings else 30,
        window_s=settings.login_rate_limit_window_s if settings else 60,
    )
    # `/v1/enroll` is unauthenticated, public and does one argon2 verification per
    # request, so it gets its OWN bucket: a fleet behind one NAT rebooting together
    # must not be able to lock the operator out of /v1/auth/login, or the reverse.
    app.state.enroll_limiter = FixedWindowLimiter(
        per_key=settings.enroll_rate_limit_per_ip if settings else 10,
        per_global=settings.enroll_rate_limit_global if settings else 60,
        window_s=settings.login_rate_limit_window_s if settings else 60,
    )
    # Progress reports (S0-fw-1) are chattier still — five-plus per boot — and get a
    # third bucket plus their own verification cache, so a reporting fleet can spend
    # neither the enroll budget nor the login one. See `deps.progress_limiter`.
    app.state.progress_limiter = FixedWindowLimiter(
        per_key=settings.progress_rate_limit_per_ip if settings else 60,
        per_global=settings.progress_rate_limit_global if settings else 300,
        window_s=settings.login_rate_limit_window_s if settings else 60,
    )
    app.state.progress_cache = VerifiedSecretCache()
    # The SSE fan-out. Created here (not in the lifespan) so a test app that never
    # runs a lifespan still has one to publish into.
    app.state.event_hub = EventHub(
        queue_size=settings.sse_queue_size if settings else 200,
        max_subscribers=settings.sse_max_clients if settings else 20,
    )
    # The prebuilt agent images (R0-infra-2), read from the object store (S0-infra-6).
    # Lazy and per app, like the caches above: **constructing this does no I/O**, because
    # a container must start when the bucket is slow or down. The index is re-read at most
    # once per TTL, which is how a `just agent-publish` reaches the flasher with nothing
    # restarted.
    app.state.agent_catalog = CatalogCache(
        ttl_s=settings.agent_catalog_ttl_s if settings else 60.0,
        index_key=settings.agent_index_key if settings else DEFAULT_INDEX_KEY,
    )
    if settings is not None and settings.admin_password_hash is None:
        logger.warning(
            "ADMIN_PASSWORD_HASH is not set: /v1/auth/login will answer 503. "
            "Mint one with `just admin-password` and put the SINGLE-QUOTED line in .env."
        )
    if settings is not None and not object_store_configured(settings):
        logger.warning(
            "no object store configured (S3_* or GCS_*): ONBOARDING as well as artifact "
            "upload and deploy (R1) will answer 503 — since S0-infra-6 the agent bundles "
            "the flasher writes live in the store, not in this image. The dev stack sets "
            "S3_* on the api service; production sets GCS_BUCKET + "
            "GCS_IMPERSONATE_SERVICE_ACCOUNT. Round-trip it with `just storage-check`."
        )
    if settings is not None and not mqtt_command_configured(settings):
        logger.warning(
            "MQTT_COMMAND_USERNAME/_PASSWORD are not set: POST /v1/devices/{id}/deploy "
            "will answer 503, because nothing may publish dn/cmd without the `commander` "
            "broker credential. The dev stack sets both on the api service; the broker "
            "side is `mosquitto/bootstrap.sh`. Prove the path with `just broker-check`."
        )
    if settings is not None and not dynsec_configured(settings):
        logger.warning(
            "MQTT_DYNSEC_USERNAME/_PASSWORD are not set: enrolled devices get a broker "
            "password nothing has been told about, and broker_provisioned_at stays NULL. "
            "Correct until R0-sec-1 secures the broker; reconcile afterwards with "
            "SELECT device_id FROM devices WHERE broker_provisioned_at IS NULL."
        )

    app.include_router(auth.router)
    app.include_router(enrollment.router)
    app.include_router(enroll.router)
    app.include_router(events.router)
    app.include_router(devices.router)
    app.include_router(agent.router)
    app.include_router(progress.router)
    app.include_router(artifacts.router)
    app.include_router(deploys.router)

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
