"""CLI push/track/login, provenance schemas, `dn/cfg`, wake-aware URL TTL, and HMAC checks (`T-10`)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from fleetforge.api.schemas import (
    ArtifactSummary,
    ArtifactUploaded,
    DeviceSummary,
    DeviceUpdate,
    ProjectDeployRequest,
    ProjectSummary,
    ProjectUpdate,
)
from fleetforge.artifact_urls import (
    ArtifactUrlExpired,
    ArtifactUrlInvalid,
    mint_artifact_url,
    verify_artifact_url,
)
from fleetforge.broker import (
    CommandPublishError,
    NullCommandPublisher,
    cfg_payload,
    cfg_topic,
)
from fleetforge.broker.publisher import CFG_RETAIN, QOS, RETAIN, MqttCommandPublisher
from fleetforge.cli import build_artifact_upload_url, detect_git_metadata, main
from fleetforge.db.models import Device
from fleetforge.deploys import DeploySnapshot, DeployTransaction
from fleetforge.ingest.protocol import AnnouncePayload
from fleetforge.rollout import republish_open_requested_on_wake
from fleetforge.rollout_state import effective_url_ttl_s
from tests.conftest import settings_for_tests
from tests.doubles import FakeMqttClient
from tests.support_deploy import DEVICE_ID


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

        assert list(DeviceSummary.model_fields)[-1] == "deploy"
        assert "secrets_rev" in ProjectSummary.model_fields

    def test_non_app_artifact_schema_serialization_omits_app_and_includes_cargo(self) -> None:
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
            _ = (token, content_type)
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
            _ = (token, body, content_type)
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
            assert main(["--token", "ffa_test.token", "status", "--project", "eink-todo"]) == 0
            status_out = capsys.readouterr().out
            assert "kitchen-eink" in status_out
            assert "64 KB" in status_out
            assert "confirmed (2.0.1)" in status_out

            assert main(["--token", "ffa_test.token", "logs", "--project", "eink-todo"]) == 0
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

    def test_cli_login_whoami_logout_and_saved_credentials_fallback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FLEETFORGE_TOKEN", raising=False)
        monkeypatch.delenv("FLEETFORGE_URL", raising=False)
        creds_file = tmp_path / "creds" / "credentials.json"
        env_file = tmp_path / ".env"

        requests_seen: list[tuple[str, str, str]] = []

        def fake_login(
            base_url: str, username: str, password: str
        ) -> tuple[int, dict[str, Any], str | None]:
            assert base_url == "https://bingo.tvaroska.sk"
            assert username == "boris"
            assert password == "s3cret-pass"
            return (
                200,
                {"expires_at": "2026-11-13T21:00:00Z"},
                "ffa_00000000-0000-4000-8000-000000000099_sessionsecret",
            )

        def fake_request(
            method: str,
            url: str,
            *,
            token: str,
            body: bytes | None = None,
            content_type: str = "application/octet-stream",
        ) -> tuple[int, dict[str, Any]]:
            _ = content_type
            requests_seen.append((method, url, token))
            if method == "POST" and url.endswith("/v1/auth/tokens"):
                payload = json.loads((body or b"{}").decode("utf-8"))
                assert payload["project"] == "eink-todo"
                assert payload["scopes"] == ["read", "write:artifacts", "deploy"]
                return 201, {
                    "id": "00000000-0000-4000-8000-000000000100",
                    "token": "ffa_00000000-0000-4000-8000-000000000100_mintedsecret",
                    "name": payload["name"],
                    "subject": "boris",
                    "scopes": payload["scopes"],
                    "project_slug": "eink-todo",
                }
            if method == "GET" and url.endswith("/v1/auth/me"):
                return 200, {
                    "token_id": "00000000-0000-4000-8000-000000000100",
                    "subject": "boris",
                    "email": "boris@tvaroska.com",
                    "display_name": "Boris Tvaroska",
                    "is_superadmin": True,
                    "scopes": ["read", "write:artifacts", "deploy"],
                    "expires_at": "2026-12-06T21:00:00Z",
                    "projects": [{"slug": "eink-todo", "role": "owner"}],
                }
            if method == "GET" and "/v1/devices?" in url:
                return 200, {"devices": [], "arrivals": []}
            if method == "POST" and url.endswith("/v1/auth/logout"):
                return 204, {}
            return 200, {}

        with (
            patch("fleetforge.cli._login_with_password", side_effect=fake_login),
            patch("fleetforge.cli._request_json", side_effect=fake_request),
        ):
            rc_login = main(
                [
                    "--credentials-file",
                    str(creds_file),
                    "login",
                    "-u",
                    "boris",
                    "-p",
                    "s3cret-pass",
                    "--project",
                    "eink-todo",
                    "--write-env",
                    str(env_file),
                ]
            )
            assert rc_login == 0
            login_out = json.loads(capsys.readouterr().out)
            assert login_out["status"] == "authenticated"
            assert login_out["subject"] == "boris"
            assert creds_file.is_file()
            assert (creds_file.stat().st_mode & 0o777) == 0o600
            env_text = env_file.read_text(encoding="utf-8")
            assert (
                "FLEETFORGE_TOKEN=ffa_00000000-0000-4000-8000-000000000100_mintedsecret" in env_text
            )
            assert "FLEETFORGE_PROJECT=eink-todo" in env_text

            rc_whoami = main(["--credentials-file", str(creds_file), "whoami"])
            assert rc_whoami == 0
            whoami_out = capsys.readouterr().out
            assert "boris (boris@tvaroska.com)" in whoami_out
            assert "eink-todo (owner)" in whoami_out

            rc_status = main(
                ["--credentials-file", str(creds_file), "status", "--project", "eink-todo"]
            )
            assert rc_status == 0
            assert "No enrolled devices found." in capsys.readouterr().out
            assert requests_seen[-1][2] == "ffa_00000000-0000-4000-8000-000000000100_mintedsecret"

            rc_logout = main(["--credentials-file", str(creds_file), "logout"])
            assert rc_logout == 0
            logout_out = json.loads(capsys.readouterr().out)
            assert logout_out["removed_saved_profile"] is True

            rc_unauth = main(["--credentials-file", str(creds_file), "status"])
            assert rc_unauth == 2

    def test_cli_push_deploy_and_track_resolve_saved_or_pinned_project_without_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FLEETFORGE_PROJECT", raising=False)
        bin_file = tmp_path / "app.bin"
        bin_file.write_bytes(b"\xe9\x00" + b"z" * 128)
        creds_file = tmp_path / "credentials.json"
        creds_file.write_text(
            json.dumps(
                {
                    "default_url": "https://bingo.tvaroska.sk",
                    "profiles": {
                        "https://bingo.tvaroska.sk": {
                            "url": "https://bingo.tvaroska.sk",
                            "token": "ffa_00000000-0000-4000-8000-000000000100_mintedsecret",
                            "project": "eink-todo",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        seen_urls: list[tuple[str, str]] = []

        def fake_request(
            method: str,
            url: str,
            *,
            token: str,
            body: bytes | None = None,
            content_type: str = "application/octet-stream",
        ) -> tuple[int, dict[str, Any]]:
            _ = (token, content_type)
            seen_urls.append((method, url))
            if method == "POST" and "/v1/artifact?" in url:
                return 201, {
                    "sha256": "b" * 64,
                    "size_bytes": len(body or b""),
                    "target": "esp32s3",
                    "version": "2.0.2",
                    "partition_layout": "ab-4m-v1",
                    "created": True,
                }
            if method == "POST" and url.endswith("/deploy"):
                return 202, {
                    "project": "eink-todo",
                    "version": "2.0.2",
                    "target_filter": "all",
                    "deployed": [DEVICE_ID],
                    "skipped": {},
                }
            if method == "PATCH":
                return 200, {"slug": "eink-todo", "track_mode": "branch", "track_ref": "main"}
            return 200, {}

        with patch("fleetforge.cli._request_json", side_effect=fake_request):
            rc_push = main(
                [
                    "--credentials-file",
                    str(creds_file),
                    "push",
                    str(bin_file),
                    "--target",
                    "esp32s3",
                    "--version",
                    "2.0.2",
                    "--no-git",
                    "--deploy",
                    "all",
                ]
            )
            assert rc_push == 0
            assert "project=eink-todo" in seen_urls[0][1]
            assert seen_urls[1] == (
                "POST",
                "https://bingo.tvaroska.sk/v1/projects/eink-todo/deploy",
            )

            rc_track = main(
                [
                    "--credentials-file",
                    str(creds_file),
                    "track",
                    "--mode",
                    "branch",
                    "--ref",
                    "main",
                ]
            )
            assert rc_track == 0
            assert seen_urls[2] == ("PATCH", "https://bingo.tvaroska.sk/v1/projects/eink-todo")


async def test_wake_aware_url_ttl_and_on_wake_stage_republish() -> None:
    settings = settings_for_tests(
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
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: []))
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
            "fleetforge.rollout.latest_deploys",
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
        patch("fleetforge.rollout.requested_rollout", return_value=None),
        patch(
            "fleetforge.rollout.deploy_transaction",
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


def test_verify_artifact_url_enforces_hmac_signature_and_expiry() -> None:
    """Offline guard for `artifact_urls.verify_artifact_url` MAC and expiry checks."""
    digest = "a" * 64
    secret = "test-url-secret-0123456789abcdef"  # noqa: S105
    t0 = dt.datetime(2026, 11, 26, 12, 0, 0, tzinfo=dt.UTC)
    url = mint_artifact_url(
        "https://fleet.example.com",
        digest,
        secret=secret,
        ttl_s=60,
        now=t0,
    )
    params = dict(pair.split("=", 1) for pair in url.split("?", 1)[1].split("&"))
    assert (
        verify_artifact_url(
            digest,
            exp=params["exp"],
            sig=params["sig"],
            secret=secret,
            now=t0 + dt.timedelta(seconds=30),
        )
        == int(t0.timestamp()) + 60
    )

    with pytest.raises(ArtifactUrlInvalid):
        verify_artifact_url(
            digest,
            exp=params["exp"],
            sig=params["sig"][:-1] + ("A" if params["sig"][-1] != "A" else "B"),
            secret=secret,
            now=t0 + dt.timedelta(seconds=30),
        )

    with pytest.raises(ArtifactUrlExpired):
        verify_artifact_url(
            digest,
            exp=params["exp"],
            sig=params["sig"],
            secret=secret,
            now=t0 + dt.timedelta(seconds=61),
        )
