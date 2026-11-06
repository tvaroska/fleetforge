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

    def test_cli_new_scaffolds_project_and_keeps_immutables_in_sync(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        tpl_dir = repo_root / "templates" / "esp-idf"
        basic_idf = repo_root / "agent" / "components" / "fleetforge" / "examples" / "basic_idf"
        assert (tpl_dir / "partitions.csv").read_bytes() == (
            repo_root / "agent" / "partitions.csv"
        ).read_bytes()
        assert (tpl_dir / "main" / "fleetforge_start.c").read_bytes() == (
            basic_idf / "main" / "fleetforge_start.c"
        ).read_bytes()
        assert (tpl_dir / "main" / "fleetforge_start.h").read_bytes() == (
            basic_idf / "main" / "fleetforge_start.h"
        ).read_bytes()

        dest = tmp_path / "oled-status"
        rc = main(
            [
                "new",
                str(dest),
                "--target",
                "esp32c6",
                "--project",
                "oled-status",
                "--ref",
                "v0.8.4",
            ]
        )
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["app_name"] == "oled_status"
        assert out["project"] == "oled-status"
        assert out["target"] == "esp32c6"
        assert (dest / "AGENTS.md").is_file()
        assert (dest / "CLAUDE.md").exists()
        assert "project(oled_status)" in (dest / "CMakeLists.txt").read_text(encoding="utf-8")
        idf_yml = (dest / "main" / "idf_component.yml").read_text(encoding="utf-8")
        assert "https://github.com/tvaroska/fleetforge.git" in idf_yml
        assert "path: agent/components/fleetforge" in idf_yml
        assert "version: v0.8.4" in idf_yml
        sdk = (dest / "sdkconfig.defaults").read_text(encoding="utf-8")
        assert "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y" in sdk
        assert "CONFIG_MBEDTLS_DYNAMIC_BUFFER=y" in sdk

        # Refuses non-empty directory without --force
        rc_exists = main(["new", str(dest)])
        assert rc_exists == 1

    def test_cli_status_logs_and_secrets_subcommands(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fake_request(
            method: str,
            url: str,
            *,
            token: str,
            body: bytes | None = None,
            content_type: str = "application/octet-stream",
        ) -> tuple[int, dict[str, Any]]:
            if method == "GET" and "/v1/devices?" in url:
                return 200, {
                    "devices": [
                        {
                            "device_id": DEVICE_ID,
                            "name": "kitchen-eink",
                            "platform_type": "esp32s3",
                            "fw_version": "2.0.1",
                            "online": True,
                            "power_class": "always_on",
                            "secrets_rev": 2,
                            "health": {"free_heap": 65572, "rssi": -59},
                            "deploy": {"state": "confirmed", "artifact_version": "2.0.1"},
                        }
                    ],
                    "arrivals": [],
                }
            if method == "GET" and f"/v1/devices/{DEVICE_ID}/logs" in url:
                return 200, {
                    "device_id": DEVICE_ID,
                    "lines": [
                        {
                            "id": 1,
                            "logged_at": "2026-11-06T12:00:00Z",
                            "level": "I",
                            "text": "list 6 items in 1808 ms",
                            "context": {
                                "firmware.version": "2.0.1",
                                "device.free_heap_bytes": 65572,
                                "device.rssi_dbm": -59,
                            },
                        }
                    ],
                }
            if "/secrets" in url:
                return 200, {"secrets_rev": 3, "secrets": [{"key": "api_url"}]}
            return 200, {}

        with patch("fleetforge.cli._request_json", side_effect=fake_request):
            assert (
                main(
                    [
                        "--token",
                        "ffa_test.token",
                        "status",
                        "--project",
                        "eink-todo",
                    ]
                )
                == 0
            )
            status_out = capsys.readouterr().out
            assert "kitchen-eink" in status_out
            assert "64 KB" in status_out
            assert "confirmed (2.0.1)" in status_out

            assert (
                main(
                    [
                        "--token",
                        "ffa_test.token",
                        "logs",
                        "--project",
                        "eink-todo",
                    ]
                )
                == 0
            )
            logs_out = capsys.readouterr().out
            assert "list 6 items in 1808 ms" in logs_out
            assert "fw=2.0.1 heap=65572 rssi=-59" in logs_out

            assert (
                main(
                    [
                        "--token",
                        "ffa_test.token",
                        "secrets",
                        "set",
                        "api_url",
                        "https://boris.tvaroska.sk/todo/v1/list",
                        "--project",
                        "eink-todo",
                    ]
                )
                == 0
            )
            sec_out = json.loads(capsys.readouterr().out)
            assert sec_out["secrets_rev"] == 3


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
        # No heartbeat yet: the announce-known fw_version rides along, nothing empty.
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
        """OTEL-be-2: board identity once per batch (resource), latest heartbeat per record.

        A firmware line never has to repeat fw/library/layout/reset reason; the server has
        them from the announce. Snapshot values are typed, and absent fields are absent
        (no empty strings, no nulls) so the collector sees a stable schema.
        """
        import datetime as dt
        import uuid

        from fleetforge.ingestor.otel import device_snapshot

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
        # Heartbeat values are per record, not per resource: they change between batches.
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

        # The same snapshot is what `device_logs.context` stores: plain JSON scalars.
        snap = device_snapshot(dev)
        assert snap["device.rssi_dbm"] == -61
        assert snap["firmware.version"] == "2.0.1"
        assert json.dumps(snap)  # JSONB-serialisable
        assert device_snapshot(None) == {}
        bare = Device(device_id="x", platform_type="esp32", capabilities=[])
        assert device_snapshot(bare) == {}
        bare_keys = {a["key"] for a in _resource_attrs_of(bare)}
        assert "firmware.version" not in bare_keys
        assert "device.name" not in bare_keys


def _resource_attrs_of(device: Device) -> list[dict[str, Any]]:
    from fleetforge.ingestor.otel import _resource_attributes

    return _resource_attributes(device, "p")


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
        assert "nvs_erase_all" not in ff_sec_c
        assert "nvs_erase_key" in ff_sec_c
        assert "SECRETS_MAX_RETRIES" in ff_sec_c
        assert "s_failed_rev >= s_target_rev" in ff_sec_c

        # Pillar 3: OTel-compatible up/log + RTC_NOINIT_ATTR crash ring + sleep drain gate
        assert "RTC_NOINIT_ATTR" in ff_log_c
        assert "[prev-boot] %s" in ff_log_c
        assert "batch[FF_LOG_CAPACITY]" not in ff_log_c
        assert "ff_log_drained()" in ff_sleep_c
        assert "-Wframe-larger-than=1024" in (root / "CMakeLists.txt").read_text(encoding="utf-8")

        # Pillar 4: NVS ff_net overlay + Improv Wi-Fi BLE GATT service + non-blocking retry timer
        ff_identity_c = (root / "src" / "ff_identity.c").read_text(encoding="utf-8")
        ff_net_c = (root / "src" / "ff_net.c").read_text(encoding="utf-8")
        assert "00467768-6228-2272-4663-277478268000" in ff_wifi_c
        assert "00467768-6228-2272-4663-277478268003" in ff_wifi_c
        assert "improv_handle_rpc_command" in ff_wifi_c
        assert "expected_csum" in ff_wifi_c
        assert "ble_gatts_chr_updated" in ff_wifi_c
        assert "improv_ble_notify_status" in ff_wifi_c
        assert "IMPROV_STATE_PROVISIONED" in ff_wifi_c
        assert "load_nvs_net_overlay" in ff_wifi_c
        assert "IMPROV_BLE_WINDOW_S" in ff_wifi_c
        assert "improv_ble_window_start" in ff_wifi_c
        assert "s_retry_timer" in ff_wifi_c
        assert "vTaskDelay(" not in ff_wifi_c
        assert "ff_net_known_networks(cfg->net_count)" in ff_identity_c
        assert "ff_net_wifi_known_count()" in ff_net_c

        # Starter templates demonstrate both logf/ff_logf and getSecret/ff_get_secret
        assert "Fleetforge.logf" in basic_ino
        assert "Fleetforge.getSecret" in basic_ino
        assert "ff_logf" in basic_idf
        assert "ff_get_secret" in basic_idf


class TestOtelQueueAndRateLimiterAndStagedRollout:
    async def test_otel_client_pooling_and_bounded_queue_drain(self) -> None:
        from fleetforge.ingestor.otel import (
            OtelExportBatch,
            _enqueue_batch,
            close_otel_client,
            flush_otel_queue,
            get_otel_client,
            get_otel_queue,
        )

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

        with patch("fleetforge.ingestor.otel.send_otel_batch", return_value=True) as mock_send:
            flushed = await flush_otel_queue()
            assert flushed == 1
            assert queue.qsize() == 0
            mock_send.assert_called_once_with(batch)

        await close_otel_client()

    def test_fixed_window_limiter_protects_global_bucket_from_key_eviction(self) -> None:
        from fleetforge.auth.ratelimit import FixedWindowLimiter

        limiter = FixedWindowLimiter(per_key=5, per_global=3, window_s=60.0, max_keys=4)
        for i in range(3):
            limiter.record_failure(f"10.0.0.{i}")
        assert not limiter.allow("10.0.0.99")
        # Churning spoofed IPs past max_keys must never evict the global failure bucket
        for i in range(10, 30):
            _ = limiter.allow(f"192.168.1.{i}")
        assert not limiter.allow("10.0.0.99")

    async def test_staged_cohort_rollout_canary_wave_and_auto_halt(self) -> None:
        import datetime as dt
        import uuid
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from fleetforge.api.deps import AuthContext
        from fleetforge.api.routers.deploys import deploy_version_to_project_devices
        from fleetforge.config import Settings
        from fleetforge.deploys import DeploySnapshot
        from fleetforge.firmware.manifest import BUILTIN_LAYOUTS
        from fleetforge.partition_profiles import LayoutCatalog

        proj_id = uuid.uuid4()
        token_id = uuid.uuid4()
        now = dt.datetime.now(dt.UTC)

        def make_dev(idx: int, *, is_dev: bool = False, fw: str = "1.0.0") -> Device:
            return Device(
                device_id=f"a4cf12b3de0{idx}",
                project_id=proj_id,
                platform_type="esp32c6",
                fw_version=fw,
                partition_layout="ab-4m-v1",
                ota_slot_size=1966080,
                rollback_capable=True,
                capabilities=["ota"],
                link_type="wifi",
                power_class="always_on",
                is_dev=is_dev,
                presence_reported=True,
                last_seen=now,
            )

        devices = [make_dev(i, is_dev=(i == 2)) for i in range(1, 8)]
        dev_by_id = {d.device_id: d for d in devices}
        session = AsyncMock()
        scalars_res = SimpleNamespace(all=lambda: devices)
        session.scalars = AsyncMock(return_value=scalars_res)
        session.get = AsyncMock(
            side_effect=lambda model, key: (
                dev_by_id.get(str(key)) if model is Device else SimpleNamespace(name="ci-token")
            )
        )
        art_row = SimpleNamespace(
            sha256="c" * 64,
            size_bytes=2048,
            partition_layout="ab-4m-v1",
            has_lib_marker=True,
            sig=None,
        )
        session.execute = AsyncMock(return_value=SimpleNamespace(first=lambda: art_row))

        admin = AuthContext(
            token_id=token_id,
            subject="ci",
            expires_at=now + dt.timedelta(hours=1),
            scopes=("deploy",),
            is_superadmin=True,
        )
        settings = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            artifact_url_secret="s" * 32,
            public_base_url="https://fleet.example.com",
        )
        publisher = AsyncMock()
        catalog = LayoutCatalog(
            layouts=BUILTIN_LAYOUTS,
            by_fingerprint={},
            pending=frozenset(),
        )

        with (
            patch(
                "fleetforge.api.routers.deploys.load_layout_catalog",
                return_value=catalog,
            ),
            patch(
                "fleetforge.api.routers.deploys.latest_open_transaction",
                return_value=None,
            ),
            patch("fleetforge.api.routers.deploys.record_requested", AsyncMock()),
        ):
            # 1. Wave 1 (Canary Gate): No board has confirmed 1.2.0 yet -> only dev board stages
            with patch(
                "fleetforge.api.routers.deploys.latest_deploys",
                return_value={},
            ):
                deployed, skipped = await deploy_version_to_project_devices(
                    session,
                    project_id=proj_id,
                    version="1.2.0",
                    target_filter="all",
                    apply="auto",
                    override=(),
                    admin=admin,
                    settings=settings,
                    publisher=publisher,
                    canary=True,
                    max_parallel=3,
                )
                assert [d.device_id for d in deployed] == ["a4cf12b3de02"]
                assert len(skipped) == 6
                assert all("waiting for canary a4cf12b3de02" in msg for msg in skipped.values())

            # 2. Wave 2 (Bounded Concurrency): Canary confirmed -> stages up to max_parallel=3
            devices[1].fw_version = "1.2.0"
            with patch(
                "fleetforge.api.routers.deploys.latest_deploys",
                return_value={
                    "a4cf12b3de02": DeploySnapshot(
                        cmd_id="cmd-1",
                        state="confirmed",
                        at=now,
                        is_terminal=True,
                        artifact_version="1.2.0",
                        from_version="1.0.0",
                        pct=None,
                        detail=None,
                        steps=(),
                        sent_by=None,
                        selftest=None,
                    )
                },
            ):
                deployed2, skipped2 = await deploy_version_to_project_devices(
                    session,
                    project_id=proj_id,
                    version="1.2.0",
                    target_filter="all",
                    apply="auto",
                    override=(),
                    admin=admin,
                    settings=settings,
                    publisher=publisher,
                    canary=True,
                    max_parallel=3,
                )
                assert len(deployed2) == 3
                assert sum("queued for next wave" in msg for msg in skipped2.values()) == 3

            # 3. Auto-Halt on Rollback: A board rolled back on 1.2.0 -> halts immediately
            with patch(
                "fleetforge.api.routers.deploys.latest_deploys",
                return_value={
                    "a4cf12b3de02": DeploySnapshot(
                        cmd_id="cmd-1",
                        state="rolled_back",
                        at=now,
                        is_terminal=True,
                        artifact_version="1.2.0",
                        from_version="1.0.0",
                        pct=None,
                        detail="confirm timer expired",
                        steps=(),
                        sent_by=None,
                        selftest=None,
                    )
                },
            ):
                deployed3, skipped3 = await deploy_version_to_project_devices(
                    session,
                    project_id=proj_id,
                    version="1.2.0",
                    target_filter="all",
                    apply="auto",
                    override=(),
                    admin=admin,
                    settings=settings,
                    publisher=publisher,
                    canary=True,
                    max_parallel=3,
                )
                assert deployed3 == []
                assert all("rollout halted" in msg for msg in skipped3.values())

    async def test_advance_project_rollout_on_confirm_progresses_waves_without_republishing_in_flight(
        self,
    ) -> None:
        import datetime as dt
        import uuid
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from fleetforge.api.routers.deploys import advance_project_rollout_on_confirm
        from fleetforge.config import Settings
        from fleetforge.deploys import DeploySender, DeploySnapshot, RequestedRollout
        from fleetforge.firmware.manifest import BUILTIN_LAYOUTS
        from fleetforge.partition_profiles import LayoutCatalog

        proj_id = uuid.uuid4()
        now = dt.datetime.now(dt.UTC)

        def make_dev(idx: int, *, is_dev: bool = False, fw: str = "1.0.0") -> Device:
            return Device(
                device_id=f"a4cf12b3de0{idx}",
                project_id=proj_id,
                platform_type="esp32c6",
                fw_version=fw,
                partition_layout="ab-4m-v1",
                ota_slot_size=1966080,
                rollback_capable=True,
                capabilities=["ota"],
                link_type="wifi",
                power_class="always_on",
                is_dev=is_dev,
                presence_reported=True,
                last_seen=now,
            )

        devices = [make_dev(i, is_dev=(i == 1)) for i in range(1, 6)]
        dev_by_id = {d.device_id: d for d in devices}
        session = AsyncMock()
        scalars_res = SimpleNamespace(all=lambda: devices)
        session.scalars = AsyncMock(return_value=scalars_res)
        session.get = AsyncMock(side_effect=lambda model, key: dev_by_id.get(str(key)))
        art_row = SimpleNamespace(
            sha256="c" * 64,
            size_bytes=2048,
            partition_layout="ab-4m-v1",
            has_lib_marker=True,
            sig=None,
        )
        session.execute = AsyncMock(return_value=SimpleNamespace(first=lambda: art_row))

        settings = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            artifact_url_secret="s" * 32,
            public_base_url="https://fleet.example.com",
        )
        publisher = AsyncMock()
        catalog = LayoutCatalog(layouts=BUILTIN_LAYOUTS, by_fingerprint={}, pending=frozenset())
        rollout_req = RequestedRollout(
            artifact_version="1.2.0",
            apply="auto",
            sent_by=DeploySender(subject="ci", token_id="tok-1", credential="ci-token"),
            rollout={
                "target_filter": "all",
                "canary": True,
                "max_parallel": 2,
                "override": [],
                "target_chip": "esp32c6",
            },
        )

        with (
            patch(
                "fleetforge.api.routers.deploys.requested_rollout",
                return_value=rollout_req,
            ),
            patch(
                "fleetforge.api.routers.deploys.load_layout_catalog",
                return_value=catalog,
            ),
            patch(
                "fleetforge.api.routers.deploys.latest_open_transaction",
                return_value=None,
            ),
            patch("fleetforge.api.routers.deploys.record_requested", AsyncMock()),
        ):
            # Canary (device 1) confirmed, device 2 is already in-flight -> only 1 new board (device 3) stages
            with patch(
                "fleetforge.api.routers.deploys.latest_deploys",
                return_value={
                    "a4cf12b3de01": DeploySnapshot(
                        cmd_id="cmd-1",
                        state="confirmed",
                        at=now,
                        is_terminal=True,
                        artifact_version="1.2.0",
                        from_version="1.0.0",
                        pct=None,
                        detail=None,
                        steps=(),
                        sent_by=None,
                        selftest=None,
                    ),
                    "a4cf12b3de02": DeploySnapshot(
                        cmd_id="cmd-2",
                        state="downloading",
                        at=now,
                        is_terminal=False,
                        artifact_version="1.2.0",
                        from_version="1.0.0",
                        pct=50,
                        detail=None,
                        steps=(),
                        sent_by=None,
                        selftest=None,
                    ),
                },
            ):
                advanced = await advance_project_rollout_on_confirm(
                    session,
                    device=devices[0],
                    cmd_id="cmd-1",
                    settings=settings,
                    publisher=publisher,
                )
                assert [d.device_id for d in advanced] == ["a4cf12b3de03"]

    async def test_ci_webhook_header_encrypted_at_rest_and_decrypted_on_dispatch(self) -> None:
        from unittest.mock import AsyncMock

        from fleetforge.api.routers.projects import _trigger_ci_webhook
        from fleetforge.config import Settings
        from fleetforge.secrets_crypto import decrypt_secret, encrypt_secret

        settings = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            artifact_url_secret="k" * 32,
        )
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
        with patch("fleetforge.api.routers.projects.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value.__aenter__.return_value = mock_client

            ok = await _trigger_ci_webhook(proj, settings=settings)
            assert ok is True
            mock_client.post.assert_called_once()
            sent_headers = mock_client.post.call_args.kwargs["headers"]
            assert sent_headers["Authorization"] == raw_header

    async def test_otel_project_cache_avoids_repeated_db_queries_and_invalidates_on_update(
        self,
    ) -> None:
        import uuid
        from unittest.mock import AsyncMock

        from fleetforge.config import Settings
        from fleetforge.ingestor.otel import (
            _project_otel_cache,
            _resolve_project_otel,
            invalidate_project_otel_cache,
        )
        from fleetforge.secrets_crypto import encrypt_secret

        proj_id = uuid.uuid4()
        invalidate_project_otel_cache()
        settings = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            artifact_url_secret="k" * 32,
        )
        enc_headers = encrypt_secret('{"Authorization": "Bearer secret-tok"}', settings)

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
        )
        session.get = AsyncMock(return_value=proj)
        dev = Device(
            device_id="a4cf12b3de01",
            project_id=proj_id,
            platform_type="esp32c6",
            link_type="wifi",
            power_class="always_on",
        )

        with patch("fleetforge.ingestor.otel._safe_settings", return_value=settings):
            res1 = await _resolve_project_otel(session, dev)
            res2 = await _resolve_project_otel(session, dev)
            assert res1 == (
                "eink",
                "https://otel.example.com",
                {"Authorization": "Bearer secret-tok"},
            )
            assert res2 == res1
            assert session.get.call_count == 1
            assert any(k[0] == proj_id for k in _project_otel_cache)

            invalidate_project_otel_cache(proj_id)
            assert not any(k[0] == proj_id for k in _project_otel_cache)
            res3 = await _resolve_project_otel(session, dev)
            assert res3 == res1
            assert session.get.call_count == 2

    def test_non_app_artifact_schema_serialization_omits_app_and_includes_cargo(self) -> None:
        import datetime as dt

        from fleetforge.api.schemas import ArtifactSummary, ArtifactUploaded

        now = dt.datetime.now(dt.UTC)
        app_up = ArtifactUploaded(
            sha256="a" * 64,
            size_bytes=1024,
            target="esp32s3",
            version="1.0.0",
            partition_layout="ab-4m-v1",
            created=True,
            has_lib_marker=True,
        )
        assert "artifact_type" not in app_up.model_dump()

        cargo_up = ArtifactUploaded(
            sha256="b" * 64,
            size_bytes=3_000_000,
            target="rpi5",
            version="2.0.0",
            partition_layout=None,
            created=True,
            has_lib_marker=None,
            artifact_type="model_weights",
        )
        dumped = cargo_up.model_dump()
        assert dumped["artifact_type"] == "model_weights"
        assert dumped["partition_layout"] is None
        assert dumped["has_lib_marker"] is None

        app_sum = ArtifactSummary(
            target="esp32s3",
            version="1.0.0",
            sha256="a" * 64,
            size_bytes=1024,
            partition_layout="ab-4m-v1",
            kind="user_firmware",
            created_at=now,
        )
        assert "artifact_type" not in app_sum.model_dump()

        cargo_sum = ArtifactSummary(
            target="rpi5",
            version="2.0.0",
            sha256="b" * 64,
            size_bytes=3_000_000,
            partition_layout=None,
            kind="user_firmware",
            created_at=now,
            artifact_type="model_weights",
        )
        assert cargo_sum.model_dump()["artifact_type"] == "model_weights"

    async def test_wake_aware_url_ttl_and_on_wake_stage_republish(self) -> None:
        import datetime as dt
        import uuid
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from fleetforge.api.routers.deploys import (
            effective_url_ttl_s,
            republish_open_requested_on_wake,
        )
        from fleetforge.config import Settings
        from fleetforge.deploys import DeploySnapshot, DeployTransaction

        settings = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            artifact_url_secret="s" * 32,
            public_base_url="https://fleet.example.com",
            signed_url_ttl_s=1800,
        )
        always_on = Device(
            device_id="a4cf12b3de01",
            platform_type="esp32c6",
            link_type="wifi",
            power_class="always_on",
        )
        sleepy = Device(
            device_id="a4cf12b3de02",
            project_id=uuid.uuid4(),
            platform_type="esp32c6",
            link_type="wifi",
            power_class="sleepy",
            expected_wake_interval_s=3600,
        )
        assert effective_url_ttl_s(always_on, settings) == 1800
        assert effective_url_ttl_s(sleepy, settings) == 2 * 3600 + 1800

        session = AsyncMock()
        session.get = AsyncMock(return_value=sleepy)
        art_row = SimpleNamespace(
            sha256="d" * 64,
            size_bytes=4096,
            partition_layout="ab-4m-v1",
            has_lib_marker=True,
            sig=None,
            artifact_type="app",
        )
        session.execute = AsyncMock(return_value=SimpleNamespace(first=lambda: art_row))
        publisher = AsyncMock()
        now = dt.datetime.now(dt.UTC)

        with (
            patch(
                "fleetforge.api.routers.deploys.latest_deploys",
                return_value={
                    sleepy.device_id: DeploySnapshot(
                        cmd_id="cmd-wake-1",
                        state="requested",
                        at=now - dt.timedelta(hours=1),
                        is_terminal=False,
                        artifact_version="1.3.0",
                        from_version="1.0.0",
                        pct=None,
                        detail=None,
                        steps=(),
                        sent_by=None,
                        selftest=None,
                    )
                },
            ),
            patch(
                "fleetforge.api.routers.deploys.deploy_transaction",
                return_value=DeployTransaction(
                    cmd_id="cmd-wake-1",
                    apply="on_command",
                    newest_state="requested",
                    ended_state=None,
                ),
            ),
        ):
            republished = await republish_open_requested_on_wake(
                session,
                device_id=sleepy.device_id,
                settings=settings,
                publisher=publisher,
            )
            assert republished is True
            publisher.publish.assert_awaited_once()
            pub_dev_id, pub_payload = publisher.publish.call_args.args
            assert pub_dev_id == sleepy.device_id
            assert pub_payload["id"] == "cmd-wake-1"
            assert pub_payload["apply"] == "on_command"
            assert pub_payload["artifact"]["version"] == "1.3.0"

    async def test_stalled_wave_recovery_and_cohort_cancel(self) -> None:
        import datetime as dt
        import uuid
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from fleetforge.api.deps import AuthContext
        from fleetforge.api.routers.deploys import advance_project_rollout_on_confirm
        from fleetforge.api.routers.projects import cancel_project_deploy
        from fleetforge.api.schemas import ProjectDeployCancelRequest
        from fleetforge.config import Settings
        from fleetforge.deploys import DeploySender, DeploySnapshot, RequestedRollout
        from fleetforge.firmware.manifest import BUILTIN_LAYOUTS
        from fleetforge.partition_profiles import LayoutCatalog

        proj_id = uuid.uuid4()
        token_id = uuid.uuid4()
        now = dt.datetime.now(dt.UTC)

        # Device 1 confirmed canary; Device 2 went offline 10 minutes ago while downloading
        # (past confirm_timeout_s=120s); Device 3 is online and waiting.
        dev1 = Device(
            device_id="a4cf12b3de01",
            project_id=proj_id,
            platform_type="esp32c6",
            fw_version="1.2.0",
            partition_layout="ab-4m-v1",
            ota_slot_size=1966080,
            rollback_capable=True,
            capabilities=["ota", "cancel"],
            link_type="wifi",
            power_class="always_on",
            is_dev=True,
            presence_reported=True,
            last_seen=now,
        )
        dev2_stalled = Device(
            device_id="a4cf12b3de02",
            project_id=proj_id,
            platform_type="esp32c6",
            fw_version="1.0.0",
            partition_layout="ab-4m-v1",
            ota_slot_size=1966080,
            rollback_capable=True,
            capabilities=["ota", "cancel"],
            link_type="wifi",
            power_class="always_on",
            is_dev=False,
            presence_reported=False,
            last_seen=now - dt.timedelta(minutes=10),
        )
        dev3_online = Device(
            device_id="a4cf12b3de03",
            project_id=proj_id,
            platform_type="esp32c6",
            fw_version="1.0.0",
            partition_layout="ab-4m-v1",
            ota_slot_size=1966080,
            rollback_capable=True,
            capabilities=["ota", "cancel"],
            link_type="wifi",
            power_class="always_on",
            is_dev=False,
            presence_reported=True,
            last_seen=now,
        )
        devices = [dev1, dev2_stalled, dev3_online]
        dev_by_id = {d.device_id: d for d in devices}

        session = AsyncMock()
        session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: devices))
        session.get = AsyncMock(
            side_effect=lambda model, key: (
                dev_by_id.get(str(key)) if model is Device else SimpleNamespace(name="ci-token")
            )
        )
        art_row = SimpleNamespace(
            sha256="c" * 64,
            size_bytes=2048,
            partition_layout="ab-4m-v1",
            has_lib_marker=True,
            sig=None,
            artifact_type="app",
        )
        session.execute = AsyncMock(return_value=SimpleNamespace(first=lambda: art_row))

        settings = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            artifact_url_secret="s" * 32,
            public_base_url="https://fleet.example.com",
            confirm_timeout_s=120,
        )
        publisher = AsyncMock()
        catalog = LayoutCatalog(layouts=BUILTIN_LAYOUTS, by_fingerprint={}, pending=frozenset())
        rollout_req = RequestedRollout(
            artifact_version="1.2.0",
            apply="auto",
            sent_by=DeploySender(subject="ci", token_id="tok-1", credential="ci-token"),
            rollout={
                "target_filter": "all",
                "canary": True,
                "max_parallel": 1,
                "override": [],
                "target_chip": "esp32c6",
            },
        )
        snapshots = {
            dev1.device_id: DeploySnapshot(
                cmd_id="cmd-1",
                state="confirmed",
                at=now,
                is_terminal=True,
                artifact_version="1.2.0",
                from_version="1.0.0",
                pct=None,
                detail=None,
                steps=(),
                sent_by=None,
                selftest=None,
            ),
            dev2_stalled.device_id: DeploySnapshot(
                cmd_id="cmd-2",
                state="downloading",
                at=now - dt.timedelta(minutes=10),
                is_terminal=False,
                artifact_version="1.2.0",
                from_version="1.0.0",
                pct=25,
                detail=None,
                steps=(),
                sent_by=None,
                selftest=None,
            ),
        }

        with (
            patch("fleetforge.api.routers.deploys.requested_rollout", return_value=rollout_req),
            patch("fleetforge.api.routers.deploys.load_layout_catalog", return_value=catalog),
            patch("fleetforge.api.routers.deploys.latest_open_transaction", return_value=None),
            patch("fleetforge.api.routers.deploys.record_requested", AsyncMock()),
            patch("fleetforge.api.routers.deploys.latest_deploys", return_value=snapshots),
        ):
            # Even with max_parallel=1, stalled offline dev2 does NOT starve dev3_online!
            advanced = await advance_project_rollout_on_confirm(
                session,
                device=dev1,
                cmd_id="cmd-1",
                settings=settings,
                publisher=publisher,
            )
            assert [d.device_id for d in advanced] == [dev3_online.device_id]

        # Test cohort cancel (`POST /v1/projects/{slug}/deploy/cancel`)
        proj = Project(id=proj_id, slug="eink-frame", name="E-Ink")
        admin = AuthContext(
            token_id=token_id,
            subject="ci",
            expires_at=now + dt.timedelta(hours=1),
            scopes=("deploy",),
            is_superadmin=True,
        )

        class _FakeSessionCM:
            async def __aenter__(self) -> Any:
                return session

            async def __aexit__(self, *args: object) -> None:
                return None

        with (
            patch(
                "fleetforge.api.routers.projects.resolve_project_for_request",
                return_value=proj,
            ),
            patch("fleetforge.api.routers.projects.latest_deploys", return_value=snapshots),
            patch("fleetforge.api.routers.projects.record_sent", AsyncMock()) as mock_rec_sent,
        ):
            cancel_res = await cancel_project_deploy(
                slug="eink-frame",
                body=ProjectDeployCancelRequest(version="1.2.0"),
                admin=admin,
                settings=settings,
                sessionmaker=lambda: _FakeSessionCM(),  # type: ignore[arg-type]
                publisher=publisher,
            )
            assert [c.device_id for c in cancel_res.cancelled] == [dev2_stalled.device_id]
            assert cancel_res.cancelled[0].cmd_id == "cmd-2"
            assert dev1.device_id in cancel_res.skipped
            assert dev3_online.device_id in cancel_res.skipped
            mock_rec_sent.assert_awaited_once()

    async def test_sub_versions_and_pluggable_cargo_handler_end_to_end(self) -> None:
        import hashlib

        from fleetforge.announce_fields import normalize_sub_versions
        from fleetforge.ingestor.protocol import AnnouncePayload
        from fleetforge.simulator.device import DeviceIdentity, LinkProfile, StageRunner

        assert normalize_sub_versions({"model_weights": "2.1.0"}) == {"model_weights": "2.1.0"}
        assert normalize_sub_versions({"model_weights": "2.1.0", " bad key ": "1.0"}) is None
        ann = AnnouncePayload.model_validate(
            {
                "device_id": DEVICE_ID,
                "sub_versions": {"model_weights": "2.1.0", "voice_pack": "1.0.4"},
            }
        )
        assert ann.sub_versions == {"model_weights": "2.1.0", "voice_pack": "1.0.4"}

        identity = DeviceIdentity(device_id=DEVICE_ID, fw_version="1.0.0")
        applied_cargo: list[tuple[str | None, bytes]] = []
        payload_bytes = b"quantized-weights-v2.1.0"
        digest = hashlib.sha256(payload_bytes).hexdigest()

        def on_weights(data: bytes, version: str | None) -> None:
            applied_cargo.append((version, data))

        client = FakeMqttClient()
        runner = StageRunner(
            identity=identity,
            link=LinkProfile(),
            cargo_handlers={"model_weights": on_weights},
        )
        with patch("fleetforge.simulator.device._fetch", return_value=payload_bytes):
            await runner._stage(
                client,  # type: ignore[arg-type]
                "cmd-cargo-1",
                {
                    "artifact": {
                        "url": "https://fleet.example.com/v1/artifact/bin",
                        "sha256": digest,
                        "size": len(payload_bytes),
                        "version": "2.1.0",
                        "type": "model_weights",
                    }
                },
                lambda _msg: None,
            )
        assert applied_cargo == [("2.1.0", payload_bytes)]
        assert runner.identity.fw_version == "1.0.0"
        assert runner.identity.sub_versions == {"model_weights": "2.1.0"}
        published_statuses = [
            json.loads(raw)["state"]
            for topic, raw, _, _ in client.published
            if topic.endswith("/up/status")
        ]
        assert published_statuses == [
            "staging",
            "downloading",
            "verifying",
            "staged",
            "applying",
            "confirming",
            "confirmed",
        ]
        published_announces = [
            json.loads(raw)
            for topic, raw, _, _ in client.published
            if topic.endswith("/up/announce")
        ]
        assert published_announces[-1]["sub_versions"] == {"model_weights": "2.1.0"}

        # Verify C firmware agent cargo handler & sub_versions registration symbols
        root = Path(__file__).resolve().parents[1] / "agent" / "components" / "fleetforge"
        ff_ota_h = (root / "src" / "ff_ota.h").read_text(encoding="utf-8")
        ff_ota_c = (root / "src" / "ff_ota.c").read_text(encoding="utf-8")
        ff_id_c = (root / "src" / "ff_identity.c").read_text(encoding="utf-8")
        ff_mqtt_c = (root / "src" / "ff_mqtt.c").read_text(encoding="utf-8")
        assert "ff_ota_register_cargo_handler" in ff_ota_h
        assert "run_cargo_ota" in ff_ota_c
        assert "ff_identity_set_sub_version" in ff_id_c
        assert "ff_mqtt_complete_cargo" in ff_mqtt_c


class TestCuj3PitTetheredDockAndVenueOffline:
    def test_cuj3_firmware_venue_offline_nimble_coexistence_and_dock_drain(self) -> None:
        root = Path(__file__).resolve().parents[1] / "agent" / "components" / "fleetforge"
        ff_sec_h = (root / "include" / "ff_sec.h").read_text(encoding="utf-8")
        ff_net_c = (root / "src" / "ff_net.c").read_text(encoding="utf-8")
        ff_wifi_c = (root / "src" / "ff_net_wifi.c").read_text(encoding="utf-8")
        ff_mqtt_c = (root / "src" / "ff_mqtt.c").read_text(encoding="utf-8")
        ff_log_c = (root / "src" / "ff_log.c").read_text(encoding="utf-8")
        ff_telem_c = (root / "src" / "ff_telemetry.c").read_text(encoding="utf-8")
        ff_ota_h = (root / "src" / "ff_ota.h").read_text(encoding="utf-8")
        ff_ota_c = (root / "src" / "ff_ota.c").read_text(encoding="utf-8")
        arduino_h = (root / "src" / "Fleetforge.h").read_text(encoding="utf-8")
        arduino_cpp = (root / "src" / "Fleetforge.cpp").read_text(encoding="utf-8")
        basic_idf = (root / "examples" / "basic_idf" / "main" / "main.c").read_text(
            encoding="utf-8"
        )

        # Track 1 (CUJ-3 Step 4): Venue Offline Mode, NimBLE coexistence, offline PENDING_VERIFY
        assert "esp_err_t ff_set_offline_mode(bool offline);" in ff_sec_h
        assert "ff_set_offline_mode(" in ff_net_c
        assert "ff_net_set_dock_active(" in ff_net_c
        assert 'return "dock";' in ff_net_c
        assert "s_venue_offline" in ff_wifi_c
        assert "s_improv_ble_enabled" in ff_wifi_c
        assert "ble_hs_synced()" in ff_wifi_c
        assert "ff_net_wifi_set_offline_mode(" in ff_wifi_c
        assert "ff_net_wifi_set_improv_ble_enabled(" in ff_wifi_c
        assert "ff_mqtt_confirm_offline(" in ff_mqtt_c
        assert "if (!s_selftest_registered)" in ff_mqtt_c
        assert "setOfflineMode(bool offline)" in arduino_h
        assert "FleetforgeClass::setOfflineMode(bool offline)" in arduino_cpp
        assert "ff_set_offline_mode(false);" in basic_idf

        # Track 2 (CUJ-3 Steps 5-6): Offline ring-drain JSON + USB-C dock stage
        assert "ff_log_drain_json(" in ff_log_c
        assert "ff_telemetry_drain_json(" in ff_telem_c
        assert "esp_err_t ff_ota_dock_stage(" in ff_ota_h
        assert "ff_ota_dock_stage(const ff_ota_cmd_t *cmd" in ff_ota_c

        # Verify ingestor/main.py does not import from fleetforge.api (ARCH-1 / ARCH-5)
        ingestor_main = (
            Path(__file__).resolve().parents[1] / "src" / "fleetforge" / "ingestor" / "main.py"
        ).read_text(encoding="utf-8")
        assert "fleetforge.api" not in ingestor_main

    async def test_cuj3_end_to_end_venue_offline_dock_stage_selftest_and_sync(
        self, tmp_path: Path
    ) -> None:
        import datetime as dt
        import hashlib
        import uuid
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from fleetforge.api.deps import AuthContext
        from fleetforge.api.routers.devices import dock_sync_device
        from fleetforge.api.schemas import DockSyncRequest
        from fleetforge.config import Settings
        from fleetforge.deploys import DeploySnapshot
        from fleetforge.signing import sign_digest
        from fleetforge.simulator.device import (
            CAPABILITY_SELFTEST,
            SAFE_WINDOW_AUTO,
            SAFE_WINDOW_HOLD,
            DeviceIdentity,
            LinkProfile,
            StageRunner,
        )

        # 1. Chloe's FLUX-10413-Hub node enters Venue Offline Mode at the competition venue
        fake_now = [1000.0]
        identity = DeviceIdentity(
            device_id=DEVICE_ID,
            platform_type="esp32s3",
            fw_version="2026.2.1-qual11",
            capabilities=("ota", CAPABILITY_SELFTEST, "data_partition"),
        )
        applied_luts: list[tuple[str | None, bytes]] = []
        runner = StageRunner(
            identity=identity,
            link=LinkProfile(),
            safe_window=SAFE_WINDOW_HOLD,
            selftest_fail_marker="badcan",
            require_signature=True,
            cargo_handlers={"data_partition": lambda data, ver: applied_luts.append((ver, data))},
            clock=lambda: fake_now[0],
        )
        runner.set_offline_mode(True)
        assert runner.offline_mode is True

        # 2. During Qual 11, node records telemetry and structured logs offline with relative age_ms
        assert runner.record_telemetry("lora_rssi", -78.0) is True
        fake_now[0] += 5.0
        assert runner.record_telemetry("can_bus_err_cnt", 0.0) is True
        runner.record_log("I", "qual 11: scouting packet synced over NimBLE")
        fake_now[0] += 5.0
        runner.record_log("E", "watchdog reset before pit dock", prev_boot=True)

        # 3. Pit dock stage while safe_window="hold" (active BLE sync) parks in awaiting_safe_window
        lut_bytes = b"BAT01,12.8V\nBAT02,12.6V\n"
        lut_sha = hashlib.sha256(lut_bytes).hexdigest()
        lut_sig = sign_digest(lut_sha)
        cargo_steps = runner.dock_stage(
            "cmd-dock-lut-1",
            lut_bytes,
            sha256=lut_sha,
            version="v14",
            sig=lut_sig,
            artifact_type="data_partition",
        )
        assert [s["state"] for s in cargo_steps] == [
            "staging",
            "downloading",
            "verifying",
            "staged",
            "applying",
            "confirming",
            "confirmed",
        ]
        assert applied_luts == [("v14", lut_bytes)]
        assert runner.identity.sub_versions == {"data_partition": "v14"}
        assert runner.identity.link_type == "dock"

        # Unsigned or tampered app binary over USB-C dock is rejected before flashing
        good_bin = b"\xe9\x00" + b"FFOTALIB:2026.2.2-qual12" * 16
        good_sha = hashlib.sha256(good_bin).hexdigest()
        good_sig = sign_digest(good_sha)

        unsigned_steps = runner.dock_stage(
            "cmd-dock-unsigned",
            good_bin,
            sha256=good_sha,
            version="2026.2.2-qual12",
            sig=None,
        )
        assert unsigned_steps[-1]["state"] == "failed"
        assert unsigned_steps[-1]["detail"] == "signature missing"
        assert runner.identity.fw_version == "2026.2.1-qual11"

        # While safe_window="hold", a valid signed app stage parks in awaiting_safe_window
        held_steps = runner.dock_stage(
            "cmd-dock-app-hold",
            good_bin,
            sha256=good_sha,
            version="2026.2.2-qual12",
            sig=good_sig,
        )
        assert [s["state"] for s in held_steps] == [
            "staging",
            "downloading",
            "verifying",
            "staged",
            "awaiting_safe_window",
        ]
        runner._clear_staged()

        # 4. Safe window opens in the pit -> signed app stages, reboots, and confirms via local ff_selftest
        runner.safe_window = SAFE_WINDOW_AUTO
        app_steps = runner.dock_stage(
            "cmd-dock-app-1",
            good_bin,
            sha256=good_sha,
            version="2026.2.2-qual12",
            sig=good_sig,
        )
        assert [s["state"] for s in app_steps] == [
            "staging",
            "downloading",
            "verifying",
            "staged",
            "applying",
            "rebooting",
        ]
        boot_steps = runner.dock_confirm_boot()
        assert [s["state"] for s in boot_steps] == ["confirming", "confirmed"]
        assert boot_steps[-1]["detail"] == "selftest passed"
        assert runner.identity.fw_version == "2026.2.2-qual12"

        # 5. A broken build whose CAN/LoRa self-test fails rolls back offline to 2026.2.2-qual12
        bad_bin = b"\xe9\x00" + b"FFOTALIB:2026.2.3-badcan" * 16
        bad_sha = hashlib.sha256(bad_bin).hexdigest()
        bad_sig = sign_digest(bad_sha)
        runner.dock_stage(
            "cmd-dock-app-bad",
            bad_bin,
            sha256=bad_sha,
            version="2026.2.3-badcan",
            sig=bad_sig,
        )
        rb_steps = runner.dock_confirm_boot()
        assert [s["state"] for s in rb_steps] == ["confirming", "rolling_back", "rolled_back"]
        assert (
            rb_steps[-1]["detail"]
            == "selftest failed: simulated self-test failure; returned to ota_1"
        )
        assert runner.identity.fw_version == "2026.2.2-qual12"

        # 6. Drain the USB-C dock bundle and sync to `POST /v1/devices/{device_id}/dock-sync`
        bundle = runner.dock_drain()
        assert bundle["announce"]["link_type"] == "dock"  # type: ignore[index]
        assert bundle["announce"]["fw_version"] == "2026.2.2-qual12"  # type: ignore[index]
        assert bundle["announce"]["sub_versions"] == {"data_partition": "v14"}  # type: ignore[index]
        assert len(bundle["telemetry"]) == 1  # type: ignore[arg-type]
        assert len(bundle["logs"]) == 1  # type: ignore[arg-type]
        # Second drain is empty because rings were committed
        empty_bundle = runner.dock_drain()
        assert empty_bundle["statuses"] == []
        assert empty_bundle["telemetry"] == []
        assert empty_bundle["logs"] == []

        proj_id = uuid.uuid4()
        now = dt.datetime.now(dt.UTC)
        dev_row = Device(
            device_id=DEVICE_ID,
            project_id=proj_id,
            platform_type="esp32s3",
            fw_version="2026.2.1-qual11",
            partition_layout="ab-4m-v1",
            ota_slot_size=1966080,
            rollback_capable=True,
            capabilities=["ota", "selftest", "data_partition"],
            link_type="wifi",
            power_class="always_on",
            presence_reported=False,
            last_seen=now - dt.timedelta(hours=2),
            enrolled_at=now - dt.timedelta(days=1),
            is_dev=False,
            stay_awake=False,
        )
        session = AsyncMock()
        session.get = AsyncMock(return_value=dev_row)

        class _FakeSessionCM:
            async def __aenter__(self) -> Any:
                return session

            async def __aexit__(self, *args: object) -> None:
                return None

        admin = AuthContext(
            token_id=uuid.uuid4(),
            subject="chloe",
            expires_at=now + dt.timedelta(hours=1),
            scopes=("deploy",),
            is_superadmin=True,
        )
        settings = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            artifact_url_secret="s" * 32,
        )

        async def fake_handle_up_message(
            _sess: Any,
            *,
            topic: str,
            payload: bytes,
            retained: bool,
            received_at: dt.datetime,
            tolerance: float,
            settings: Any = None,
            publisher: Any = None,
        ) -> Any:
            parsed = json.loads(payload.decode("utf-8"))
            if topic.endswith("/up/announce"):
                dev_row.link_type = parsed["link_type"]
                dev_row.fw_version = parsed["fw_version"]
                dev_row.sub_versions = parsed.get("sub_versions")
                dev_row.last_seen = received_at
            return SimpleNamespace(topic=topic)

        with (
            patch(
                "fleetforge.api.routers.devices.handle_up_message",
                side_effect=fake_handle_up_message,
            ),
            patch(
                "fleetforge.api.routers.devices.latest_deploys",
                return_value={
                    DEVICE_ID: DeploySnapshot(
                        cmd_id="cmd-dock-app-1",
                        state="confirmed",
                        at=now,
                        is_terminal=True,
                        artifact_version="2026.2.2-qual12",
                        from_version="2026.2.1-qual11",
                        pct=100,
                        detail="selftest passed",
                        steps=(),
                        sent_by=None,
                        selftest=None,
                    )
                },
            ),
            patch("fleetforge.api.routers.devices.latest_commands", return_value={}),
            patch("fleetforge.api.routers.devices.fault_counts", return_value={}),
            patch("fleetforge.api.routers.devices.latest_metrics", return_value={}),
        ):
            sync_resp = await dock_sync_device(
                device_id=DEVICE_ID,
                body=DockSyncRequest.model_validate(bundle),
                admin=admin,
                settings=settings,
                sessionmaker=lambda: _FakeSessionCM(),  # type: ignore[arg-type]
            )
            assert sync_resp.device_id == DEVICE_ID
            assert sync_resp.link_type == "dock"
            assert sync_resp.fw_version == "2026.2.2-qual12"
            assert sync_resp.sub_versions == {"data_partition": "v14"}
            assert sync_resp.statuses_recorded == len(bundle["statuses"])  # type: ignore[arg-type]
            assert sync_resp.telemetry_batches_recorded == 1
            assert sync_resp.log_batches_recorded == 1

        # Verify `fleetforge dock-sync` CLI subcommand
        bundle_file = tmp_path / "dock_bundle.json"
        bundle_file.write_text(json.dumps(bundle), encoding="utf-8")
        with patch(
            "fleetforge.cli._request_json",
            return_value=(200, sync_resp.model_dump(mode="json")),
        ) as mock_cli_req:
            rc = main(
                [
                    "--url",
                    "http://localhost:8000",
                    "--token",
                    "ffa_chloe.pit",
                    "dock-sync",
                    DEVICE_ID,
                    str(bundle_file),
                ]
            )
            assert rc == 0
            mock_cli_req.assert_called_once()
            assert mock_cli_req.call_args.args[1].endswith(f"/v1/devices/{DEVICE_ID}/dock-sync")


class TestTrack1InfrastructureAndSecurityHardening:
    def test_multifernet_secret_rotation_fallback_and_signing_default_ring_flag(self) -> None:
        from fleetforge.config import Settings
        from fleetforge.secrets_crypto import decrypt_secret, encrypt_secret
        from fleetforge.signing import DEFAULT_PRIMARY_PUBLIC_KEY_PEM, SigningKeyRing

        # 1. Encrypt under legacy ARTIFACT_URL_SECRET only
        legacy_settings = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            artifact_url_secret="legacy-artifact-url-secret-0001",
            secrets_encryption_key=None,
        )
        token = encrypt_secret("wifi-psk-top-secret", legacy_settings)

        # 2. Introduce SECRETS_ENCRYPTION_KEY while keeping ARTIFACT_URL_SECRET in MultiFernet ring
        dual_settings = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            artifact_url_secret="legacy-artifact-url-secret-0001",
            secrets_encryption_key="dedicated-secrets-key-0002",
        )
        assert decrypt_secret(token, dual_settings) == "wifi-psk-top-secret"

        # 3. Re-encrypting under dual_settings uses primary SECRETS_ENCRYPTION_KEY, surviving ARTIFACT_URL_SECRET rotation
        rotated_token = encrypt_secret("wifi-psk-top-secret", dual_settings)
        post_rotate_settings = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            artifact_url_secret="brand-new-artifact-url-secret-0003",
            secrets_encryption_key="dedicated-secrets-key-0002",
        )
        assert decrypt_secret(rotated_token, post_rotate_settings) == "wifi-psk-top-secret"

        # 4. SigningKeyRing tracks whether it fell back to the built-in default dev ring
        assert SigningKeyRing.from_config().uses_default_ring is True
        assert (
            SigningKeyRing.from_config(public_keys=DEFAULT_PRIMARY_PUBLIC_KEY_PEM).uses_default_ring
            is False
        )

    def test_event_hub_fails_closed_for_tenant_projects_when_event_project_id_is_none(
        self,
    ) -> None:
        import uuid

        from fleetforge.api.eventstream import EventHub
        from fleetforge.db.models import DEFAULT_PROJECT_ID

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
        import base64
        import hashlib
        import uuid
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from fastapi import HTTPException

        from fleetforge.api.routers.device_secrets import get_device_secrets
        from fleetforge.config import Settings
        from fleetforge.db.models import Device, Project, ProjectSecret

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
        settings = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            artifact_url_secret="k" * 32,
        )

        with pytest.raises(HTTPException) as exc_info:
            await get_device_secrets(
                request=req,  # type: ignore[arg-type]
                settings=settings,
                sessionmaker=lambda: _FakeSessionCM(),  # type: ignore[arg-type]
            )
        assert exc_info.value.status_code == 500

    async def test_rollout_auto_advancement_halts_on_cancelled_and_caches_artifact_per_target(
        self,
    ) -> None:
        import datetime as dt
        import uuid
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from fleetforge.api.routers.deploys import advance_project_rollout_on_confirm
        from fleetforge.config import Settings
        from fleetforge.deploys import DeploySender, DeploySnapshot, RequestedRollout
        from fleetforge.firmware.manifest import BUILTIN_LAYOUTS
        from fleetforge.partition_profiles import LayoutCatalog

        proj_id = uuid.uuid4()
        now = dt.datetime.now(dt.UTC)
        devices = [
            Device(
                device_id=f"a4cf12b3de0{i}",
                project_id=proj_id,
                platform_type="esp32c6",
                fw_version="1.2.0" if i == 1 else "1.0.0",
                partition_layout="ab-4m-v1",
                ota_slot_size=1966080,
                rollback_capable=True,
                capabilities=["ota", "cancel"],
                link_type="wifi",
                power_class="always_on",
                is_dev=(i == 1),
                presence_reported=True,
                last_seen=now,
            )
            for i in range(1, 5)
        ]

        session = AsyncMock()
        session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: devices))
        art_row = SimpleNamespace(
            sha256="c" * 64,
            size_bytes=2048,
            partition_layout="ab-4m-v1",
            has_lib_marker=True,
            sig=None,
            artifact_type="app",
        )
        session.execute = AsyncMock(return_value=SimpleNamespace(first=lambda: art_row))
        settings = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            artifact_url_secret="s" * 32,
            public_base_url="https://fleet.example.com",
        )
        publisher = AsyncMock()
        catalog = LayoutCatalog(layouts=BUILTIN_LAYOUTS, by_fingerprint={}, pending=frozenset())
        rollout_req = RequestedRollout(
            artifact_version="1.2.0",
            apply="auto",
            sent_by=DeploySender(subject="ci", token_id="tok-1", credential="ci-token"),
            rollout={
                "target_filter": "all",
                "canary": True,
                "max_parallel": 2,
                "override": [],
                "target_chip": "esp32c6",
            },
        )

        # When device 2 was cancelled on 1.2.0, background rollout auto-advancement halts
        cancelled_snapshots = {
            "a4cf12b3de01": DeploySnapshot(
                cmd_id="cmd-1",
                state="confirmed",
                at=now,
                is_terminal=True,
                artifact_version="1.2.0",
                from_version="1.0.0",
                pct=None,
                detail=None,
                steps=(),
                sent_by=None,
                selftest=None,
            ),
            "a4cf12b3de02": DeploySnapshot(
                cmd_id="cmd-2",
                state="cancelled",
                at=now,
                is_terminal=True,
                artifact_version="1.2.0",
                from_version="1.0.0",
                pct=None,
                detail="cancelled by operator",
                steps=(),
                sent_by=None,
                selftest=None,
            ),
        }
        with (
            patch("fleetforge.api.routers.deploys.requested_rollout", return_value=rollout_req),
            patch("fleetforge.api.routers.deploys.load_layout_catalog", return_value=catalog),
            patch("fleetforge.api.routers.deploys.latest_open_transaction", return_value=None),
            patch("fleetforge.api.routers.deploys.record_requested", AsyncMock()),
            patch(
                "fleetforge.api.routers.deploys.latest_deploys",
                return_value=cancelled_snapshots,
            ),
        ):
            advanced = await advance_project_rollout_on_confirm(
                session,
                device=devices[0],
                cmd_id="cmd-1",
                settings=settings,
                publisher=publisher,
            )
            assert advanced == []
            # Only the Project.id FOR UPDATE lock query executed; zero artifact lookups ran
            assert session.execute.call_count == 1

        # When not cancelled, all 3 eligible esp32c6 boards share 1 cached artifact lookup
        session.execute.reset_mock()
        confirmed_only_snapshots = {
            "a4cf12b3de01": cancelled_snapshots["a4cf12b3de01"],
        }
        with (
            patch("fleetforge.api.routers.deploys.requested_rollout", return_value=rollout_req),
            patch("fleetforge.api.routers.deploys.load_layout_catalog", return_value=catalog),
            patch("fleetforge.api.routers.deploys.latest_open_transaction", return_value=None),
            patch("fleetforge.api.routers.deploys.record_requested", AsyncMock()),
            patch(
                "fleetforge.api.routers.deploys.latest_deploys",
                return_value=confirmed_only_snapshots,
            ),
        ):
            advanced2 = await advance_project_rollout_on_confirm(
                session,
                device=devices[0],
                cmd_id="cmd-1",
                settings=settings,
                publisher=publisher,
            )
            assert len(advanced2) == 2
            # 1 FOR UPDATE lock query + 1 cached _ARTIFACT_FOR_TARGET_SQL query for esp32c6
            assert session.execute.call_count == 2


class TestTrack2ShowcaseAndUxHygiene:
    """Track 2 showcase repo & documentation hygiene checks (DOC-1..DOC-5, IMPL-1)."""

    def test_license_and_manifest_metadata_present(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        license_file = repo_root / "LICENSE"
        assert license_file.is_file()
        assert "MIT License" in license_file.read_text()

        pyproject = (repo_root / "pyproject.toml").read_text()
        assert 'license = { text = "MIT" }' in pyproject

        pkg_json = json.loads((repo_root / "frontend" / "package.json").read_text())
        assert pkg_json.get("license") == "MIT"

        lib_json = json.loads(
            (repo_root / "agent" / "components" / "fleetforge" / "library.json").read_text()
        )
        assert lib_json.get("license") == "MIT"

        idf_yml = (
            repo_root / "agent" / "components" / "fleetforge" / "idf_component.yml"
        ).read_text()
        assert 'license: "MIT"' in idf_yml

    def test_critical_md_paths_exist(self) -> None:
        """DOC-5: Every concrete repository file path named in CRITICAL.md must exist."""
        import re

        repo_root = Path(__file__).resolve().parent.parent
        critical_text = (repo_root / "CRITICAL.md").read_text()
        # Extract backticked repo-relative paths containing a slash and not wildcards/routes
        tokens = re.findall(r"`([A-Za-z0-9_./-]+\.[A-Za-z0-9_]+)`", critical_text)
        checked = 0
        for tok in tokens:
            if "/" not in tok or tok.startswith("/"):
                continue
            target = repo_root / tok
            assert target.exists(), f"CRITICAL.md references missing path: {tok}"
            checked += 1
        assert checked >= 15

    def test_docs_have_no_unshipped_claims_or_stale_todo_links(self) -> None:
        """DOC-2, DOC-4: README, releases.md, and library README match shipped code."""
        repo_root = Path(__file__).resolve().parent.parent
        readme = (repo_root / "README.md").read_text()
        assert "v0.8.0" in readme
        assert "(TODO.md)" not in readme

        releases = (repo_root / "docs" / "releases.md").read_text()
        for unshipped in (
            "actions/fleetforge-push/action.yml",
            ".gitlab/fleetforge-push.yml",
            ".gitea/workflows/fleetforge-push.yml",
            "cloudbuild/fleetforge-push.yaml",
            "auto_on_tag",
            "require_ci_tests",
        ):
            assert unshipped not in releases, unshipped

        lib_readme = (repo_root / "agent" / "components" / "fleetforge" / "README.md").read_text()
        assert "FluxTray.jar" not in lib_readme

    def test_cloudbuild_ci_pipeline_present_and_no_github_workflows(self) -> None:
        """IMPL-1: CI runs on GCP Cloud Build (`cloudbuild.yaml`) with no `.github/workflows`."""
        repo_root = Path(__file__).resolve().parent.parent
        cb = repo_root / "cloudbuild.yaml"
        assert cb.is_file()
        cb_text = cb.read_text()
        assert "python-lint-typecheck-and-unit-tests" in cb_text
        assert "frontend-typecheck-and-tests" in cb_text
        assert "docker-build-images" in cb_text
        assert not (repo_root / ".github" / "workflows").exists()

    def test_frontend_ux4_ux5_ux6_confirmations_queued_breakdown_and_copy_snippet(self) -> None:
        """UX-4, UX-5, UX-6, UX-14: Destructive confirmations, queued vs skipped, and copy snippet."""
        repo_root = Path(__file__).resolve().parent.parent
        dashboard_tsx = (repo_root / "frontend" / "src" / "Dashboard.tsx").read_text()
        assert 'data-testid="confirm-bulk-action"' in dashboard_tsx
        assert "Confirm deploy all" in dashboard_tsx
        assert "Confirm cancel rollout" in dashboard_tsx
        assert "queued for next wave" in dashboard_tsx
        assert 'data-testid="copy-cli-snippet"' in dashboard_tsx
        assert "RULE E301" not in dashboard_tsx

        project_access_tsx = (repo_root / "frontend" / "src" / "ProjectAccess.tsx").read_text()
        assert 'data-testid="confirm-delete-secret"' in project_access_tsx
        assert 'data-testid="confirm-delete-group"' in project_access_tsx
