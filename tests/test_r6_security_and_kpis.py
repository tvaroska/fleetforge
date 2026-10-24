"""R4-test-1 and R6 (R6-SEC-1, R6-SEC-2, R6-FW-1, R6-BE-1, R6-TEST-1) verification suite.

Covers:
1. R4-test-1: Live metric (`up/telemetry`) + log line (`up/log`) emitted by a board reaching
   the ingestor, stored in Postgres, and served by the dashboard read model (`GET /v1/devices`,
   `GET /v1/devices/{id}/telemetry`, `GET /v1/devices/{id}/logs`) alongside SSE hints.
2. R6-SEC-1 & R6-SEC-2: ECDSA P-256 (`ecdsa-p256-sha256:<base64_der>`) artifact signing,
   >=2-key public key rotation ring across server (`signing.py`), simulator (`device.py`),
   and C firmware (`ff_mqtt.c`, `ff_ota.c`), plus rejection of malformed, tampered, or
   unsigned artifacts.
3. R6-FW-1: Resumable HTTP `Range: bytes=<offset>-` download with retry and exponential
   backoff in the simulator and C firmware (`ff_ota.c`).
4. R6-BE-1 & R6-TEST-1: Delivery success (>= 90%) and Fleet safety (= 100%) KPIs computed
   over `deploy_events` history, including the rollback-saves gap (`rolled_back`), cancelled
   exclusion, and decommissioned-device evidence preservation.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from collections.abc import AsyncIterator, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fleetforge.api.deps import get_command_publisher, get_object_store, get_settings
from fleetforge.api.schemas import ArtifactSummary, ArtifactUploaded, DeployKpisSummary
from fleetforge.broker.commands import stage_payload
from fleetforge.clock import now_utc
from fleetforge.db.models import (
    DeployEvent,
    DeployState,
    Device,
    DeviceLog,
    DeviceMetric,
    DeviceTelemetry,
)
from fleetforge.deploy_precheck import (
    ResolvedArtifact,
    warnings,
)
from fleetforge.deploys import (
    DeploySender,
    deploy_kpis,
    record_observed_status,
    record_publish_failure,
    record_requested,
)
from fleetforge.events import EventType
from fleetforge.ingestor.handlers import handle_up_message
from fleetforge.ingestor.protocol import LogPayload, TelemetryPayload, decode
from fleetforge.signing import (
    DEFAULT_PRIMARY_PRIVATE_KEY_PEM,
    DEFAULT_PRIMARY_PUBLIC_KEY_PEM,
    DEFAULT_PUBLIC_KEY_RING,
    DEFAULT_SECONDARY_PRIVATE_KEY_PEM,
    DEFAULT_SECONDARY_PUBLIC_KEY_PEM,
    SigningKeyRing,
    _cli,
    generate_keypair,
    is_valid_sig_format,
    sign_bytes,
    sign_digest,
    verify_bytes,
)
from fleetforge.simulator.device import (
    DEFAULT_SIGNING_PUBKEY_RING,
    LINK_FAST,
    SAFE_WINDOW_AUTO,
    STAGE_WALK,
    STATE_DOWNLOADING,
    STATE_FAILED,
    STATE_STAGING,
    STATE_VERIFYING,
    STATUS,
    DeviceIdentity,
    LinkProfile,
    StageRunner,
    run_session,
    up_topic,
)
from fleetforge.simulator.state import Credential
from tests.conftest import (
    FakeCommandPublisher,
    MemoryObjectStore,
    client_for,
    login_admin,
    settings_for_tests,
)
from tests.test_simulator import FakeClient, FakeMessage, transcript

REPO_ROOT = Path(__file__).resolve().parent.parent
FF_OTA_H = REPO_ROOT / "agent" / "components" / "fleetforge" / "src" / "ff_ota.h"
FF_OTA_C = REPO_ROOT / "agent" / "components" / "fleetforge" / "src" / "ff_ota.c"
FF_MQTT_C = REPO_ROOT / "agent" / "components" / "fleetforge" / "src" / "ff_mqtt.c"

DEVICE_ID = "a4cf12b3de90"
SECOND_DEVICE_ID = "a4cf12b3de91"
THIRD_DEVICE_ID = "a4cf12b3de92"
IMAGE = b"\xe9\x06\x02\x20" + b"signed-firmware-payload-r6-" * 4000
IMAGE_SHA256 = hashlib.sha256(IMAGE).hexdigest()
SIGNED_URL = f"https://storage.invalid/blobs/sha256/{IMAGE_SHA256}?exp=9999999999&sig=deadbeef"


# ===========================================================================
# Pure unit tests: R6-SEC-1 & R6-SEC-2 (Signing, 2-key rotation ring, CLI)
# ===========================================================================


class TestSigningAndKeyRotationPure:
    def test_default_ring_has_two_distinct_p256_public_keys_matching_simulator_and_c(self) -> None:
        assert len(DEFAULT_PUBLIC_KEY_RING) == 2
        assert DEFAULT_PRIMARY_PUBLIC_KEY_PEM != DEFAULT_SECONDARY_PUBLIC_KEY_PEM
        assert DEFAULT_SIGNING_PUBKEY_RING == DEFAULT_PUBLIC_KEY_RING

        ota_c = FF_OTA_C.read_text(encoding="utf-8")
        # Both public keys in the rotation ring must match the C agent's compiled-in ring.
        for pub_pem in DEFAULT_PUBLIC_KEY_RING:
            body_lines = [
                line.strip()
                for line in pub_pem.strip().splitlines()
                if not line.startswith("-----")
            ]
            for line in body_lines:
                assert line in ota_c

    def test_primary_and_secondary_signatures_verify_against_the_rotation_ring(self) -> None:
        sig_primary = sign_bytes(IMAGE, DEFAULT_PRIMARY_PRIVATE_KEY_PEM)
        sig_secondary = sign_bytes(IMAGE, DEFAULT_SECONDARY_PRIVATE_KEY_PEM)

        assert is_valid_sig_format(sig_primary)
        assert is_valid_sig_format(sig_secondary)
        assert verify_bytes(IMAGE, sig_primary, DEFAULT_PUBLIC_KEY_RING)
        assert verify_bytes(IMAGE, sig_secondary, DEFAULT_PUBLIC_KEY_RING)

        # Tampered bytes or an unrelated key must fail verification.
        assert not verify_bytes(IMAGE + b"\x00", sig_primary, DEFAULT_PUBLIC_KEY_RING)
        rogue_priv, _ = generate_keypair()
        sig_rogue = sign_bytes(IMAGE, rogue_priv)
        assert is_valid_sig_format(sig_rogue)
        assert not verify_bytes(IMAGE, sig_rogue, DEFAULT_PUBLIC_KEY_RING)

    def test_signing_key_ring_prepends_custom_active_private_keys_public_half(self) -> None:
        custom_priv, custom_pub = generate_keypair()
        ring = SigningKeyRing.from_config(
            private_key=custom_priv,
            public_keys=f"{DEFAULT_PRIMARY_PUBLIC_KEY_PEM};{DEFAULT_SECONDARY_PUBLIC_KEY_PEM}",
        )
        assert ring.can_sign
        sig = ring.sign_digest(IMAGE_SHA256)
        assert ring.verify_digest(IMAGE_SHA256, sig)
        assert custom_pub in ring.public_keys_pem
        assert len(ring.public_keys_pem) == 3

    def test_cli_keygen_sign_and_verify_round_trip(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fw_file = tmp_path / "app.bin"
        fw_file.write_bytes(IMAGE)

        assert _cli(["keygen", "--out-dir", str(tmp_path)]) == 0
        capsys.readouterr()
        priv_file = tmp_path / "signing-key.pem"
        pub_file = tmp_path / "signing-pub.pem"
        assert priv_file.exists()
        assert pub_file.exists()

        assert _cli(["sign", str(fw_file), "--key", str(priv_file)]) == 0
        sig = capsys.readouterr().out.strip()
        assert is_valid_sig_format(sig)

        assert (
            _cli(
                [
                    "verify",
                    str(fw_file),
                    sig,
                    "--pubkey",
                    str(pub_file),
                ]
            )
            == 0
        )
        other_pub_file = tmp_path / "other-pub.pem"
        other_pub_file.write_text(DEFAULT_PRIMARY_PUBLIC_KEY_PEM, encoding="utf-8")
        assert (
            _cli(
                [
                    "verify",
                    str(fw_file),
                    sig,
                    "--pubkey",
                    str(other_pub_file),
                ]
            )
            == 1
        )

    def test_stage_payload_includes_sig_only_when_present(self) -> None:
        sig = sign_digest(IMAGE_SHA256)
        unsigned = stage_payload(
            cmd_id="c1",
            url=SIGNED_URL,
            sha256=IMAGE_SHA256,
            size=len(IMAGE),
            version="1.5.0",
            apply="auto",
            confirm_timeout_s=300,
        )
        assert "sig" not in unsigned["artifact"]

        signed = stage_payload(
            cmd_id="c1",
            url=SIGNED_URL,
            sha256=IMAGE_SHA256,
            size=len(IMAGE),
            version="1.5.0",
            apply="auto",
            confirm_timeout_s=300,
            sig=sig,
        )
        assert signed["artifact"]["sig"] == sig


# ===========================================================================
# Pure C firmware source tripwires: R6-SEC-1, R6-SEC-2, R6-FW-1
# ===========================================================================


class TestCFirmwareSecurityAndRangeResumeTripwires:
    def test_ff_mqtt_validates_artifact_sig_before_starting_ota(self) -> None:
        mqtt_c = FF_MQTT_C.read_text(encoding="utf-8")
        ota_h = FF_OTA_H.read_text(encoding="utf-8")

        assert "FF_OTA_MAX_SIG" in ota_h
        assert "char sig[FF_OTA_MAX_SIG];" in ota_h
        assert "is_valid_artifact_sig" in mqtt_c
        assert '"artifact sig malformed"' in mqtt_c
        # Refusal must happen in `on_stage` before `ff_ota_start(&cmd)`.
        assert mqtt_c.index('"artifact sig malformed"') < mqtt_c.index("ff_ota_start(&cmd)")

    def test_ff_ota_verifies_ecdsa_signature_across_key_ring_before_finish(self) -> None:
        ota_c = FF_OTA_C.read_text(encoding="utf-8")

        assert "s_signing_pubkey_ring" in ota_c
        assert "verify_artifact_signature" in ota_c
        assert "mbedtls_pk_verify" in ota_c
        assert '"signature invalid"' in ota_c
        assert '"signature missing"' in ota_c
        # Signature verification in `ota_task` must precede `esp_https_ota_finish(https_ota)`.
        task_body = ota_c[ota_c.index("static void ota_task") :]
        assert task_body.index("verify_artifact_signature(") < task_body.index(
            "esp_https_ota_finish("
        )

    def test_ff_ota_uses_partial_http_range_download_and_resume_backoff(self) -> None:
        ota_c = FF_OTA_C.read_text(encoding="utf-8")

        assert ".partial_http_download = true" in ota_c
        assert ".max_http_request_size = OTA_RANGE_CHUNK_BYTES" in ota_c
        assert ".http_client_init_cb = ota_http_client_init_cb" in ota_c
        assert 'esp_http_client_set_header(s_ota_http_client, "Range", s_resume_range_hdr)' in ota_c
        assert "esp_http_client_close(s_ota_http_client)" in ota_c
        assert "esp_http_client_open(s_ota_http_client, 0)" in ota_c
        assert "esp_http_client_fetch_headers(s_ota_http_client)" in ota_c
        assert "status != 206" in ota_c
        assert "FF_OTA_PRODUCTION_BUILD" in ota_c
        assert "resume_range_perform" in ota_c
        assert '"bytes=%d-"' in ota_c
        assert "OTA_MAX_RETRIES" in ota_c
        assert "OTA_RETRY_BASE_MS" in ota_c


# ===========================================================================
# Pure simulator tests: R6-SEC-1, R6-SEC-2, R6-FW-1
# ===========================================================================


def _sim_stage_bytes(*, sig: object = None, include_sig: bool = False) -> bytes:
    artifact: dict[str, object] = {
        "url": SIGNED_URL,
        "sha256": IMAGE_SHA256,
        "size": len(IMAGE),
        "version": "2.0.0",
    }
    if include_sig or sig is not None:
        artifact["sig"] = sig
    return json.dumps(
        {
            "id": "cmd-r6-1",
            "type": "stage",
            "artifact": artifact,
            "apply": "auto",
            "confirm_timeout_s": 300,
        }
    ).encode("utf-8")


async def _drive_sim_stage(
    cmd_bytes: bytes,
    *,
    require_signature: bool = False,
) -> tuple[StageRunner, list[dict[str, Any]], list[str]]:
    identity = DeviceIdentity(device_id=DEVICE_ID, fw_version="1.4.2")
    fake = FakeClient()
    fake.inbox.put_nowait(FakeMessage(f"ff/v1/d/{DEVICE_ID}/dn/cmd", cmd_bytes))
    stage = StageRunner(
        identity=identity,
        link=LinkProfile(LINK_FAST),
        safe_window=SAFE_WINDOW_AUTO,
        require_signature=require_signature,
    )
    lines, step = transcript()
    await run_session(
        identity,
        Credential(
            device_id=DEVICE_ID,
            mqtt_username=DEVICE_ID,
            mqtt_password="secret",
            api_base="https://api.example.invalid",
            enrolled_at="2026-10-22T00:00:00Z",
        ),
        client_factory=lambda: fake,  # type: ignore[arg-type,return-value]
        link=LinkProfile(LINK_FAST),
        heartbeat_interval_s=10.0,
        awake_s=0.3,
        stage=stage,
        step=step,
    )
    reports = [
        json.loads(payload)
        for topic, payload, _, _ in fake.published
        if topic == up_topic(DEVICE_ID, STATUS)
    ]
    return stage, reports, lines


class _RangeResponse:
    def __init__(self, data: bytes, *, status: int = 200, fail_after: int | None = None) -> None:
        self._data = data
        self.status = status
        self._fail_after = fail_after
        self._offset = 0

    def __enter__(self) -> _RangeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if self._fail_after is not None and self._offset >= self._fail_after:
            raise OSError("simulated mid-stream link drop")
        limit = len(self._data) - self._offset if size < 0 else size
        if self._fail_after is not None:
            limit = min(limit, self._fail_after - self._offset)
        chunk = self._data[self._offset : self._offset + limit]
        self._offset += len(chunk)
        return chunk


class TestSimulatorSigningAndRangeResume:
    @pytest.mark.parametrize(
        "priv_pem",
        [DEFAULT_PRIMARY_PRIVATE_KEY_PEM, DEFAULT_SECONDARY_PRIVATE_KEY_PEM],
        ids=["primary_key", "secondary_key_rotation"],
    )
    async def test_signed_artifact_verifies_and_applies_across_key_rotation_ring(
        self, monkeypatch: pytest.MonkeyPatch, priv_pem: str
    ) -> None:
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda _url, timeout=None: _RangeResponse(IMAGE),
        )
        sig = sign_bytes(IMAGE, priv_pem)
        stage, reports, lines = await _drive_sim_stage(
            _sim_stage_bytes(sig=sig), require_signature=True
        )

        assert [r["state"] for r in reports] == list(STAGE_WALK)
        assert stage.identity.fw_version == "2.0.0"
        assert any("ecdsa-p256-sha256 signature verified" in line for line in lines)

    async def test_malformed_signature_is_refused_before_any_download(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetched: list[object] = []

        def record_and_return(url: object, timeout: float | None = None) -> _RangeResponse:
            fetched.append(url)
            return _RangeResponse(IMAGE)

        monkeypatch.setattr("urllib.request.urlopen", record_and_return)
        _, reports, _ = await _drive_sim_stage(_sim_stage_bytes(sig="not-a-valid-sig"))

        assert [r["state"] for r in reports] == [STATE_FAILED]
        assert reports[-1]["detail"] == "artifact sig malformed"
        assert fetched == []

    async def test_wrong_key_or_tampered_signature_fails_at_verify_and_never_applies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda _url, timeout=None: _RangeResponse(IMAGE),
        )
        rogue_priv, _ = generate_keypair()
        rogue_sig = sign_bytes(IMAGE, rogue_priv)
        stage, reports, _ = await _drive_sim_stage(_sim_stage_bytes(sig=rogue_sig))

        assert [r["state"] for r in reports] == [
            STATE_STAGING,
            STATE_DOWNLOADING,
            STATE_VERIFYING,
            STATE_FAILED,
        ]
        assert reports[-1]["detail"] == "signature invalid"
        assert stage.identity.fw_version == "1.4.2"

    async def test_unsigned_artifact_is_rejected_when_require_signature_is_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda _url, timeout=None: _RangeResponse(IMAGE),
        )
        stage, reports, _ = await _drive_sim_stage(_sim_stage_bytes(), require_signature=True)

        assert [r["state"] for r in reports] == [
            STATE_STAGING,
            STATE_DOWNLOADING,
            STATE_VERIFYING,
            STATE_FAILED,
        ]
        assert reports[-1]["detail"] == "signature missing"
        assert stage.identity.fw_version == "1.4.2"

    async def test_interrupted_download_resumes_with_http_range_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        range_headers: list[str | None] = []
        cut_offset = 64 * 1024

        def flaky_urlopen(
            req_or_url: str | urllib.request.Request, timeout: float | None = None
        ) -> _RangeResponse:
            if isinstance(req_or_url, urllib.request.Request):
                hdr = req_or_url.get_header("Range")
                range_headers.append(hdr)
                assert hdr == f"bytes={cut_offset}-"
                return _RangeResponse(IMAGE[cut_offset:], status=206)
            range_headers.append(None)
            return _RangeResponse(IMAGE, status=200, fail_after=cut_offset)

        monkeypatch.setattr("urllib.request.urlopen", flaky_urlopen)
        sig = sign_bytes(IMAGE, DEFAULT_PRIMARY_PRIVATE_KEY_PEM)
        stage, reports, lines = await _drive_sim_stage(
            _sim_stage_bytes(sig=sig), require_signature=True
        )

        assert range_headers == [None, f"bytes={cut_offset}-"]
        assert [r["state"] for r in reports] == list(STAGE_WALK)
        assert stage.identity.fw_version == "2.0.0"
        assert any(f"retry #1/3 from offset {cut_offset}" in line for line in lines)


# ===========================================================================
# Pure unit tests: R4-test-1 protocol, R6-BE-1 KPI math, R6-SEC-1 precheck
# ===========================================================================


class _FakeKpiRow:
    def __init__(self, device_id: str, cmd_id: str, ended_state: str | None) -> None:
        self.device_id = device_id
        self.cmd_id = cmd_id
        self.ended_state = ended_state


class _FakeKpiResult:
    def __init__(self, rows: list[_FakeKpiRow]) -> None:
        self._rows = rows

    def all(self) -> list[_FakeKpiRow]:
        return self._rows


class _FakeKpiSession:
    def __init__(self, rows: list[_FakeKpiRow]) -> None:
        self._rows = rows

    async def execute(self, *_args: object, **_kwargs: object) -> _FakeKpiResult:
        return _FakeKpiResult(self._rows)


class TestDeployKpisAndPrecheckPure:
    async def test_deploy_kpis_computes_delivery_success_fleet_safety_and_rollback_gap(
        self,
    ) -> None:
        empty = await deploy_kpis(_FakeKpiSession([]))  # type: ignore[arg-type]
        assert empty.total_completed == 0
        assert empty.delivery_success_pct is None
        assert empty.fleet_safety_pct is None
        assert empty.safety_gap_pct is None
        assert empty.delivery_target_met is None
        assert empty.safety_target_met is None

        rows = [
            *[_FakeKpiRow(DEVICE_ID, f"c-{i}", "confirmed") for i in range(9)],
            _FakeKpiRow(SECOND_DEVICE_ID, "c-rb", "rolled_back"),
            _FakeKpiRow(THIRD_DEVICE_ID, "c-cancel", "cancelled"),
            _FakeKpiRow(THIRD_DEVICE_ID, "c-open", None),
        ]
        kpis = await deploy_kpis(_FakeKpiSession(rows))  # type: ignore[arg-type]
        summary = DeployKpisSummary(**asdict(kpis))
        assert summary.total_completed == 10
        assert summary.confirmed == 9
        assert summary.rolled_back == 1
        assert summary.failed == 0
        assert summary.cancelled == 1
        assert summary.in_flight == 1
        assert summary.delivery_success_pct == 90.0
        assert summary.fleet_safety_pct == 100.0
        assert summary.safety_gap_pct == 10.0
        assert summary.delivery_target_met is True
        assert summary.safety_target_met is True

        with_failure = await deploy_kpis(
            _FakeKpiSession([*rows, _FakeKpiRow(DEVICE_ID, "c-fail", "failed")])  # type: ignore[arg-type]
        )
        assert with_failure.total_completed == 11
        assert with_failure.failed == 1
        assert with_failure.delivery_target_met is False
        assert with_failure.safety_target_met is False

    def test_deploy_precheck_carries_resolved_artifact_sig_and_schemas_expose_sig(self) -> None:
        device = Device(
            device_id=DEVICE_ID,
            platform_type="esp32c6",
            link_type="wifi",
            power_class="always_on",
            fw_version="1.4.2",
            partition_layout="ab-4m-v1",
            ota_slot_size=1966080,
            capabilities=["ota"],
            presence_reported=True,
            last_seen=now_utc(),
        )
        unsigned = ResolvedArtifact(
            sha256=IMAGE_SHA256,
            size_bytes=len(IMAGE),
            partition_layout="ab-4m-v1",
            has_lib_marker=True,
            sig=None,
        )
        assert warnings(device, online=True, artifact=unsigned) == []

        sig = sign_bytes(IMAGE, DEFAULT_PRIMARY_PRIVATE_KEY_PEM)
        signed = ResolvedArtifact(
            sha256=IMAGE_SHA256,
            size_bytes=len(IMAGE),
            partition_layout="ab-4m-v1",
            has_lib_marker=True,
            sig=sig,
        )
        assert signed.sig == sig
        assert warnings(device, online=True, artifact=signed) == []

        summary = ArtifactSummary(
            target="esp32c6",
            version="2.0.0",
            sha256=IMAGE_SHA256,
            size_bytes=len(IMAGE),
            kind="user_firmware",
            partition_layout="ab-4m-v1",
            sig=sig,
            created_at=now_utc(),
        )
        uploaded = ArtifactUploaded(
            target="esp32c6",
            version="2.0.0",
            sha256=IMAGE_SHA256,
            size_bytes=len(IMAGE),
            partition_layout="ab-4m-v1",
            has_lib_marker=True,
            sig=sig,
            created=True,
        )
        assert summary.sig == sig
        assert uploaded.sig == sig

    def test_r4_telemetry_and_log_wire_parsing_pure(self) -> None:
        telem = decode(
            TelemetryPayload,
            f"ff/v1/d/{DEVICE_ID}/up/telemetry",
            json.dumps(
                {
                    "dropped": 0,
                    "samples": [{"name": "battery_mv", "value": 3985.0, "age_ms": 200}],
                }
            ).encode("utf-8"),
        )
        assert telem is not None
        assert telem.dropped == 0
        assert len(telem.samples) == 1
        assert telem.samples[0].name == "battery_mv"
        assert telem.samples[0].value == 3985.0
        assert telem.samples[0].age_ms == 200

        log_batch = decode(
            LogPayload,
            f"ff/v1/d/{DEVICE_ID}/up/log",
            json.dumps(
                {
                    "dropped": 0,
                    "lines": [{"level": "I", "text": "radio calibrated ok", "age_ms": 100}],
                }
            ).encode("utf-8"),
        )
        assert log_batch is not None
        assert log_batch.dropped == 0
        assert len(log_batch.lines) == 1
        assert log_batch.lines[0].level == "I"
        assert log_batch.lines[0].text == "radio calibrated ok"
        assert log_batch.lines[0].age_ms == 100


# ===========================================================================
# Database + API E2E tests: R4-test-1, R6-SEC-1, R6-SEC-2, R6-BE-1, R6-TEST-1
# ===========================================================================


@pytest.fixture
def store(admin_app: FastAPI) -> Iterator[MemoryObjectStore]:
    mem = MemoryObjectStore()
    admin_app.dependency_overrides[get_object_store] = lambda: mem
    yield mem
    admin_app.dependency_overrides.pop(get_object_store, None)


@pytest.fixture
def publisher(admin_app: FastAPI) -> Iterator[FakeCommandPublisher]:
    pub = FakeCommandPublisher()
    admin_app.dependency_overrides[get_command_publisher] = lambda: pub
    yield pub
    admin_app.dependency_overrides.pop(get_command_publisher, None)


@pytest.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await _cleanup(session)
        try:
            yield session
        finally:
            await session.rollback()
            await _cleanup(session)


async def _cleanup(session: AsyncSession) -> None:
    await session.execute(delete(DeviceTelemetry))
    await session.execute(delete(DeviceMetric))
    await session.execute(delete(DeviceLog))
    await session.execute(delete(DeployEvent))
    await session.execute(delete(Device))
    await session.execute(text("DELETE FROM artifact_versions"))
    await session.execute(text("DELETE FROM artifacts"))
    await session.commit()


class TestR4LiveTelemetryAndLogsToDashboard:
    """R4-test-1: A metric and a log line emitted on a device reach the dashboard live."""

    async def test_emitted_metric_and_log_line_reach_dashboard_read_models_and_emit_sse_hints(
        self, admin_app: FastAPI, db: AsyncSession
    ) -> None:
        now = now_utc()
        device = Device(
            device_id=DEVICE_ID,
            platform_type="esp32c6",
            link_type="wifi",
            power_class="always_on",
            fw_version="1.4.2",
            presence_reported=True,
            last_seen=now,
        )
        db.add(device)
        await db.commit()

        # 1. Board emits `up/telemetry` with a metric sample (`battery_mv = 3985.0`).
        telemetry_event = await handle_up_message(
            db,
            topic=f"ff/v1/d/{DEVICE_ID}/up/telemetry",
            payload=json.dumps(
                {
                    "dropped": 0,
                    "samples": [{"name": "battery_mv", "value": 3985.0, "age_ms": 200}],
                }
            ).encode("utf-8"),
            retained=False,
            received_at=now,
            tolerance=2.5,
        )
        await db.commit()
        assert telemetry_event is not None
        assert telemetry_event.type is EventType.DEVICE_UPDATED

        # 2. Board emits `up/log` with a live log line (`"radio calibrated ok"`).
        log_event = await handle_up_message(
            db,
            topic=f"ff/v1/d/{DEVICE_ID}/up/log",
            payload=json.dumps(
                {
                    "dropped": 0,
                    "lines": [{"level": "I", "text": "radio calibrated ok", "age_ms": 100}],
                }
            ).encode("utf-8"),
            retained=False,
            received_at=now,
            tolerance=2.5,
        )
        await db.commit()
        assert log_event is not None
        assert log_event.type is EventType.DEVICE_UPDATED

        # 3. Dashboard re-reads `GET /v1/devices`, `GET /v1/devices/{id}/telemetry`, and
        #    `GET /v1/devices/{id}/logs` on the SSE hint and sees both live.
        token = await login_admin(admin_app)
        headers = {"Authorization": f"Bearer {token}"}
        async with client_for(admin_app) as client:
            fleet_res = await client.get("/v1/devices", headers=headers)
            series_res = await client.get(
                f"/v1/devices/{DEVICE_ID}/telemetry",
                params={"name": "battery_mv"},
                headers=headers,
            )
            logs_res = await client.get(f"/v1/devices/{DEVICE_ID}/logs", headers=headers)

        assert fleet_res.status_code == 200
        row = fleet_res.json()["devices"][0]
        assert row["metrics"] == [
            {
                "name": "battery_mv",
                "value": 3985.0,
                "at": row["metrics"][0]["at"],
            }
        ]

        assert series_res.status_code == 200
        assert series_res.json()["samples"][0]["value"] == 3985.0

        assert logs_res.status_code == 200
        lines = logs_res.json()["lines"]
        assert len(lines) == 1
        assert lines[0]["level"] == "I"
        assert lines[0]["text"] == "radio calibrated ok"


class TestR6ApiArtifactSigningAndDeploy:
    """R6-SEC-1 & R6-SEC-2 API integration: upload verification, auto-signing, and stage_payload."""

    async def test_detached_signature_is_verified_stored_and_forwarded_in_stage_command(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
        publisher: FakeCommandPublisher,
    ) -> None:
        db.add(
            Device(
                device_id=DEVICE_ID,
                platform_type="esp32",
                link_type="wifi",
                power_class="always_on",
                fw_version="1.4.2",
                partition_layout="ab-4m-v1",
                ota_slot_size=1966080,
                capabilities=["ota"],
                presence_reported=True,
                last_seen=now_utc(),
            )
        )
        await db.commit()

        token = await login_admin(admin_app)
        headers = {"Authorization": f"Bearer {token}"}
        # Sign with the secondary key in the rotation ring to prove key rotation end-to-end.
        sig = sign_bytes(IMAGE, DEFAULT_SECONDARY_PRIVATE_KEY_PEM)

        async with client_for(admin_app, base_url="https://testserver") as client:
            up_res = await client.post(
                "/v1/artifact",
                params={"target": "esp32", "version": "2.0.0", "sig": sig},
                content=IMAGE,
                headers=headers,
            )
            assert up_res.status_code == 201, up_res.text
            assert up_res.json()["sig"] == sig

            list_res = await client.get("/v1/artifact", headers=headers)
            assert list_res.status_code == 200
            assert list_res.json()["artifacts"][0]["sig"] == sig

            dep_res = await client.post(
                f"/v1/devices/{DEVICE_ID}/deploy",
                json={"version": "2.0.0", " override": ["no_library_marker"]}.copy()
                if False
                else {"version": "2.0.0", "override": ["no_library_marker"]},
                headers=headers,
            )
            assert dep_res.status_code == 202, dep_res.text

        (_dev_id, published_payload) = publisher.published[0]
        assert published_payload["artifact"]["sig"] == sig

    async def test_invalid_or_malformed_detached_signature_is_rejected_before_storing_blob(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
    ) -> None:
        token = await login_admin(admin_app)
        headers = {"Authorization": f"Bearer {token}"}
        rogue_priv, _ = generate_keypair()
        rogue_sig = sign_bytes(IMAGE, rogue_priv)

        async with client_for(admin_app, base_url="https://testserver") as client:
            bad_fmt = await client.post(
                "/v1/artifact",
                params={"target": "esp32", "version": "2.0.0", "sig": "bad-sig"},
                content=IMAGE,
                headers=headers,
            )
            bad_key = await client.post(
                "/v1/artifact",
                params={"target": "esp32", "version": "2.0.0", "sig": rogue_sig},
                content=IMAGE,
                headers=headers,
            )

        assert bad_fmt.status_code == 400
        assert bad_key.status_code == 422
        assert store.puts == []

    async def test_server_auto_signs_upload_when_artifact_signing_key_is_configured(
        self,
        admin_app: FastAPI,
        db: AsyncSession,
        store: MemoryObjectStore,
    ) -> None:
        token = await login_admin(admin_app)
        headers = {"Authorization": f"Bearer {token}"}
        prev = admin_app.dependency_overrides.get(get_settings)
        admin_app.dependency_overrides[get_settings] = lambda: settings_for_tests(
            artifact_signing_key=DEFAULT_PRIMARY_PRIVATE_KEY_PEM
        )
        try:
            async with client_for(admin_app, base_url="https://testserver") as client:
                res = await client.post(
                    "/v1/artifact",
                    params={"target": "esp32", "version": "2.1.0"},
                    content=IMAGE,
                    headers=headers,
                )
        finally:
            if prev is None:
                admin_app.dependency_overrides.pop(get_settings, None)
            else:
                admin_app.dependency_overrides[get_settings] = prev

        assert res.status_code == 201, res.text
        sig = res.json()["sig"]
        assert verify_bytes(IMAGE, sig, DEFAULT_PUBLIC_KEY_RING)


class TestR6DeployKpis:
    """R6-BE-1 & R6-TEST-1: Delivery success (>= 90%) and Fleet safety (= 100%) KPIs."""

    async def test_kpis_measure_delivery_success_fleet_safety_and_rollback_gap(
        self, admin_app: FastAPI, db: AsyncSession
    ) -> None:
        now = now_utc()
        for dev_id in (DEVICE_ID, SECOND_DEVICE_ID, THIRD_DEVICE_ID):
            db.add(
                Device(
                    device_id=dev_id,
                    platform_type="esp32c6",
                    link_type="wifi",
                    power_class="always_on",
                    fw_version="1.4.2",
                )
            )
        await db.commit()

        sender = DeploySender(subject="admin", token_id="t1", credential="admin")

        # 9 confirmed deploys + 1 rolled_back deploy:
        # -> delivery_success_pct = 90.0% (target >= 90% met!)
        # -> fleet_safety_pct = 100.0% (target = 100% met!)
        # -> safety_gap_pct = 10.0% (1 rollback save)
        for i in range(9):
            cmd_id = f"cmd-ok-{i}"
            await record_requested(
                db,
                device_id=DEVICE_ID,
                cmd_id=cmd_id,
                artifact_version="1.5.0",
                from_version="1.4.2",
                sha256=IMAGE_SHA256,
                size_bytes=len(IMAGE),
                target="esp32c6",
                apply="auto",
                sent_by=sender,
            )
            await record_observed_status(
                db,
                device_id=DEVICE_ID,
                cmd_id=cmd_id,
                state=DeployState.CONFIRMED.value,
                at=now,
                pct=100,
            )

        # 1 rolled_back deploy (board automatically saved by A/B rollback)
        await record_requested(
            db,
            device_id=SECOND_DEVICE_ID,
            cmd_id="cmd-rb-1",
            artifact_version="1.6.0-broken",
            from_version="1.5.0",
            sha256=IMAGE_SHA256,
            size_bytes=len(IMAGE),
            target="esp32c6",
            apply="auto",
            sent_by=sender,
        )
        await record_observed_status(
            db,
            device_id=SECOND_DEVICE_ID,
            cmd_id="cmd-rb-1",
            state=DeployState.ROLLED_BACK.value,
            at=now,
            detail="selftest failed: simulated; returned to ota_0",
        )

        # 1 cancelled deploy (excluded from KPI denominator per `counts_in_deploy_kpis`)
        await record_requested(
            db,
            device_id=THIRD_DEVICE_ID,
            cmd_id="cmd-cancel-1",
            artifact_version="1.5.0",
            from_version="1.4.2",
            sha256=IMAGE_SHA256,
            size_bytes=len(IMAGE),
            target="esp32c6",
            apply="on_command",
            sent_by=sender,
        )
        await record_observed_status(
            db,
            device_id=THIRD_DEVICE_ID,
            cmd_id="cmd-cancel-1",
            state=DeployState.CANCELLED.value,
            at=now,
        )

        # 1 in-flight deploy (awaiting_safe_window — not terminal yet)
        await record_requested(
            db,
            device_id=THIRD_DEVICE_ID,
            cmd_id="cmd-inflight-1",
            artifact_version="1.5.0",
            from_version="1.4.2",
            sha256=IMAGE_SHA256,
            size_bytes=len(IMAGE),
            target="esp32c6",
            apply="auto",
            sent_by=sender,
        )
        await record_observed_status(
            db,
            device_id=THIRD_DEVICE_ID,
            cmd_id="cmd-inflight-1",
            state=DeployState.AWAITING_SAFE_WINDOW.value,
            at=now,
        )

        # 1 standalone rollback verb transaction (no `requested` row -> excluded from KPIs)
        await record_observed_status(
            db,
            device_id=DEVICE_ID,
            cmd_id="verb-rollback-only",
            state=DeployState.ROLLED_BACK.value,
            at=now,
            detail="rolled back by command; returned to ota_0",
        )

        # Soft-delete SECOND_DEVICE_ID to verify decommissioned device evidence still counts.
        second_dev = await db.get(Device, SECOND_DEVICE_ID)
        assert second_dev is not None
        second_dev.decommissioned_at = now
        await db.commit()

        kpis = await deploy_kpis(db)
        assert kpis.total_completed == 10
        assert kpis.confirmed == 9
        assert kpis.rolled_back == 1
        assert kpis.failed == 0
        assert kpis.cancelled == 1
        assert kpis.in_flight == 1
        assert kpis.delivery_success_pct == 90.0
        assert kpis.fleet_safety_pct == 100.0
        assert kpis.safety_gap_pct == 10.0
        assert kpis.delivery_target_met is True
        assert kpis.safety_target_met is True

        # Verify `GET /v1/devices/kpis` returns the exact same numbers.
        token = await login_admin(admin_app)
        async with client_for(admin_app) as client:
            res = await client.get("/v1/devices/kpis", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200, res.text
        assert res.json() == {
            "total_completed": 10,
            "confirmed": 9,
            "rolled_back": 1,
            "failed": 0,
            "cancelled": 1,
            "in_flight": 1,
            "delivery_success_pct": 90.0,
            "fleet_safety_pct": 100.0,
            "safety_gap_pct": 10.0,
            "delivery_target_pct": 90.0,
            "safety_target_pct": 100.0,
            "delivery_target_met": True,
            "safety_target_met": True,
        }

        # Now record a failed deploy (`publish_failed`) and verify safety drops below 100%.
        await record_requested(
            db,
            device_id=DEVICE_ID,
            cmd_id="cmd-fail-1",
            artifact_version="1.7.0",
            from_version="1.5.0",
            sha256=IMAGE_SHA256,
            size_bytes=len(IMAGE),
            target="esp32c6",
            apply="auto",
            sent_by=sender,
        )
        await record_publish_failure(
            db,
            device_id=DEVICE_ID,
            cmd_id="cmd-fail-1",
            artifact_version="1.7.0",
            from_version="1.5.0",
        )
        await db.commit()

        after_fail = await deploy_kpis(db)
        assert after_fail.total_completed == 11
        assert after_fail.failed == 1
        assert after_fail.delivery_target_met is False
        assert after_fail.safety_target_met is False
