"""Starter project scaffolding (`fleetforge new`), firmware template parity, cargo OTA, and pit dock-sync (`T-10`)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from fleetforge.announce_fields import normalize_sub_versions
from fleetforge.api.deps import AuthContext
from fleetforge.api.routers.devices import dock_sync_device
from fleetforge.api.schemas import DockSyncRequest
from fleetforge.cli import main
from fleetforge.db.models import Device
from fleetforge.deploys import DeploySnapshot
from fleetforge.ingest.protocol import AnnouncePayload
from fleetforge.signing import sign_digest
from fleetforge.simulator.device import (
    CAPABILITY_SELFTEST,
    SAFE_WINDOW_HOLD,
    DeviceIdentity,
    LinkProfile,
    StageRunner,
)
from tests.conftest import settings_for_tests
from tests.doubles import FakeMqttClient
from tests.fixtures.dev_signing_keys import DEV_PRIMARY_PRIVATE_KEY_PEM
from tests.support_deploy import DEVICE_ID


def test_cli_new_scaffolds_project_and_keeps_immutables_in_sync(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
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
    assert (tpl_dir / "AGENTS.md.tmpl").is_file()
    assert not (tpl_dir / "AGENTS.md").exists()
    assert (dest / "AGENTS.md").is_file()
    assert not (dest / "AGENTS.md.tmpl").exists()
    assert "{{" not in (dest / "AGENTS.md").read_text(encoding="utf-8")
    assert (dest / "CLAUDE.md").exists()
    assert not (dest / "cloudbuild.yaml").exists()
    gh_wf = dest / ".github" / "workflows" / "fleetforge.yml"
    assert gh_wf.is_file()
    gh_text = gh_wf.read_text(encoding="utf-8")
    assert "idf.py set-target esp32c6" in gh_text
    assert "build/oled_status.bin" in gh_text
    assert "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683" in gh_text
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in gh_text
    assert "urllib.parse.urlencode" in gh_text
    main_c_text = (dest / "main" / "main.c").read_text(encoding="utf-8")
    assert "has_url ? api_url" not in main_c_text
    just_text = (dest / "justfile").read_text(encoding="utf-8")
    assert "build-idf:" in just_text
    assert "build-docker:" in just_text
    assert "cloudbuild" not in just_text
    assert "project(oled_status)" in (dest / "CMakeLists.txt").read_text(encoding="utf-8")
    idf_yml = (dest / "main" / "idf_component.yml").read_text(encoding="utf-8")
    assert "https://github.com/tvaroska/fleetforge.git" in idf_yml
    assert "path: agent/components/fleetforge" in idf_yml
    assert "version: v0.8.4" in idf_yml
    sdk = (dest / "sdkconfig.defaults").read_text(encoding="utf-8")
    assert "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y" in sdk
    assert "CONFIG_MBEDTLS_DYNAMIC_BUFFER=y" in sdk

    rc_exists = main(["new", str(dest)])
    assert rc_exists == 1


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

        assert "/v1/device-secrets" in ff_sec_c
        assert "ff_get_secret" in ff_sec_c
        assert "ff_sec_synced()" in ff_mqtt_c
        assert "nvs_erase_all" not in ff_sec_c
        assert "nvs_erase_key" in ff_sec_c
        assert "SECRETS_MAX_RETRIES" in ff_sec_c
        assert "s_failed_rev >= s_target_rev" in ff_sec_c

        assert "RTC_NOINIT_ATTR" in ff_log_c
        assert "[prev-boot] %s" in ff_log_c
        assert "batch[FF_LOG_CAPACITY]" not in ff_log_c
        assert "ff_log_drained()" in ff_sleep_c
        assert "-Wframe-larger-than=1024" in (root / "CMakeLists.txt").read_text(encoding="utf-8")

        ff_identity_c = (root / "src" / "ff_identity.c").read_text(encoding="utf-8")
        ff_net_c = (root / "src" / "ff_net.c").read_text(encoding="utf-8")
        agent_src = "\n".join(
            p.read_text(encoding="utf-8") for p in sorted((root / "src").glob("*.[ch]"))
        )
        assert "00467768-6228-2272-4663-277478268000" in agent_src
        assert "00467768-6228-2272-4663-277478268003" in agent_src
        assert "improv_handle_rpc_command" in agent_src
        assert "expected_csum" in agent_src
        assert "ble_gatts_chr_updated" in agent_src
        assert "improv_ble_notify_status" in agent_src
        assert "IMPROV_STATE_PROVISIONED" in agent_src
        assert "load_nvs_net_overlay" in agent_src
        assert "IMPROV_BLE_WINDOW_S" in agent_src
        assert "improv_ble_window_start" in agent_src
        assert "s_retry_timer" in agent_src
        assert "vTaskDelay(" not in ff_wifi_c
        assert "ff_net_known_networks(cfg->net_count)" in ff_identity_c
        assert "ff_net_wifi_known_count()" in ff_net_c

        assert "Fleetforge.logf" in basic_ino
        assert "Fleetforge.getSecret" in basic_ino
        assert "ff_logf" in basic_idf
        assert "ff_get_secret" in basic_idf


async def test_sub_versions_and_pluggable_cargo_handler_end_to_end() -> None:
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
        json.loads(raw) for topic, raw, _, _ in client.published if topic.endswith("/up/announce")
    ]
    assert published_announces[-1]["sub_versions"] == {"model_weights": "2.1.0"}

    root = Path(__file__).resolve().parents[1] / "agent" / "components" / "fleetforge"
    agent_src = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted((root / "src").glob("*.[ch]"))
    )
    ff_id_c = (root / "src" / "ff_identity.c").read_text(encoding="utf-8")
    assert "ff_ota_register_cargo_handler" in agent_src
    assert "run_cargo_ota" in agent_src
    assert "ff_identity_set_sub_version" in ff_id_c
    assert "ff_mqtt_complete_cargo" in agent_src


class TestCuj3PitTetheredDockAndVenueOffline:
    def test_cuj3_firmware_venue_offline_nimble_coexistence_and_dock_drain(self) -> None:
        root = Path(__file__).resolve().parents[1] / "agent" / "components" / "fleetforge"
        ff_sec_h = (root / "include" / "ff_sec.h").read_text(encoding="utf-8")
        ff_net_c = (root / "src" / "ff_net.c").read_text(encoding="utf-8")
        ff_wifi_c = (root / "src" / "ff_net_wifi.c").read_text(encoding="utf-8")
        ff_mqtt_c = (root / "src" / "ff_mqtt.c").read_text(encoding="utf-8")
        ff_log_c = (root / "src" / "ff_log.c").read_text(encoding="utf-8")
        ff_telem_c = (root / "src" / "ff_telemetry.c").read_text(encoding="utf-8")
        arduino_h = (root / "src" / "Fleetforge.h").read_text(encoding="utf-8")
        arduino_cpp = (root / "src" / "Fleetforge.cpp").read_text(encoding="utf-8")
        basic_idf = (root / "examples" / "basic_idf" / "main" / "main.c").read_text(
            encoding="utf-8"
        )
        agent_src = "\n".join(
            p.read_text(encoding="utf-8") for p in sorted((root / "src").glob("*.[ch]"))
        )

        assert "esp_err_t ff_set_offline_mode(bool offline);" in ff_sec_h
        assert "ff_set_offline_mode(" in ff_net_c
        assert "ff_net_set_dock_active" not in ff_net_c
        assert "s_venue_offline" in ff_wifi_c
        assert "s_venue_offline" not in ff_mqtt_c
        assert "s_improv_ble_enabled" in agent_src
        assert "ble_hs_synced()" in agent_src
        assert "ff_net_wifi_set_offline_mode(" in agent_src
        assert "ff_net_wifi_set_improv_ble_enabled(" in agent_src
        assert "ff_net_wifi_improv_authorize(" in agent_src
        assert "ff_mqtt_confirm_offline(" in agent_src
        assert "if (!s_selftest_registered)" in agent_src
        assert "setOfflineMode(bool offline)" in arduino_h
        assert "FleetforgeClass::setOfflineMode(bool offline)" in arduino_cpp
        assert "ff_set_offline_mode(false);" in basic_idf

        assert "ff_log_drain_json(" in ff_log_c
        assert "ff_telemetry_drain_json(" in ff_telem_c
        assert "esp_err_t ff_ota_dock_stage(" in agent_src
        assert "ff_ota_dock_stage(const ff_ota_cmd_t *cmd" in agent_src

        ingestor_main = (
            Path(__file__).resolve().parents[1] / "src" / "fleetforge" / "ingestor" / "main.py"
        ).read_text(encoding="utf-8")
        assert "fleetforge.api" not in ingestor_main

    async def test_cuj3_end_to_end_venue_offline_dock_stage_selftest_and_sync(
        self, tmp_path: Path
    ) -> None:
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

        assert runner.record_telemetry("lora_rssi", -78.0) is True
        fake_now[0] += 5.0
        assert runner.record_telemetry("can_bus_err_cnt", 0.0) is True
        runner.record_log("I", "qual 11: scouting packet synced over NimBLE")
        fake_now[0] += 5.0
        runner.record_log("E", "watchdog reset before pit dock", prev_boot=True)

        lut_bytes = b"BAT01,12.8V\nBAT02,12.6V\n"
        lut_sha = hashlib.sha256(lut_bytes).hexdigest()
        lut_sig = sign_digest(lut_sha, DEV_PRIMARY_PRIVATE_KEY_PEM)
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

        good_bin = b"\xe9\x00" + b"FFOTALIB:2026.2.2-qual12" * 16
        good_sha = hashlib.sha256(good_bin).hexdigest()
        good_sig = sign_digest(good_sha, DEV_PRIMARY_PRIVATE_KEY_PEM)

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
        ]
        runner.dock_reboot_staged()
        boot_steps = runner.dock_confirm_boot()
        assert [s["state"] for s in boot_steps] == ["confirming", "confirmed"]
        assert boot_steps[-1]["detail"] == "selftest passed"
        assert runner.identity.fw_version == "2026.2.2-qual12"

        bad_bin = b"\xe9\x00" + b"FFOTALIB:2026.2.3-badcan" * 16
        bad_sha = hashlib.sha256(bad_bin).hexdigest()
        bad_sig = sign_digest(bad_sha, DEV_PRIMARY_PRIVATE_KEY_PEM)
        runner.dock_stage(
            "cmd-dock-app-bad",
            bad_bin,
            sha256=bad_sha,
            version="2026.2.3-badcan",
            sig=bad_sig,
        )
        runner.dock_reboot_staged()
        rb_steps = runner.dock_confirm_boot()
        assert [s["state"] for s in rb_steps] == ["confirming", "rolling_back", "rolled_back"]
        assert (
            rb_steps[-1]["detail"]
            == "selftest failed: simulated self-test failure; returned to ota_1"
        )
        assert runner.identity.fw_version == "2026.2.2-qual12"

        bundle = runner.dock_drain()
        assert bundle["announce"]["link_type"] == "dock"  # type: ignore[index]
        assert bundle["announce"]["fw_version"] == "2026.2.2-qual12"  # type: ignore[index]
        assert bundle["announce"]["sub_versions"] == {"data_partition": "v14"}  # type: ignore[index]
        assert len(bundle["telemetry"]) == 1  # type: ignore[arg-type]
        assert len(bundle["logs"]) == 1  # type: ignore[arg-type]
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
        settings = settings_for_tests(
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
            **_extra: Any,
        ) -> Any:
            _ = (retained, tolerance, settings, publisher)
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
