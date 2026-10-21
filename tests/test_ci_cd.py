"""Unit tests for Pillar 1: Git-Driven CI/CD, `dn/cfg` retained config, and CLI (`CI-CD-be-1`, `CI-CD-cli-1`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from fleetforge.api.routers.deploys import matches_project_tracking
from fleetforge.api.schemas import (
    ArtifactSummary,
    ArtifactUploaded,
    DeviceSummary,
    DeviceUpdate,
    ProjectDeployRequest,
    ProjectSummary,
    ProjectUpdate,
)
from fleetforge.broker import (
    CommandPublishError,
    NullCommandPublisher,
    cfg_payload,
    cfg_topic,
)
from fleetforge.broker.publisher import CFG_RETAIN, QOS, RETAIN, MqttCommandPublisher
from fleetforge.cli import build_artifact_upload_url, detect_git_metadata, main
from fleetforge.db.models import Device, Project
from fleetforge.ingestor.otel import (
    _resolve_endpoint,
    build_otlp_logs_payload,
    build_otlp_metrics_payload,
    parse_otel_headers,
)
from fleetforge.ingestor.protocol import AnnouncePayload, LogLine, TelemetrySample
from fleetforge.secrets_crypto import decrypt_secret, encrypt_secret
from tests.test_broker_commands import DEVICE_ID, FakeMqttClient


class TestProjectTrackingMatcher:
    def _project(self, **overrides: Any) -> Project:
        values: dict[str, Any] = {
            "slug": "eink-frame",
            "name": "E-Ink Frame",
            "repo_url": "https://github.com/tvaroska/eink-frame.git",
            "track_mode": "branch",
            "track_ref": "main",
            "auto_deploy_target": "dev",
        }
        values.update(overrides)
        return Project(**values)

    def test_branch_tracking_matches_exact_branch_and_normalizes_repo_url(self) -> None:
        proj = self._project(track_mode="branch", track_ref="main")
        assert matches_project_tracking(
            proj,
            repo_url="https://github.com/tvaroska/eink-frame/",
            branch="main",
            commit_sha="a1b2c3d4",
            tag=None,
            test_passed=True,
        )
        assert not matches_project_tracking(
            proj,
            repo_url="https://github.com/tvaroska/eink-frame",
            branch="feature/weather",
            commit_sha="a1b2c3d4",
            tag=None,
            test_passed=True,
        )

    def test_failed_tests_never_auto_deploy(self) -> None:
        proj = self._project(track_mode="branch", track_ref="main")
        assert not matches_project_tracking(
            proj,
            repo_url=None,
            branch="main",
            commit_sha="a1b2c3d4",
            tag=None,
            test_passed=False,
        )

    def test_tag_glob_tracking_matches_wildcard_and_exact_tags(self) -> None:
        proj = self._project(track_mode="tag", track_ref="v*")
        assert matches_project_tracking(
            proj,
            repo_url=None,
            branch=None,
            commit_sha="a1b2c3d4",
            tag="v1.2.0",
            test_passed=True,
        )
        assert not matches_project_tracking(
            proj,
            repo_url=None,
            branch="main",
            commit_sha="a1b2c3d4",
            tag="nightly-20261020",
            test_passed=True,
        )

    def test_commit_tracking_matches_full_sha_or_prefix(self) -> None:
        proj = self._project(track_mode="commit", track_ref="a1b2c3d")
        assert matches_project_tracking(
            proj,
            repo_url=None,
            branch="main",
            commit_sha="a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4",
            tag=None,
            test_passed=None,
        )
        assert not matches_project_tracking(
            proj,
            repo_url=None,
            branch="main",
            commit_sha="deadbeef0000",
            tag=None,
            test_passed=True,
        )

    def test_off_mode_or_off_target_never_matches(self) -> None:
        assert not matches_project_tracking(
            self._project(track_mode="off", track_ref="main"),
            repo_url=None,
            branch="main",
            commit_sha="a1b2c3d",
            tag=None,
            test_passed=True,
        )
        assert not matches_project_tracking(
            self._project(track_mode="branch", track_ref="main", auto_deploy_target="off"),
            repo_url=None,
            branch="main",
            commit_sha="a1b2c3d",
            tag=None,
            test_passed=True,
        )

    def test_different_repo_url_is_rejected_when_project_pins_repo(self) -> None:
        proj = self._project(repo_url="https://github.com/tvaroska/eink-frame")
        assert not matches_project_tracking(
            proj,
            repo_url="https://github.com/other/repo",
            branch="main",
            commit_sha="a1b2c3d",
            tag=None,
            test_passed=True,
        )


class TestArtifactAndProjectSchemas:
    def test_artifact_uploaded_omits_unset_ci_fields_and_keeps_set_ones(self) -> None:
        plain = ArtifactUploaded(
            sha256="a" * 64,
            size_bytes=1024,
            target="esp32c6",
            version="1.0.0",
            partition_layout="ab-4m-v1",
            created=True,
            has_lib_marker=True,
        )
        assert set(plain.model_dump()) == {
            "sha256",
            "size_bytes",
            "target",
            "version",
            "partition_layout",
            "created",
            "has_lib_marker",
        }

        with_ci = ArtifactUploaded(
            sha256="a" * 64,
            size_bytes=1024,
            target="esp32c6",
            version="1.0.0-a1b2c3d",
            partition_layout="ab-4m-v1",
            created=True,
            has_lib_marker=True,
            branch="main",
            commit_sha="a1b2c3d",
            test_passed=True,
            auto_deployed_devices=[DEVICE_ID],
        )
        dumped = with_ci.model_dump()
        assert dumped["branch"] == "main"
        assert dumped["commit_sha"] == "a1b2c3d"
        assert dumped["test_passed"] is True
        assert dumped["auto_deployed_devices"] == [DEVICE_ID]
        assert "tag" not in dumped

    def test_artifact_summary_omits_unset_ci_fields_and_keeps_set_ones(self) -> None:
        import datetime as dt

        now = dt.datetime.now(dt.UTC)
        plain = ArtifactSummary(
            target="esp32c6",
            version="1.0.0",
            sha256="b" * 64,
            size_bytes=2048,
            partition_layout="ab-4m-v1",
            kind="user_firmware",
            created_at=now,
        )
        assert set(plain.model_dump()) == {
            "target",
            "version",
            "sha256",
            "size_bytes",
            "partition_layout",
            "kind",
            "created_at",
        }

        with_ci = ArtifactSummary(
            target="esp32c6",
            version="1.0.0",
            sha256="b" * 64,
            size_bytes=2048,
            partition_layout="ab-4m-v1",
            kind="user_firmware",
            created_at=now,
            tag="v1.0.0",
            test_passed=True,
        )
        dumped = with_ci.model_dump()
        assert dumped["tag"] == "v1.0.0"
        assert dumped["test_passed"] is True

    def test_device_and_project_models_validate_cd_and_cfg_fields(self) -> None:
        patch_dev = DeviceUpdate(is_dev=True, stay_awake=True)
        assert patch_dev.is_dev is True
        assert patch_dev.stay_awake is True

        patch_proj = ProjectUpdate(
            repo_url="https://github.com/tvaroska/eink-frame",
            track_mode="branch",
            track_ref="main",
            auto_deploy_target="dev",
        )
        assert patch_proj.track_mode == "branch"
        assert patch_proj.auto_deploy_target == "dev"

        deploy_req = ProjectDeployRequest(version="1.2.0", target_filter="dev")
        assert deploy_req.target_filter == "dev"
        assert deploy_req.apply == "auto"

        # Ensure `deploy` remains the very last field on `DeviceSummary`.
        assert list(DeviceSummary.model_fields)[-1] == "deploy"
        assert "secrets_rev" in ProjectSummary.model_fields


class TestDnCfgPublisherAndAnnounceSecretsRev:
    def test_cfg_topic_and_payload_shape(self) -> None:
        assert cfg_topic(DEVICE_ID) == f"ff/v1/d/{DEVICE_ID}/dn/cfg"
        assert cfg_payload(secrets_rev=4, stay_awake=True) == {
            "proto": 1,
            "secrets_rev": 4,
            "stay_awake": True,
        }

    async def test_publish_cfg_is_retained_at_qos_1_while_cmd_remains_unretained(self) -> None:
        client = FakeMqttClient()
        pub = MqttCommandPublisher(client, timeout_s=5.0)  # type: ignore[arg-type]

        await pub.publish_cfg(DEVICE_ID, cfg_payload(secrets_rev=2, stay_awake=True))

        topic, raw, qos, retain = client.published[0]
        assert topic == f"ff/v1/d/{DEVICE_ID}/dn/cfg"
        assert json.loads(raw) == {"proto": 1, "secrets_rev": 2, "stay_awake": True}
        assert qos == 1
        assert retain is True
        assert (QOS, RETAIN, CFG_RETAIN) == (1, False, True)

    async def test_null_publisher_publish_cfg_raises(self) -> None:
        with pytest.raises(CommandPublishError):
            await NullCommandPublisher().publish_cfg(
                DEVICE_ID, cfg_payload(secrets_rev=1, stay_awake=False)
            )

    def test_announce_payload_tolerates_valid_and_malformed_secrets_rev(self) -> None:
        valid = AnnouncePayload.model_validate({"device_id": DEVICE_ID, "secrets_rev": 7})
        assert valid.secrets_rev == 7

        for bad in (-1, "seven", True, 2**40):
            parsed = AnnouncePayload.model_validate({"device_id": DEVICE_ID, "secrets_rev": bad})
            assert parsed.secrets_rev is None


class TestCli:
    def test_build_artifact_upload_url_encodes_provenance(self) -> None:
        url = build_artifact_upload_url(
            "https://fleetforge.example.com/",
            target="esp32c6",
            version="1.2.0-a1b2c3d",
            project="eink-frame",
            repo_url="https://github.com/tvaroska/eink-frame",
            branch="main",
            commit_sha="a1b2c3d4",
            test_passed=True,
        )
        assert url.startswith("https://fleetforge.example.com/v1/artifact?")
        assert "project=eink-frame" in url
        assert "branch=main" in url
        assert "commit_sha=a1b2c3d4" in url
        assert "test_passed=true" in url

    def test_cli_push_and_track_subcommands(self, tmp_path: Path) -> None:
        bin_file = tmp_path / "app.bin"
        bin_file.write_bytes(b"\xe9\x00" + b"x" * 128)

        calls: list[tuple[str, str, bytes | None]] = []

        def fake_request(
            method: str,
            url: str,
            *,
            token: str,
            body: bytes | None = None,
            content_type: str = "application/octet-stream",
        ) -> tuple[int, dict[str, Any]]:
            calls.append((method, url, body))
            if method == "POST" and "/v1/artifact?" in url:
                return 201, {
                    "sha256": "a" * 64,
                    "size_bytes": len(body or b""),
                    "target": "esp32c6",
                    "version": "1.2.0",
                    "partition_layout": "ab-4m-v1",
                    "created": True,
                    "has_lib_marker": True,
                    "auto_deployed_devices": [DEVICE_ID],
                }
            if method == "POST" and url.endswith("/deploy"):
                return 202, {
                    "project": "eink-frame",
                    "version": "1.2.0",
                    "target_filter": "dev",
                    "deployed": [],
                    "skipped": {},
                }
            return 200, {"slug": "eink-frame", "track_mode": "branch", "track_ref": "main"}

        with patch("fleetforge.cli._request_json", side_effect=fake_request):
            rc = main(
                [
                    "--url",
                    "http://localhost:8000",
                    "--token",
                    "ffa_test.token",
                    "push",
                    str(bin_file),
                    "--project",
                    "eink-frame",
                    "--target",
                    "esp32c6",
                    "--version",
                    "1.2.0",
                    "--branch",
                    "main",
                    "--commit",
                    "a1b2c3d",
                    "--test-passed",
                    "--no-git",
                    "--deploy",
                    "dev",
                ]
            )
            assert rc == 0
            assert len(calls) == 2
            assert "branch=main" in calls[0][1]
            assert calls[1][1] == "http://localhost:8000/v1/projects/eink-frame/deploy"

            rc_track = main(
                [
                    "--url",
                    "http://localhost:8000",
                    "--token",
                    "ffa_test.token",
                    "track",
                    "--project",
                    "eink-frame",
                    "--mode",
                    "branch",
                    "--ref",
                    "main",
                    "--auto-deploy",
                    "dev",
                ]
            )
            assert rc_track == 0
            assert calls[2][0] == "PATCH"
            assert calls[2][1] == "http://localhost:8000/v1/projects/eink-frame"

    def test_detect_git_metadata_runs_cleanly_in_repo(self) -> None:
        meta = detect_git_metadata(Path(__file__).resolve().parent)
        assert "commit_sha" in meta
        assert meta["commit_sha"] is None or len(meta["commit_sha"]) >= 7


class TestSecretsCryptoAndDeviceSecretsAuth:
    def test_encrypt_and_decrypt_secret_round_trip(self) -> None:
        ciphertext = encrypt_secret("sk-live-rotated-key-42", None)
        assert "sk-live-rotated-key-42" not in ciphertext
        assert decrypt_secret(ciphertext, None) == "sk-live-rotated-key-42"

    def test_parse_basic_auth_validates_device_id_and_password(self) -> None:
        import base64

        from fleetforge.api.routers.device_secrets import _parse_basic_auth

        token = base64.b64encode(f"{DEVICE_ID}:my-mqtt-pass:with:colons".encode()).decode("ascii")
        parsed = _parse_basic_auth(f"Basic {token}")
        assert parsed == (DEVICE_ID, "my-mqtt-pass:with:colons")

        # Rejects non-Basic, invalid base64, non-MAC device_id, or empty password
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
        import datetime as dt

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
        attrs = {a["key"]: a["value"]["stringValue"] for a in res_logs["resource"]["attributes"]}
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


class TestCuj2FirmwareAndTemplates:
    def test_firmware_pillars_and_templates_present(self) -> None:
        root = Path(__file__).resolve().parents[1] / "agent" / "components" / "fleetforge"
        ff_log_c = (root / "src" / "ff_log.c").read_text(encoding="utf-8")
        ff_sec_c = (root / "src" / "ff_sec.c").read_text(encoding="utf-8")
        ff_mqtt_c = (root / "src" / "ff_mqtt.c").read_text(encoding="utf-8")
        ff_wifi_c = (root / "src" / "ff_net_wifi.c").read_text(encoding="utf-8")
        ff_sleep_c = (root / "src" / "ff_sleep.c").read_text(encoding="utf-8")
        basic_ino = (root / "examples" / "Basic" / "Basic.ino").read_text(encoding="utf-8")
        basic_idf = (root / "examples" / "basic_idf" / "main" / "main.c").read_text(
            encoding="utf-8"
        )

        # Pillar 2: Rotating secrets in NVS ff_sec + HTTPS /v1/device-secrets
        assert "/v1/device-secrets" in ff_sec_c
        assert "ff_get_secret" in ff_sec_c
        assert "ff_sec_synced()" in ff_mqtt_c

        # Pillar 3: OTel-compatible up/log + RTC_NOINIT_ATTR crash ring + sleep drain gate
        assert "RTC_NOINIT_ATTR" in ff_log_c
        assert "[prev-boot] %s" in ff_log_c
        assert "batch[FF_LOG_CAPACITY]" not in ff_log_c
        assert "ff_log_drained()" in ff_sleep_c
        assert "-Wframe-larger-than=1024" in (root / "CMakeLists.txt").read_text(encoding="utf-8")

        # Pillar 4: NVS ff_net overlay + Improv Wi-Fi BLE GATT service
        assert "00467768-6228-2272-4663-277478268000" in ff_wifi_c
        assert "load_nvs_net_overlay" in ff_wifi_c
        assert "IMPROV_BLE_WINDOW_S" in ff_wifi_c
        assert "improv_ble_window_start" in ff_wifi_c

        # Starter templates demonstrate both logf/ff_logf and getSecret/ff_get_secret
        assert "Fleetforge.logf" in basic_ino
        assert "Fleetforge.getSecret" in basic_ino
        assert "ff_logf" in basic_idf
        assert "ff_get_secret" in basic_idf
