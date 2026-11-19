"""The API's two probes, and the one-origin invariant.

`/v1/readyz` covers the database AND the deploy-mandatory settings (S0-infra-9); the
container healthcheck uses it. `/v1/healthz` covers neither and stays no-I/O.

These run against the ASGI app in-process (`httpx.ASGITransport`) — no uvicorn, no
network; the app fixtures live in `tests/conftest.py`. `test_healthz_ok`
deliberately does **not** request the `engine` fixture:
liveness must answer with the database stopped, and depending on the fixture here
would hide a `/v1/healthz` that had quietly grown an I/O call.
"""

from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError

from fleetforge import __version__
from fleetforge.config import Settings, get_settings
from fleetforge.db.base import get_sessionmaker
from tests.conftest import client_for, settings_for_tests


def deployable_settings(**overrides: object) -> Settings:
    """Every deploy-mandatory setting present (S0-infra-9), then `overrides` on top.

    `settings_for_tests` nulls the object store and does NOT pin the commander pair, so
    both are set explicitly here: a readiness test that leaned on the developer's `.env`
    would pass on one box and fail on the next.
    """
    values: dict[str, object] = {
        "s3_endpoint_url": "http://minio:9000",
        "s3_bucket": "fleetforge",
        "mqtt_command_username": "ff-commander",
        "mqtt_command_password": "unused",
    }
    values.update(overrides)
    return settings_for_tests(**values)


class FailingSession:
    """A session whose every statement fails the way a dead database does."""

    async def __aenter__(self) -> "FailingSession":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def execute(self, *args: Any, **kwargs: Any) -> None:
        raise OperationalError("SELECT 1", {}, OSError("connection refused"))


async def test_healthz_ok(app_no_db: FastAPI) -> None:
    """Liveness answers 200 with the version, and touches no I/O."""
    async with client_for(app_no_db) as client:
        response = await client.get("/v1/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__


async def test_healthz_carries_build_provenance(app_no_db: FastAPI) -> None:
    """The dashboard footer renders these beside its own build; both keys always exist.

    A missing key would render as an empty gap in the footer, which reads as "same
    build" — the one conclusion the line exists to prevent. Unbaked builds say
    `unknown` instead.
    """
    async with client_for(app_no_db) as client:
        response = await client.get("/v1/healthz")
    body = response.json()
    assert set(body) == {"status", "version", "commit", "built_at"}
    assert body["commit"] and body["built_at"]


async def test_readyz_ok(app_with_db: FastAPI) -> None:
    """Readiness answers 200 once the database is reachable and deploy is configured."""
    settings = deployable_settings()
    app_with_db.dependency_overrides[get_settings] = lambda: settings
    async with client_for(app_with_db) as client:
        response = await client.get("/v1/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_readyz_is_503_when_database_is_unreachable(app_no_db: FastAPI) -> None:
    """An unreachable database is a 503 with a reason, never a bare 500."""
    settings = deployable_settings()
    app_no_db.dependency_overrides[get_settings] = lambda: settings
    app_no_db.dependency_overrides[get_sessionmaker] = lambda: FailingSession
    async with client_for(app_no_db) as client:
        response = await client.get("/v1/readyz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not-ready",
        "detail": "database unreachable",
        "missing": [],
    }


@pytest.mark.parametrize(
    ("overrides", "gap"),
    [
        ({"s3_endpoint_url": None}, "S3_* or GCS_*"),
        # Compose interpolates an unset `${VAR}` to "", not absence.
        ({"artifact_url_secret": ""}, "ARTIFACT_URL_SECRET"),
        ({"public_base_url": None}, "PUBLIC_BASE_URL"),
        ({"mqtt_command_password": ""}, "MQTT_COMMAND_USERNAME/_PASSWORD"),
    ],
)
async def test_readyz_is_503_naming_a_missing_deploy_setting(
    app_with_db: FastAPI, overrides: dict[str, object], gap: str
) -> None:
    """S0-infra-9: a stack that cannot deploy is not ready, and says which setting.

    Each of these was a startup WARNING and nothing else; F-2026-09-23-001 was a prod
    stack with `PUBLIC_BASE_URL` unset whose every probe was green.
    """
    settings = deployable_settings(**overrides)
    app_with_db.dependency_overrides[get_settings] = lambda: settings
    async with client_for(app_with_db) as client:
        response = await client.get("/v1/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not-ready"
    assert body["missing"] == [gap]
    assert gap in body["detail"]


async def test_readyz_reports_every_gap_at_once_and_no_values(app_no_db: FastAPI) -> None:
    """Database and configuration together, so one restart fixes everything.

    The probe is public (nginx proxies `/v1/*`), so it carries names, never values.
    """
    settings = deployable_settings(artifact_url_secret=None, public_base_url=None)
    app_no_db.dependency_overrides[get_settings] = lambda: settings
    app_no_db.dependency_overrides[get_sessionmaker] = lambda: FailingSession
    async with client_for(app_no_db) as client:
        response = await client.get("/v1/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["missing"] == ["ARTIFACT_URL_SECRET", "PUBLIC_BASE_URL"]
    assert body["detail"] == (
        "database unreachable; not configured: ARTIFACT_URL_SECRET, PUBLIC_BASE_URL"
    )
    assert "ff-commander" not in response.text
    assert "minio" not in response.text


async def test_healthz_stays_green_while_deploy_is_unconfigured(app_no_db: FastAPI) -> None:
    """Liveness must not follow readiness: a config gap is not a reason to restart."""
    settings = deployable_settings(artifact_url_secret=None)
    app_no_db.dependency_overrides[get_settings] = lambda: settings
    async with client_for(app_no_db) as client:
        response = await client.get("/v1/healthz")
    assert response.status_code == 200


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


async def test_lifespan_with_settings_starts_and_dispatches_device_events(
    engine: Any, clean_tables: None
) -> None:
    """T-2: entering `app.router.lifespan_context(app)` with real `Settings`, PostgreSQL, and
    an injected listener starts cleanly and dispatches `DEVICE_ANNOUNCE` (wake republish) and
    `DEVICE_DEPLOY` (`confirmed` wave advancement)."""
    import asyncio
    import datetime as dt
    import hashlib

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from fleetforge.api.deps import get_command_publisher
    from fleetforge.api.main import create_app
    from fleetforge.clock import now_utc
    from fleetforge.db.models import DEFAULT_PROJECT_ID, Artifact, ArtifactVersion, Device
    from fleetforge.deploys import DeploySender, record_observed_status, record_requested
    from fleetforge.events import DeviceEvent, EventType
    from fleetforge.signing import generate_keypair, sign_digest

    priv_pem, pub_pem = generate_keypair()
    other_priv, other_pub = generate_keypair()
    _ = other_priv
    blob = b"\xe9\x00" + b"FFOTALIB:2.0.0" * 32
    sha = hashlib.sha256(blob).hexdigest()
    sig = sign_digest(sha, priv_pem)
    now = now_utc()

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        session.add(
            Artifact(
                sha256=sha,
                kind="user_firmware",
                target="esp32c6",
                size_bytes=len(blob),
                partition_layout="ab-4m-v1",
                has_lib_marker=True,
                sig=sig,
            )
        )
        session.add(
            ArtifactVersion(
                project_id=DEFAULT_PROJECT_ID,
                target="esp32c6",
                version="2.0.0",
                sha256=sha,
            )
        )
        # Sleepy board with open `requested` intent + canary & fleet board for rollout advance
        session.add_all(
            [
                Device(
                    device_id="a4cf12b3de11",
                    project_id=DEFAULT_PROJECT_ID,
                    platform_type="esp32c6",
                    fw_version="1.0.0",
                    partition_layout="ab-4m-v1",
                    ota_slot_size=1966080,
                    rollback_capable=True,
                    capabilities=["ota", "sig"],
                    link_type="wifi",
                    power_class="sleepy",
                    expected_wake_interval_s=3600,
                    presence_reported=True,
                    last_seen=now,
                ),
                Device(
                    device_id="a4cf12b3de12",
                    project_id=DEFAULT_PROJECT_ID,
                    platform_type="esp32c6",
                    fw_version="2.0.0",
                    partition_layout="ab-4m-v1",
                    ota_slot_size=1966080,
                    rollback_capable=True,
                    capabilities=["ota", "sig"],
                    link_type="wifi",
                    power_class="always_on",
                    is_dev=True,
                    presence_reported=True,
                    last_seen=now,
                ),
                Device(
                    device_id="a4cf12b3de13",
                    project_id=DEFAULT_PROJECT_ID,
                    platform_type="esp32c6",
                    fw_version="1.0.0",
                    partition_layout="ab-4m-v1",
                    ota_slot_size=1966080,
                    rollback_capable=True,
                    capabilities=["ota", "sig"],
                    link_type="wifi",
                    power_class="always_on",
                    is_dev=False,
                    presence_reported=True,
                    last_seen=now,
                ),
            ]
        )
        await session.flush()
        sender = DeploySender(subject="admin", token_id="tok-1", credential="session")
        await record_requested(
            session,
            device_id="a4cf12b3de11",
            cmd_id="cmd-sleepy-wake",
            artifact_version="2.0.0",
            from_version="1.0.0",
            sha256=sha,
            size_bytes=len(blob),
            target="esp32c6",
            apply="auto",
            sent_by=sender,
        )
        await record_requested(
            session,
            device_id="a4cf12b3de12",
            cmd_id="cmd-canary-200",
            artifact_version="2.0.0",
            from_version="1.0.0",
            sha256=sha,
            size_bytes=len(blob),
            target="esp32c6",
            apply="auto",
            sent_by=sender,
            rollout={
                "target_filter": "all",
                "canary": True,
                "max_parallel": 2,
                "override": [],
                "target_chip": "esp32c6",
            },
        )
        await record_observed_status(
            session,
            device_id="a4cf12b3de12",
            cmd_id="cmd-canary-200",
            state="confirmed",
            pct=100,
            at=now + dt.timedelta(seconds=5),
        )
        await session.commit()

    captured_on_event: list[Any] = []

    class FakeListener:
        def __init__(self, _dsn: str, _hub: Any, *, ping_s: float, on_event: Any = None) -> None:
            self.ping_s = ping_s
            captured_on_event.append(on_event)

        async def run(self) -> None:
            await asyncio.Event().wait()

    published: list[tuple[str, dict[str, Any]]] = []

    class RecordingPublisher:
        async def publish(self, device_id: str, payload: dict[str, Any]) -> None:
            published.append((device_id, payload))

        async def publish_many(self, items: Any) -> None:
            published.extend(items)

    settings = deployable_settings(
        artifact_signing_key=priv_pem,
        artifact_signing_public_keys=f"{pub_pem};{other_pub}",
        rollout_reconcile_interval_s=3600.0,
    )
    app = create_app(settings_override=settings, listener_factory=FakeListener)
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    app.dependency_overrides[get_command_publisher] = lambda: RecordingPublisher()

    async with app.router.lifespan_context(app):
        assert len(captured_on_event) == 1
        on_event = captured_on_event[0]
        assert on_event is not None

        # 1. Dispatch DEVICE_ANNOUNCE for sleepy board -> republishes `cmd-sleepy-wake`
        await on_event(
            DeviceEvent(
                type=EventType.DEVICE_ANNOUNCE,
                device_id="a4cf12b3de11",
                at=now,
                online=True,
            )
        )
        assert [dev_id for dev_id, _ in published] == ["a4cf12b3de11"]
        assert published[0][1]["id"] == "cmd-sleepy-wake"

        # 2. Dispatch DEVICE_DEPLOY (`confirmed`) for canary -> advances Wave 2 to `a4cf12b3de13`
        published.clear()
        await on_event(
            DeviceEvent(
                type=EventType.DEVICE_DEPLOY,
                device_id="a4cf12b3de12",
                at=now,
                online=True,
                state="confirmed",
                cmd_id="cmd-canary-200",
                is_terminal=True,
            )
        )
        assert [dev_id for dev_id, _ in published] == ["a4cf12b3de13"]
        assert published[0][1]["artifact"]["version"] == "2.0.0"
