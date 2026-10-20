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
from fleetforge.db.models import Project
from fleetforge.ingestor.protocol import AnnouncePayload
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
