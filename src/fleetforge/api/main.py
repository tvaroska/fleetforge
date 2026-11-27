"""FastAPI application factory (`create_app`), router wiring, and `/v1/healthz` + `/v1/readyz`.

Everything is mounted under `/v1` — including OpenAPI docs — matching `spec/device-protocol.md`.
There is deliberately no CORS middleware: the dashboard SPA and API share one origin behind nginx
(`tests/test_api_health.py::test_no_cors_headers`), making `SameSite=Strict` cookies CSRF-safe.
Lifespan background tasks (`ff_events` listener + rollout reconciliation) live in `api/lifespan.py`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fleetforge import __version__
from fleetforge.api.deps import (
    SettingsDep,
    deploy_config_gaps,
    dynsec_configured,
    mqtt_command_configured,
)
from fleetforge.api.eventstream import EventHub
from fleetforge.api.lifespan import build_lifespan
from fleetforge.api.routers import (
    agent,
    artifact_download,
    artifact_sim,
    artifact_upload,
    artifacts,
    auth,
    auth_tokens,
    builds,
    commands,
    deploys,
    device_secrets,
    devices,
    enroll,
    enrollment,
    events,
    groups,
    partition_profiles,
    policies,
    progress,
    project_secrets,
    projects,
    swarm,
    users,
)
from fleetforge.auth.cache import VerifiedSecretCache
from fleetforge.auth.ratelimit import FixedWindowLimiter
from fleetforge.buildinfo import build_info
from fleetforge.config import Settings, get_settings
from fleetforge.db.base import get_sessionmaker
from fleetforge.firmware import DEFAULT_INDEX_KEY, CatalogCache
from fleetforge.storage import SignedUrlCache
from fleetforge.storage.factory import object_store_configured

logger = logging.getLogger(__name__)

SessionMaker = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]

_ROUTERS = (
    auth.router,
    auth_tokens.router,
    projects.router,
    project_secrets.router,
    builds.router,
    policies.router,
    groups.router,
    users.router,
    enrollment.router,
    enroll.router,
    events.router,
    devices.router,
    device_secrets.router,
    agent.router,
    progress.router,
    artifact_upload.router,
    artifacts.router,
    artifact_sim.router,
    swarm.router,
    partition_profiles.router,
    artifact_download.router,
    deploys.router,
    commands.router,
)


@dataclass(frozen=True)
class _AppStateKnobs:
    login_per_ip: int = 5
    login_global: int = 30
    window_s: int = 60
    enroll_per_ip: int = 10
    enroll_global: int = 60
    progress_per_ip: int = 60
    progress_global: int = 300
    sse_queue_size: int = 200
    sse_max_clients: int = 20
    agent_catalog_ttl_s: float = 60.0
    agent_index_key: str = DEFAULT_INDEX_KEY
    signed_url_ttl_s: int = 1800
    artifact_url_refresh_margin_s: int = 300


def configure_logging(level: int = logging.INFO) -> None:
    """Send uvicorn's and the app's logs to stdout in one JSON line-per-event shape."""
    logging.basicConfig(
        level=level,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        force=True,
    )


def _settings_or_none() -> Settings | None:
    """Return `Settings` if the environment provides them, else `None`."""
    try:
        return get_settings()
    except ValidationError as exc:
        logger.warning("settings unavailable (%s); falling back to defaults", exc.error_count())
        return None


def _knobs_for(settings: Settings | None) -> _AppStateKnobs:
    if settings is None:
        return _AppStateKnobs()
    return _AppStateKnobs(
        login_per_ip=settings.login_rate_limit_per_ip,
        login_global=settings.login_rate_limit_global,
        window_s=settings.login_rate_limit_window_s,
        enroll_per_ip=settings.enroll_rate_limit_per_ip,
        enroll_global=settings.enroll_rate_limit_global,
        progress_per_ip=settings.progress_rate_limit_per_ip,
        progress_global=settings.progress_rate_limit_global,
        sse_queue_size=settings.sse_queue_size,
        sse_max_clients=settings.sse_max_clients,
        agent_catalog_ttl_s=settings.agent_catalog_ttl_s,
        agent_index_key=settings.agent_index_key,
        signed_url_ttl_s=settings.signed_url_ttl_s,
        artifact_url_refresh_margin_s=settings.artifact_url_refresh_margin_s,
    )


def _init_app_state(app: FastAPI, settings: Settings | None) -> None:
    knobs = _knobs_for(settings)
    app.state.token_cache = VerifiedSecretCache()
    app.state.login_limiter = FixedWindowLimiter(
        per_key=knobs.login_per_ip,
        per_global=knobs.login_global,
        window_s=knobs.window_s,
    )
    app.state.enroll_limiter = FixedWindowLimiter(
        per_key=knobs.enroll_per_ip,
        per_global=knobs.enroll_global,
        window_s=knobs.window_s,
    )
    app.state.progress_limiter = FixedWindowLimiter(
        per_key=knobs.progress_per_ip,
        per_global=knobs.progress_global,
        window_s=knobs.window_s,
    )
    app.state.progress_cache = VerifiedSecretCache()
    app.state.event_hub = EventHub(
        queue_size=knobs.sse_queue_size,
        max_subscribers=knobs.sse_max_clients,
    )
    app.state.agent_catalog = CatalogCache(
        ttl_s=knobs.agent_catalog_ttl_s,
        index_key=knobs.agent_index_key,
    )
    app.state.artifact_url_cache = SignedUrlCache(
        ttl_s=knobs.signed_url_ttl_s,
        refresh_margin_s=knobs.artifact_url_refresh_margin_s,
    )


def _log_startup_warnings(settings: Settings | None) -> None:
    if settings is None:
        return
    checks: tuple[tuple[bool, str], ...] = (
        (
            settings.admin_password_hash is None,
            "ADMIN_PASSWORD_HASH is not set: /v1/auth/login will answer 503. "
            "Mint one with `just admin-password` and put the SINGLE-QUOTED line in .env.",
        ),
        (
            not object_store_configured(settings),
            "no object store configured (S3_* or GCS_*): ONBOARDING as well as artifact "
            "upload and deploy (R1) will answer 503 — since S0-infra-6 the agent bundles "
            "the flasher writes live in the store, not in this image. The dev stack sets "
            "S3_* on the api service; production sets GCS_BUCKET + "
            "GCS_IMPERSONATE_SERVICE_ACCOUNT. Round-trip it with `just storage-check`.",
        ),
        (
            not mqtt_command_configured(settings),
            "MQTT_COMMAND_USERNAME/_PASSWORD are not set: POST /v1/devices/{id}/deploy "
            "will answer 503, because nothing may publish dn/cmd without the `commander` "
            "broker credential. The dev stack sets both on the api service; the broker "
            "side is `mosquitto/configure.sh`. Prove the path with `just broker-check`.",
        ),
        (
            not (settings.artifact_url_secret and settings.public_base_url),
            "ARTIFACT_URL_SECRET or PUBLIC_BASE_URL is not set: POST /v1/devices/{id}/deploy "
            "will answer 503 and no board can download firmware, because the download link "
            "in a stage command is signed with the first and built on the second. Mint a "
            "secret with `just artifact-secret`; PUBLIC_BASE_URL is the origin a DEVICE "
            "reaches (http://localhost:8080 in dev), never the container-internal one.",
        ),
        (
            not dynsec_configured(settings),
            "MQTT_DYNSEC_USERNAME/_PASSWORD are not set: enrolled devices get a broker "
            "password nothing has been told about, and broker_provisioned_at stays NULL. "
            "Correct until R0-sec-1 secures the broker; reconcile afterwards with "
            "SELECT device_id FROM devices WHERE broker_provisioned_at IS NULL.",
        ),
        (
            not settings.artifact_signing_public_keys,
            "ARTIFACT_SIGNING_PUBLIC_KEYS is not set: signature verification uses the "
            "built-in R6 development public key ring. Mint a production key pair with "
            "`just signing-key keygen` and configure ARTIFACT_SIGNING_PUBLIC_KEYS.",
        ),
    )
    for should_warn, message in checks:
        if should_warn:
            logger.warning(message)


def _ready_backends_payload() -> dict[str, str]:
    build_mode = (os.environ.get("FF_BUILD_BACKEND") or "auto").strip().lower()
    sim_mode = (os.environ.get("FF_SIM_BACKEND") or "auto").strip().lower()
    return {
        "status": "ready",
        "build_backend": "docker" if build_mode in ("auto", "docker") else build_mode,
        "sim_backend": "qemu" if sim_mode in ("auto", "qemu") else sim_mode,
    }


def _register_health_routes(app: FastAPI) -> None:
    @app.get("/v1/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness probe with build provenance (`commit`, `built_at`), performing zero I/O."""
        return {"status": "ok", **build_info().as_dict()}

    @app.get("/v1/readyz", tags=["health"])
    async def readyz(sessionmaker: SessionMaker, settings: SettingsDep) -> Any:
        """Readiness probe verifying database connectivity and mandatory deploy configuration."""
        missing = deploy_config_gaps(settings)
        reasons = [f"not configured: {', '.join(missing)}"] if missing else []
        try:
            async with sessionmaker() as session:
                await session.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError) as exc:
            logger.warning("readyz: database unreachable: %s", exc.__class__.__name__)
            reasons.insert(0, "database unreachable")
        if reasons:
            if missing:
                logger.warning("readyz: deploy is impossible, not configured: %s", missing)
            return JSONResponse(
                status_code=503,
                content={"status": "not-ready", "detail": "; ".join(reasons), "missing": missing},
            )
        return _ready_backends_payload()


def create_app(
    *,
    settings_override: Settings | None = None,
    listener_factory: Callable[..., Any] | None = None,
) -> FastAPI:
    """Build the ASGI application mounted under `/v1`."""
    configure_logging()
    settings = settings_override if settings_override is not None else _settings_or_none()
    app = FastAPI(
        title="Fleetforge",
        version=__version__,
        summary="OTA firmware management for embedded fleets",
        docs_url="/v1/docs",
        redoc_url=None,
        openapi_url="/v1/openapi.json",
        lifespan=build_lifespan(settings, listener_factory),
    )
    _init_app_state(app, settings)
    _log_startup_warnings(settings)
    for router in _ROUTERS:
        app.include_router(router)
    _register_health_routes(app)
    return app


app = create_app()
