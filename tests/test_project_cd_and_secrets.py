"""Project CD webhook SSRF/encryption, OTLP export, and secret hardening tests (`T-10`)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fleetforge.api.deps import AuthContext
from fleetforge.api.eventstream import EventHub
from fleetforge.api.routers.device_secrets import _parse_basic_auth, get_device_secrets
from fleetforge.api.routers.projects import _trigger_ci_webhook, update_project
from fleetforge.api.schemas import ProjectUpdate
from fleetforge.auth.ratelimit import FixedWindowLimiter
from fleetforge.broker import NullCommandPublisher
from fleetforge.db.models import (
    DEFAULT_PROJECT_ID,
    AdminToken,
    DeployEvent,
    Device,
    DeviceGroup,
    EnrollmentToken,
    Project,
    ProjectMember,
    ProjectRole,
    ProjectSecret,
    User,
)
from fleetforge.ingest.otel import (
    OtelExportBatch,
    _enqueue_batch,
    _project_otel_cache,
    _resolve_endpoint,
    _resolve_project_otel,
    _resource_attributes,
    build_otlp_logs_payload,
    build_otlp_metrics_payload,
    close_otel_client,
    device_snapshot,
    flush_otel_queue,
    get_otel_client,
    get_otel_queue,
    invalidate_project_otel_cache,
    parse_otel_headers,
)
from fleetforge.ingest.protocol import LogLine, TelemetrySample
from fleetforge.secrets_crypto import (
    SecretKeyNotConfiguredError,
    decrypt_secret,
    encrypt_secret,
)
from fleetforge.signing import DEFAULT_PRIMARY_PUBLIC_KEY_PEM, SigningKeyRing
from fleetforge.url_safety import validate_public_https_url
from tests.conftest import client_for, login_admin, settings_for_tests
from tests.support_deploy import DEVICE_ID

FORBIDDEN_WEBHOOK_URLS = (
    "http://hooks.example.com/ci",
    "https://localhost/webhook",
    "https://127.0.0.1:8443/hook",
    "https://169.254.169.254/computeMetadata/v1/",
    "https://10.0.0.12/internal",
    "https://192.168.1.50/hook",
    "https://100.64.0.5/cgnat",
    "https://metadata.google.internal/v1",
    "https://api.svc.cluster.local/v1",
    "https://user:pw@hooks.example.com/ci",
)


@pytest.fixture
async def clean_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    """Clean up rows created via the app's own sessionmaker after each test."""
    yield
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await session.execute(text("DELETE FROM partition_profiles WHERE origin <> 'builtin'"))
        await session.execute(delete(DeployEvent))
        await session.execute(delete(EnrollmentToken))
        await session.execute(delete(Device))
        await session.execute(delete(DeviceGroup))
        await session.execute(text("DELETE FROM artifact_versions"))
        await session.execute(text("DELETE FROM artifacts"))
        await session.execute(delete(ProjectMember))
        await session.execute(delete(AdminToken))
        await session.execute(delete(User))
        await session.execute(delete(Project).where(Project.id != DEFAULT_PROJECT_ID))
        await session.commit()


def test_url_safety_and_project_update_reject_ssrf_targets_offline() -> None:
    """Offline unit guard for `validate_public_https_url` and `ProjectUpdate` SSRF checks."""
    for bad_url in FORBIDDEN_WEBHOOK_URLS:
        with pytest.raises(ValueError):
            validate_public_https_url(bad_url)
        with pytest.raises(ValidationError):
            ProjectUpdate(ci_webhook_url=bad_url)
        with pytest.raises(ValidationError):
            ProjectUpdate(otel_endpoint=bad_url)

    assert (
        validate_public_https_url("https://ci.example.com/hooks/fleetforge")
        == "https://ci.example.com/hooks/fleetforge"
    )
    assert validate_public_https_url("https://8.8.8.8/webhook") == "https://8.8.8.8/webhook"


async def test_ci_webhook_header_encrypted_at_rest_and_decrypted_on_dispatch() -> None:
    settings = settings_for_tests(artifact_url_secret="k" * 32)
    raw_header = "Bearer ghp_super_secret_token"
    encrypted = encrypt_secret(raw_header, settings)
    assert encrypted != raw_header
    assert encrypted.startswith("gAAAAA")
    assert decrypt_secret(encrypted, settings) == raw_header

    proj = Project(
        slug="eink",
        name="E-Ink",
        repo_url="https://github.com/org/eink",
        track_mode="branch",
        track_ref="main",
        auto_deploy_target="dev",
        ci_webhook_url="https://api.github.com/repos/org/eink/dispatches",
        ci_webhook_header=encrypted,
    )

    mock_resp = AsyncMock()
    mock_resp.status_code = 204
    with (
        patch(
            "fleetforge.api.routers.projects.resolve_and_validate_public_https_url",
            new=AsyncMock(return_value=proj.ci_webhook_url),
        ),
        patch("fleetforge.api.routers.projects.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        ok = await _trigger_ci_webhook(proj, settings=settings)
        assert ok is True
        assert mock_client_cls.call_args.kwargs["trust_env"] is False
        assert mock_client_cls.call_args.kwargs["follow_redirects"] is False
        mock_client.post.assert_called_once()
        sent_headers = mock_client.post.call_args.kwargs["headers"]
        assert sent_headers["Authorization"] == raw_header


async def test_update_project_allows_deploy_scope_for_cd_tracking_and_blocks_admin_fields() -> None:
    now = dt.datetime.now(dt.UTC)
    proj = Project(
        id=uuid.uuid4(),
        slug="eink-todo",
        name="E-Ink Todo",
        track_mode="off",
        auto_deploy_target="off",
        secrets_rev=0,
        created_at=now,
        updated_at=now,
    )
    session = MagicMock()
    session.commit = AsyncMock()
    session.scalar = AsyncMock(return_value=0)
    session.scalars = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))

    @asynccontextmanager
    async def fake_sessionmaker() -> Any:
        yield session

    deploy_admin = AuthContext(
        token_id=uuid.uuid4(),
        subject="boris",
        scopes=("read", "write:artifacts", "deploy"),
        expires_at=None,
        project_id=proj.id,
    )

    with patch(
        "fleetforge.api.routers.projects.resolve_project_for_request",
        new=AsyncMock(return_value=proj),
    ) as mock_resolve:
        summary = await update_project(
            slug="eink-todo",
            body=ProjectUpdate(track_mode="branch", track_ref="main", auto_deploy_target="all"),
            admin=deploy_admin,
            settings=MagicMock(),
            sessionmaker=fake_sessionmaker,  # type: ignore[arg-type]
            publisher=NullCommandPublisher(),
        )
        assert summary.track_mode == "branch"
        assert summary.track_ref == "main"
        assert summary.auto_deploy_target == "all"
        mock_resolve.assert_awaited_once_with(
            session,
            deploy_admin,
            "eink-todo",
            min_role=ProjectRole.OPERATOR,
            required_scope="deploy",
        )

    with pytest.raises(HTTPException) as exc_info:
        await update_project(
            slug="eink-todo",
            body=ProjectUpdate(name="Renamed by Deploy Token"),
            admin=deploy_admin,
            settings=MagicMock(),
            sessionmaker=fake_sessionmaker,  # type: ignore[arg-type]
            publisher=NullCommandPublisher(),
        )
    assert exc_info.value.status_code == 403


async def test_webhook_url_must_be_public_https(admin_app: FastAPI, clean_tables: None) -> None:
    """A-6: `ci_webhook_url` and `otel_endpoint` reject private/internal URLs (`422`)."""
    _ = clean_tables
    admin_headers = {"Authorization": f"Bearer {await login_admin(admin_app)}"}

    async with client_for(admin_app, base_url="https://testserver") as client:
        for bad_url in FORBIDDEN_WEBHOOK_URLS:
            r_wh = await client.patch(
                "/v1/projects/default",
                headers=admin_headers,
                json={"ci_webhook_url": bad_url},
            )
            assert r_wh.status_code == 422, (
                f"expected 422 for ci_webhook_url={bad_url!r}: {r_wh.text}"
            )

            r_otel = await client.patch(
                "/v1/projects/default",
                headers=admin_headers,
                json={"otel_endpoint": bad_url},
            )
            assert r_otel.status_code == 422, (
                f"expected 422 for otel_endpoint={bad_url!r}: {r_otel.text}"
            )

        ok = await client.patch(
            "/v1/projects/default",
            headers=admin_headers,
            json={
                "ci_webhook_url": "https://ci.example.com/hooks/fleetforge",
                "otel_endpoint": "https://otel.example.com:4318",
            },
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["ci_webhook_url"] == "https://ci.example.com/hooks/fleetforge"
        assert ok.json()["otel_endpoint"] == "https://otel.example.com:4318"

        short_create = await client.post(
            "/v1/users",
            headers=admin_headers,
            json={"username": "shortpw", "password": "x"},
        )
        assert short_create.status_code == 422, short_create.text

        ok_create = await client.post(
            "/v1/users",
            headers=admin_headers,
            json={"username": "validpw", "password": "eight-chars-pass"},
        )
        assert ok_create.status_code == 201, ok_create.text

        short_patch = await client.patch(
            "/v1/users/validpw",
            headers=admin_headers,
            json={"password": "short"},
        )
        assert short_patch.status_code == 422, short_patch.text


class TestSecretsCryptoAndDeviceSecretsAuth:
    def test_encrypt_and_decrypt_secret_round_trip(self) -> None:
        settings = settings_for_tests(
            secrets_encryption_key="dedicated-test-secrets-encryption-key-01",
        )
        ciphertext = encrypt_secret("sk-live-rotated-key-42", settings)
        assert "sk-live-rotated-key-42" not in ciphertext
        assert decrypt_secret(ciphertext, settings) == "sk-live-rotated-key-42"

    def test_parse_basic_auth_validates_device_id_and_password(self) -> None:
        token = base64.b64encode(f"{DEVICE_ID}:my-mqtt-pass:with:colons".encode()).decode("ascii")
        parsed = _parse_basic_auth(f"Basic {token}")
        assert parsed == (DEVICE_ID, "my-mqtt-pass:with:colons")

        assert _parse_basic_auth(None) is None
        assert _parse_basic_auth("Bearer ffa_123") is None
        bad_id = base64.b64encode(b"not-a-mac:secret").decode("ascii")
        assert _parse_basic_auth(f"Basic {bad_id}") is None
        empty_pw = base64.b64encode(f"{DEVICE_ID}:".encode()).decode("ascii")
        assert _parse_basic_auth(f"Basic {empty_pw}") is None


class TestOtelExporter:
    def test_parse_otel_headers_supports_kv_colon_and_json(self) -> None:
        assert parse_otel_headers("Authorization=Bearer tok123, X-Org=eink") == {
            "Authorization": "Bearer tok123",
            "X-Org": "eink",
        }
        assert parse_otel_headers("Authorization: Bearer tok123\nX-Scope: prod") == {
            "Authorization": "Bearer tok123",
            "X-Scope": "prod",
        }
        assert parse_otel_headers('{"Authorization": "Basic abc=="}') == {
            "Authorization": "Basic abc==",
        }
        assert parse_otel_headers(None) == {}

    def test_resolve_endpoint_normalizes_base_and_full_urls(self) -> None:
        assert (
            _resolve_endpoint("https://otel.example.com", "/v1/logs")
            == "https://otel.example.com/v1/logs"
        )
        assert (
            _resolve_endpoint("https://otel.example.com/v1/logs", "/v1/logs")
            == "https://otel.example.com/v1/logs"
        )
        assert (
            _resolve_endpoint("https://otel.example.com/v1/logs", "/v1/metrics")
            == "https://otel.example.com/v1/metrics"
        )

    def test_build_otlp_logs_and_metrics_payloads(self) -> None:
        dev = Device(
            device_id=DEVICE_ID,
            name="kitchen-eink",
            platform_type="esp32c6",
            fw_version="1.2.0",
            link_type="wifi",
            power_class="sleepy",
            expected_wake_interval_s=900,
            capabilities=["ota"],
        )
        now = dt.datetime(2026, 10, 21, 0, 0, 10, tzinfo=dt.UTC)
        lines = [
            LogLine(age_ms=2000, level="I", text="morse: ---...--- (cycle 1)"),
            LogLine(age_ms=500, level="E", text="api key expired"),
        ]
        logs_payload = build_otlp_logs_payload(
            device=dev,
            project_slug="eink-frame",
            lines=lines,
            dropped=3,
            received_at=now,
        )
        res_logs = logs_payload["resourceLogs"][0]
        attrs = {
            a["key"]: a["value"]["stringValue"]
            for a in res_logs["resource"]["attributes"]
            if "stringValue" in a["value"]
        }
        assert attrs["service.name"] == "eink-frame"
        assert attrs["service.version"] == "1.2.0"
        assert attrs["device.id"] == DEVICE_ID
        assert attrs["hardware.platform"] == "esp32c6"

        records = res_logs["scopeLogs"][0]["logRecords"]
        assert len(records) == 2
        assert records[0]["severityText"] == "INFO"
        assert records[0]["body"]["stringValue"] == "morse: ---...--- (cycle 1)"
        assert records[1]["severityNumber"] == 17
        assert records[1]["severityText"] == "ERROR"
        keys = [a["key"] for a in records[0]["attributes"]]
        assert keys[0] == "fleetforge.age_ms"
        assert "firmware.version" in keys
        assert "device.rssi_dbm" not in keys and "device.free_heap_bytes" not in keys

        samples = [
            TelemetrySample(age_ms=1000, name="battery_v", value=3.92),
            TelemetrySample(age_ms=0, name="battery_v", value=3.91),
        ]
        metrics_payload = build_otlp_metrics_payload(
            device=dev,
            project_slug="eink-frame",
            samples=samples,
            received_at=now,
        )
        metrics = metrics_payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        assert len(metrics) == 1
        assert metrics[0]["name"] == "battery_v"
        assert len(metrics[0]["gauge"]["dataPoints"]) == 2

    def test_resource_carries_the_announce_and_records_carry_the_heartbeat(self) -> None:
        group = uuid.uuid4()
        dev = Device(
            device_id=DEVICE_ID,
            name="kitchen-eink",
            platform_type="esp32s3",
            fw_version="2.0.1",
            agent_version="0.6.0",
            partition_layout="ab-4m-v1",
            flash_chip_size=4_194_304,
            rollback_capable=True,
            boot_ok=True,
            link_type="wifi",
            power_class="sleepy",
            expected_wake_interval_s=900,
            capabilities=["ota"],
            last_reset_reason="deepsleep",
            is_dev=True,
            group_id=group,
            secrets_rev=2,
            sub_versions={"display_fw": "1.4"},
            rssi=-61,
            free_heap=41_000,
            min_free_heap=38_200,
            uptime_s=77,
            health_at=dt.datetime(2026, 11, 6, 2, 0, 0, tzinfo=dt.UTC),
        )
        now = dt.datetime(2026, 11, 6, 2, 0, 10, tzinfo=dt.UTC)
        payload = build_otlp_logs_payload(
            device=dev,
            project_slug="eink-todo",
            lines=[LogLine(age_ms=100, level="W", text="list fetch http 502")],
            dropped=0,
            received_at=now,
        )
        res = payload["resourceLogs"][0]
        attrs = {a["key"]: a["value"] for a in res["resource"]["attributes"]}
        assert attrs["service.name"] == {"stringValue": "eink-todo"}
        assert attrs["service.version"] == {"stringValue": "2.0.1"}
        assert attrs["service.instance.id"] == {"stringValue": DEVICE_ID}
        assert attrs["host.arch"] == {"stringValue": "esp32s3"}
        assert attrs["firmware.library.version"] == {"stringValue": "0.6.0"}
        assert attrs["firmware.partition_layout"] == {"stringValue": "ab-4m-v1"}
        assert attrs["firmware.boot_ok"] == {"boolValue": True}
        assert attrs["firmware.rollback_capable"] == {"boolValue": True}
        assert attrs["firmware.sub_versions.display_fw"] == {"stringValue": "1.4"}
        assert attrs["device.flash_chip_size"] == {"intValue": "4194304"}
        assert attrs["device.reset_reason"] == {"stringValue": "deepsleep"}
        assert attrs["device.power_class"] == {"stringValue": "sleepy"}
        assert attrs["device.is_dev"] == {"boolValue": True}
        assert attrs["device.group_id"] == {"stringValue": str(group)}
        assert attrs["fleetforge.secrets_rev"] == {"intValue": "2"}
        assert "device.rssi_dbm" not in attrs

        record = res["scopeLogs"][0]["logRecords"][0]
        rec = {a["key"]: a["value"] for a in record["attributes"]}
        assert rec["fleetforge.age_ms"] == {"intValue": "100"}
        assert rec["device.rssi_dbm"] == {"intValue": "-61"}
        assert rec["device.free_heap_bytes"] == {"intValue": "41000"}
        assert rec["device.min_free_heap_bytes"] == {"intValue": "38200"}
        assert rec["device.uptime_s"] == {"intValue": "77"}
        assert rec["firmware.version"] == {"stringValue": "2.0.1"}
        assert rec["fleetforge.health_at"] == {"stringValue": "2026-11-06T02:00:00+00:00"}

        snap = device_snapshot(dev)
        assert snap["device.rssi_dbm"] == -61
        assert snap["firmware.version"] == "2.0.1"
        assert json.dumps(snap)
        assert device_snapshot(None) == {}
        bare = Device(device_id="x", platform_type="esp32", capabilities=[])
        assert device_snapshot(bare) == {}
        bare_keys = {a["key"] for a in _resource_attributes(bare, "p")}
        assert "firmware.version" not in bare_keys
        assert "device.name" not in bare_keys

    async def test_otel_client_pooling_and_bounded_queue_drain(self) -> None:
        c1 = get_otel_client()
        c2 = get_otel_client()
        assert c1 is c2

        queue = get_otel_queue()
        while not queue.empty():
            queue.get_nowait()
            queue.task_done()

        batch = OtelExportBatch(
            signal="logs",
            device_id=DEVICE_ID,
            url="https://otel.example.com/v1/logs",
            body={"resourceLogs": []},
            headers={"Content-Type": "application/json"},
        )
        assert _enqueue_batch(batch) is True
        assert queue.qsize() == 1

        with patch("fleetforge.ingest.otel.send_otel_batch", return_value=True) as mock_send:
            flushed = await flush_otel_queue()
            assert flushed == 1
            assert queue.qsize() == 0
            mock_send.assert_called_once_with(batch)

        await close_otel_client()

    def test_fixed_window_limiter_protects_global_bucket_from_key_eviction(self) -> None:
        limiter = FixedWindowLimiter(per_key=5, per_global=3, window_s=60.0, max_keys=4)
        for i in range(3):
            limiter.record_failure(f"10.0.0.{i}")
        assert not limiter.allow("10.0.0.99")
        for i in range(10, 30):
            _ = limiter.allow(f"192.168.1.{i}")
        assert not limiter.allow("10.0.0.99")

    async def test_otel_project_cache_avoids_repeated_db_queries_and_invalidates_on_update(
        self,
    ) -> None:
        proj_id = uuid.uuid4()
        invalidate_project_otel_cache()
        settings = settings_for_tests(artifact_url_secret="k" * 32)
        enc_headers = encrypt_secret('{"Authorization": "Bearer secret-tok"}', settings)
        t0 = dt.datetime(2026, 11, 26, 1, 0, 0, tzinfo=dt.UTC)
        t1 = t0 + dt.timedelta(seconds=5)

        session = AsyncMock()
        session.identity_map = {}
        nested_cm = AsyncMock()
        session.begin_nested = lambda: nested_cm
        proj = Project(
            id=proj_id,
            slug="eink",
            name="E-Ink",
            otel_endpoint="https://otel.example.com",
            otel_headers=enc_headers,
            updated_at=t0,
        )
        session.scalar = AsyncMock(return_value=t0)
        session.get = AsyncMock(return_value=proj)
        dev = Device(
            device_id="a4cf12b3de01",
            project_id=proj_id,
            platform_type="esp32c6",
            link_type="wifi",
            power_class="always_on",
        )

        res1 = await _resolve_project_otel(session, dev, settings=settings)
        res2 = await _resolve_project_otel(session, dev, settings=settings)
        assert res1 == (
            "eink",
            "https://otel.example.com",
            {"Authorization": "Bearer secret-tok"},
        )
        assert res2 == res1
        assert session.get.call_count == 1
        assert any(k[0] == proj_id for k in _project_otel_cache)

        proj.otel_endpoint = "https://otel-v2.example.com"
        proj.updated_at = t1
        session.scalar = AsyncMock(return_value=t1)
        res3 = await _resolve_project_otel(session, dev, settings=settings)
        assert res3 == (
            "eink",
            "https://otel-v2.example.com",
            {"Authorization": "Bearer secret-tok"},
        )
        assert session.get.call_count == 2

        invalidate_project_otel_cache(proj_id)
        assert not any(k[0] == proj_id for k in _project_otel_cache)


class TestTrack1InfrastructureAndSecurityHardening:
    def test_multifernet_secret_rotation_fallback_and_signing_default_ring_flag(self) -> None:
        legacy_settings = settings_for_tests(
            artifact_url_secret="legacy-artifact-url-secret-0001",
            secrets_encryption_key=None,
        )
        token = encrypt_secret("wifi-psk-top-secret", legacy_settings)

        dual_settings = settings_for_tests(
            artifact_url_secret="legacy-artifact-url-secret-0001",
            secrets_encryption_key="dedicated-secrets-key-0002",
        )
        assert decrypt_secret(token, dual_settings) == "wifi-psk-top-secret"

        rotated_token = encrypt_secret("wifi-psk-top-secret", dual_settings)
        post_rotate_settings = settings_for_tests(
            artifact_url_secret="brand-new-artifact-url-secret-0003",
            secrets_encryption_key="dedicated-secrets-key-0002",
        )
        assert decrypt_secret(rotated_token, post_rotate_settings) == "wifi-psk-top-secret"

        assert SigningKeyRing.from_config().uses_default_ring is True
        assert (
            SigningKeyRing.from_config(public_keys=DEFAULT_PRIMARY_PUBLIC_KEY_PEM).uses_default_ring
            is False
        )

    def test_event_hub_fails_closed_for_tenant_projects_when_event_project_id_is_none(
        self,
    ) -> None:
        tenant_proj = uuid.uuid4()
        hub = EventHub(queue_size=8, max_subscribers=8)
        with (
            hub.subscribe(project_id=tenant_proj) as sub_tenant,
            hub.subscribe(project_id=DEFAULT_PROJECT_ID) as sub_default,
            hub.subscribe(project_id=None) as sub_unscoped,
        ):
            hub.publish('{"type":"device.seen"}', project_id=None)
            assert sub_tenant.queue.empty()
            assert sub_default.queue.get_nowait() == '{"type":"device.seen"}'
            assert sub_unscoped.queue.get_nowait() == '{"type":"device.seen"}'

    async def test_device_secrets_fails_500_instead_of_truncating_on_decrypt_error(self) -> None:
        pw = "device-mqtt-pass-123"
        proj_id = uuid.uuid4()
        dev = Device(
            device_id=DEVICE_ID,
            project_id=proj_id,
            platform_type="esp32s3",
            link_type="wifi",
            power_class="always_on",
            mqtt_password_hash=hashlib.sha256(pw.encode()).hexdigest(),
        )
        proj = Project(id=proj_id, slug="eink", name="E-Ink", secrets_rev=3)
        corrupt_row = ProjectSecret(
            project_id=proj_id,
            key="API_KEY",
            ciphertext="not-a-valid-fernet-token",
        )

        session = AsyncMock()
        call_idx = [0]

        def fake_scalars(_stmt: Any) -> Any:
            call_idx[0] += 1
            if call_idx[0] == 1:
                return SimpleNamespace(one_or_none=lambda: dev)
            return SimpleNamespace(all=lambda: [corrupt_row])

        session.scalars = AsyncMock(side_effect=fake_scalars)
        session.get = AsyncMock(return_value=proj)

        class _FakeSessionCM:
            async def __aenter__(self) -> Any:
                return session

            async def __aexit__(self, *args: object) -> None:
                return None

        auth_b64 = base64.b64encode(f"{DEVICE_ID}:{pw}".encode()).decode("ascii")
        req = SimpleNamespace(headers={"Authorization": f"Basic {auth_b64}"})
        settings = settings_for_tests(artifact_url_secret="k" * 32)

        with pytest.raises(HTTPException) as exc_info:
            await get_device_secrets(
                request=req,  # type: ignore[arg-type]
                settings=settings,
                sessionmaker=lambda: _FakeSessionCM(),  # type: ignore[arg-type]
            )
        assert exc_info.value.status_code == 500


def test_decrypt_with_missing_key_fails_closed() -> None:
    """A-4: missing secret encryption keys fail closed without sending raw ciphertext."""
    unconfigured = settings_for_tests(
        secrets_encryption_key=None,
        artifact_url_secret=None,
    )
    with pytest.raises(SecretKeyNotConfiguredError):
        encrypt_secret("secret-value", unconfigured)

    keyed = settings_for_tests(
        secrets_encryption_key="valid-encryption-key-0123456789",
    )
    token = encrypt_secret("Authorization: Bearer top-secret", keyed)
    with pytest.raises(SecretKeyNotConfiguredError):
        decrypt_secret(token, unconfigured)

    invalidate_project_otel_cache()
    proj = Project(
        id=uuid.uuid4(),
        slug="otel-test",
        name="OTel Test",
        otel_endpoint="https://otel.example.com",
        otel_headers=token,
    )
    dev = Device(
        device_id="a4cf12b3de01",
        project_id=proj.id,
        platform_type="esp32c6",
        link_type="wifi",
        power_class="always_on",
    )
    session = AsyncMock()
    session.identity_map = None

    @contextlib.asynccontextmanager
    async def _nested() -> Any:
        yield

    session.begin_nested = MagicMock(side_effect=_nested)
    session.scalar = AsyncMock(return_value=proj.updated_at)
    session.get = AsyncMock(return_value=proj)
    resolved = asyncio.run(_resolve_project_otel(session, dev, settings=unconfigured))
    assert resolved is None
