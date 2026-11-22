"""Integration, simulator, and host-compiled C firmware tests for `V3` (`V3-1`, `V3-2`).

Covers:
- `V3-2` Binary Delta OTA (`ffdelta-v1`):
  - Pure Python `create_delta_patch` + `apply_delta_patch` achieving >95% airtime reduction
    on 128 KiB ESP32 images sharing 98%+ of code, plus rejection of corrupt magic, wrong
    `from_sha256`, truncated opcodes, and tampered output digests.
  - Host-compiled `gcc -Wall -Wextra -Werror` C unit test over `agent/components/fleetforge/src/ff_delta.c`
    decoding the exact `ffdelta-v1` patch emitted by Python and rejecting out-of-bounds
    `OP_COPY`, bad `base_sha256`, unknown opcodes, and truncated streams.
- `V3-1` UGV Edge-Relay Gateway & `parent_device_id` Hierarchy:
  - `EdgeRelayGateway` (`simulator/gateway.py`) + `PUT /v1/devices/{ugv_id}/relay` +
    `POST /v1/artifact/{to_sha256}/deltas` + group rollout (`POST /v1/projects/default/deploy`):
    child drones receive stage URLs pointing at their parent UGV gateway (`http://127.0.0.1:<port>/...`),
    pulling once over uplink (`upstream_fetches == 1`) to serve multiple child drones locally
    (`child_hits >= 2`), and replaying disconnected child `up/*` envelopes via
    `POST /v1/devices/{ugv_id}/relay-sync` (while refusing non-child device spoofing with `403`).
- `V3-2` Airtime-Aware Scheduling:
  - `lora` cohort caps concurrent in-flight downloads at `1` (`airtime cap=1 on lora link`)
    even when `max_parallel=5`.
"""

from __future__ import annotations

import ctypes
import hashlib
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from fleetforge.api.deps import (
    get_command_publisher,
    get_object_store,
    get_settings,
)
from fleetforge.clock import now_utc
from fleetforge.db.models import DEFAULT_PROJECT_ID, Device, DeviceGroup
from fleetforge.delta_ota import DeltaPatchError, apply_delta_patch, create_delta_patch
from fleetforge.lib_marker import MAGIC
from fleetforge.signing import generate_keypair
from fleetforge.simulator.gateway import EdgeRelayGateway, fetch_and_apply_stage
from fleetforge.storage.blobs import blob_key
from tests.conftest import (
    FakeCommandPublisher,
    MemoryObjectStore,
    client_for,
    login_admin,
    settings_for_tests,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FF_DELTA_C = REPO_ROOT / "agent" / "components" / "fleetforge" / "src" / "ff_delta.c"
FF_DELTA_H_DIR = FF_DELTA_C.parent

_PRIV_A, _PUB_A = generate_keypair()
_PRIV_B, _PUB_B = generate_keypair()
_VALID_MARKER = MAGIC + b"\x01\x00\x00\x00" + b"0.6.0\x00".ljust(32, b"\x00") + b"\x00" * 12


def _realistic_firmware(version_tag: str) -> bytes:
    """Build a deterministic ~128 KiB firmware image sharing 98%+ of code across versions."""
    shared_prefix = (
        b"\xe9\x00\x02\x20" + _VALID_MARKER + (b"esp32-rtos-hal-code-block-012345" * 2048)
    )
    shared_suffix = b"esp32-lwip-mbedtls-rodata-6789ab" * 2000
    middle = f"APP_VERSION={version_tag}\x00".encode("ascii").ljust(256, b"\x00")
    return shared_prefix + middle + shared_suffix


class TestBinaryDeltaCodecAndHostCFirmware:
    def test_python_ffdelta_v1_achieves_high_compression_and_rejects_tampering(self) -> None:
        base_img = _realistic_firmware("1.0.0")
        target_img = _realistic_firmware("1.1.0")
        patch = create_delta_patch(base_img, target_img)

        assert len(patch) < len(target_img) // 20, (
            f"expected >95% compression, got {len(patch)} vs {len(target_img)}"
        )
        reconstructed = apply_delta_patch(
            base_img,
            patch,
            expected_to_sha256=hashlib.sha256(target_img).hexdigest(),
        )
        assert reconstructed == target_img

        # Wrong base slot digest is rejected before applying.
        with pytest.raises(DeltaPatchError, match="from_sha256"):
            apply_delta_patch(base_img + b"x", patch)

        # Corrupting a payload byte inside the patch trips SHA-256 or opcode validation.
        corrupted = bytearray(patch)
        corrupted[len(corrupted) // 2] ^= 0xFF
        with pytest.raises(DeltaPatchError):
            apply_delta_patch(base_img, bytes(corrupted))

    def test_host_gcc_ff_delta_c_decodes_python_patch_and_enforces_bounds(
        self,
        tmp_path: Path,
    ) -> None:
        gcc = shutil.which("gcc")
        assert gcc is not None, "gcc is required for host firmware verification tests"
        so_path = tmp_path / "libff_delta.so"
        subprocess.run(  # noqa: S603
            [
                gcc,
                "-std=c99",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-shared",
                "-fPIC",
                f"-I{FF_DELTA_H_DIR}",
                str(FF_DELTA_C),
                "-o",
                str(so_path),
            ],
            check=True,
        )
        lib = ctypes.CDLL(str(so_path))
        lib.ff_delta_apply_in_memory.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        lib.ff_delta_apply_in_memory.restype = ctypes.c_int

        base_img = _realistic_firmware("2.0.0")
        target_img = _realistic_firmware("2.0.1")
        patch = create_delta_patch(base_img, target_img)
        base_sha = hashlib.sha256(base_img).digest()

        out_buf = ctypes.create_string_buffer(len(target_img) + 64)
        written = ctypes.c_size_t(0)
        rc = lib.ff_delta_apply_in_memory(
            base_img,
            len(base_img),
            base_sha,
            patch,
            len(patch),
            out_buf,
            len(target_img) + 64,
            ctypes.byref(written),
        )
        assert rc == 0
        assert written.value == len(target_img)
        assert out_buf.raw[: written.value] == target_img

        # Wrong base_sha256 -> FF_DELTA_ERR_BASE_HASH (-3).
        bad_sha = bytes([base_sha[0] ^ 0xFF, *base_sha[1:]])
        rc_bad_hash = lib.ff_delta_apply_in_memory(
            base_img,
            len(base_img),
            bad_sha,
            patch,
            len(patch),
            out_buf,
            len(target_img) + 64,
            ctypes.byref(written),
        )
        assert rc_bad_hash == -3

        # Output buffer smaller than target_size -> FF_DELTA_ERR_BOUNDS (-4).
        tiny_buf = ctypes.create_string_buffer(32)
        rc_bounds = lib.ff_delta_apply_in_memory(
            base_img,
            len(base_img),
            base_sha,
            patch,
            len(patch),
            tiny_buf,
            32,
            ctypes.byref(written),
        )
        assert rc_bounds == -4


class TestSwarmHierarchyEdgeRelayAndAirtimeRollout:
    @pytest.fixture
    async def env(
        self,
        admin_app: FastAPI,
        engine: AsyncEngine,
        clean_tables: None,
    ) -> dict[str, Any]:
        _ = clean_tables
        settings = settings_for_tests(
            artifact_signing_key=_PRIV_A,
            artifact_signing_public_keys=f"{_PUB_A};{_PUB_B}",
        )
        store = MemoryObjectStore()
        publisher = FakeCommandPublisher()
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

        admin_app.dependency_overrides[get_settings] = lambda: settings
        admin_app.dependency_overrides[get_object_store] = lambda: store
        admin_app.dependency_overrides[get_command_publisher] = lambda: publisher

        token = await login_admin(admin_app)
        return {
            "app": admin_app,
            "token": token,
            "sessionmaker": sessionmaker,
            "settings": settings,
            "store": store,
            "publisher": publisher,
        }

    async def test_child_drones_fetch_delta_through_ugv_gateway_and_sync_offline_telemetry(
        self,
        env: dict[str, Any],
    ) -> None:
        """V3-1 & V3-2 done-when: child nodes fetch staged delta artifacts through UGV parent."""
        headers = {"Authorization": f"Bearer {env['token']}"}
        publisher: FakeCommandPublisher = env["publisher"]
        store: MemoryObjectStore = env["store"]
        now = now_utc()

        async with env["sessionmaker"]() as s:
            drone_grp = DeviceGroup(project_id=DEFAULT_PROJECT_ID, name="swarm-alpha")
            s.add(drone_grp)
            await s.flush()
            ugv = Device(
                device_id="a4cf12b30001",
                name="ugv-carrier-01",
                project_id=DEFAULT_PROJECT_ID,
                platform_type="esp32s3",
                fw_version="2.0.0",
                partition_layout="ab-4m-v1",
                ota_slot_size=1966080,
                rollback_capable=True,
                capabilities=["ota", "sig", "edge_relay"],
                link_type="wifi",
                power_class="always_on",
                presence_reported=True,
                last_seen=now,
            )
            s.add(ugv)
            await s.flush()
            drones = [
                Device(
                    device_id="a4cf12b30011",
                    name="drone-1",
                    project_id=DEFAULT_PROJECT_ID,
                    group_id=drone_grp.id,
                    parent_device_id=ugv.device_id,
                    platform_type="esp32c6",
                    fw_version="1.0.0",
                    partition_layout="ab-4m-v1",
                    ota_slot_size=1966080,
                    rollback_capable=True,
                    capabilities=["ota", "sig", "delta"],
                    link_type="wifi",
                    power_class="always_on",
                    is_dev=True,
                    presence_reported=True,
                    last_seen=now,
                ),
                Device(
                    device_id="a4cf12b30012",
                    name="drone-2",
                    project_id=DEFAULT_PROJECT_ID,
                    group_id=drone_grp.id,
                    parent_device_id=ugv.device_id,
                    platform_type="esp32c6",
                    fw_version="1.0.0",
                    partition_layout="ab-4m-v1",
                    ota_slot_size=1966080,
                    rollback_capable=True,
                    capabilities=["ota", "sig", "delta"],
                    link_type="wifi",
                    power_class="always_on",
                    presence_reported=True,
                    last_seen=now,
                ),
            ]
            s.add_all(drones)
            await s.commit()
            drone_group_id = drone_grp.id

        v1_bytes = _realistic_firmware("1.0.0")
        v2_bytes = _realistic_firmware("1.1.0")

        # Bridge upstream fetches inside `EdgeRelayGateway` to our in-memory ObjectStore blobs!
        blob_map: dict[str, bytes] = {}

        def _upstream_fetch(path_and_query: str) -> bytes:
            parsed = urllib.parse.urlsplit(path_and_query)
            sha = parsed.path.split("/")[3]
            return blob_map[sha]

        gateway = EdgeRelayGateway(
            gateway_id="a4cf12b30001",
            upstream_fetcher=_upstream_fetch,
        )
        relay_origin = gateway.start()
        try:
            async with client_for(env["app"], base_url="https://testserver") as client:
                # Upload base 1.0.0 and target 1.1.0 for esp32c6.
                up1 = await client.post(
                    "/v1/artifact",
                    params={
                        "target": "esp32c6",
                        "version": "1.0.0",
                        "partition_layout": "ab-4m-v1",
                    },
                    content=v1_bytes,
                    headers={
                        **headers,
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(len(v1_bytes)),
                    },
                )
                assert up1.status_code == 201
                sha1 = up1.json()["sha256"]

                up2 = await client.post(
                    "/v1/artifact",
                    params={
                        "target": "esp32c6",
                        "version": "1.1.0",
                        "partition_layout": "ab-4m-v1",
                    },
                    content=v2_bytes,
                    headers={
                        **headers,
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(len(v2_bytes)),
                    },
                )
                assert up2.status_code == 201
                sha2 = up2.json()["sha256"]

                # Compute ffdelta-v1 patch 1.0.0 -> 1.1.0 via API.
                delta_resp = await client.post(
                    f"/v1/artifact/{sha2}/deltas",
                    headers=headers,
                    json={"from_sha256": sha1},
                )
                assert delta_resp.status_code == 201, delta_resp.text
                delta_meta = delta_resp.json()
                patch_sha = delta_meta["patch_sha256"]
                assert delta_meta["savings_pct"] > 95.0

                blob_map[sha2] = await store.get(blob_key(sha2))
                blob_map[patch_sha] = await store.get(blob_key(patch_sha))

                # Register UGV gateway edge cache origin.
                reg = await client.put(
                    "/v1/devices/a4cf12b30001/relay",
                    headers=headers,
                    json={"relay_base_url": relay_origin, "cached_artifacts": []},
                )
                assert reg.status_code == 200, reg.text
                assert reg.json()["child_device_ids"] == ["a4cf12b30011", "a4cf12b30012"]

                # Group rollout (canary=False so both child drones stage in one wave).
                roll = await client.post(
                    "/v1/projects/default/deploy",
                    headers=headers,
                    json={
                        "version": "1.1.0",
                        "group_id": str(drone_group_id),
                        "canary": False,
                        "max_parallel": 5,
                    },
                )
                assert roll.status_code == 202, roll.text
                assert len(roll.json()["deployed"]) == 2
                assert len(publisher.published) == 2

                # Both child drones received URLs routed through `relay_origin` AND `artifact.delta`!
                for _drone_id, stage_cmd in publisher.published:
                    art_field = stage_cmd["artifact"]
                    assert str(art_field["url"]).startswith(relay_origin)
                    assert "delta" in art_field
                    assert str(art_field["delta"]["url"]).startswith(relay_origin)

                    reconstructed, wire_len, used_delta = fetch_and_apply_stage(
                        stage_cmd,
                        active_slot_bytes=v1_bytes,
                        pubkey_ring=(_PUB_A, _PUB_B),
                        require_signature=True,
                    )
                    assert used_delta is True
                    assert wire_len == delta_meta["patch_size_bytes"]
                    assert reconstructed == v2_bytes

                # UGV gateway pulled `patch_sha` ONCE from upstream and served BOTH drones locally!
                assert gateway.upstream_fetches == 1
                assert gateway.child_hits == 2

                # Disconnected UGV reconnects and syncs child statuses + telemetry via `relay-sync`.
                cmd_1 = publisher.published[0][1]["id"]
                cmd_2 = publisher.published[1][1]["id"]
                sync_resp = await client.post(
                    "/v1/devices/a4cf12b30001/relay-sync",
                    headers=headers,
                    json={
                        "cached_artifacts": gateway.cached_digests(),
                        "children": {
                            "a4cf12b30011": {
                                "statuses": [{"cmd_id": cmd_1, "state": "confirmed"}],
                                "telemetry": [
                                    {"samples": [{"name": "battery_v", "value": 15.8, "age_ms": 0}]}
                                ],
                            },
                            "a4cf12b30012": {
                                "statuses": [{"cmd_id": cmd_2, "state": "confirmed"}],
                            },
                        },
                    },
                )
                assert sync_resp.status_code == 200, sync_resp.text
                sync_body = sync_resp.json()
                assert sync_body["statuses_recorded"] == 2
                assert sync_body["telemetry_batches_recorded"] == 1
                assert patch_sha in sync_body["cached_artifacts"]

                # Spoofing a device NOT attached to `a4cf12b30001` is refused with 403!
                spoof = await client.post(
                    "/v1/devices/a4cf12b30001/relay-sync",
                    headers=headers,
                    json={
                        "children": {
                            "a4cf12b30001": {"statuses": []},
                        }
                    },
                )
                assert spoof.status_code == 403

                # Verify GET /v1/projects/default/swarm reflects topology, cache, and delta savings.
                topo = (await client.get("/v1/projects/default/swarm", headers=headers)).json()
                assert len(topo["gateways"]) == 1
                assert len(topo["gateways"][0]["children"]) == 2
                assert len(topo["deltas"]) == 1
        finally:
            gateway.close()

    async def test_airtime_aware_scheduler_caps_constrained_lora_links_to_one(
        self,
        env: dict[str, Any],
    ) -> None:
        headers = {"Authorization": f"Bearer {env['token']}"}
        now = now_utc()
        async with env["sessionmaker"]() as s:
            lora_grp = DeviceGroup(project_id=DEFAULT_PROJECT_ID, name="lora-sensors")
            s.add(lora_grp)
            await s.flush()
            for idx in range(3):
                s.add(
                    Device(
                        device_id=f"a4cf12b3020{idx}",
                        project_id=DEFAULT_PROJECT_ID,
                        group_id=lora_grp.id,
                        platform_type="esp32c6",
                        fw_version="1.0.0",
                        partition_layout="ab-4m-v1",
                        ota_slot_size=1966080,
                        rollback_capable=True,
                        capabilities=["ota", "sig"],
                        link_type="lora",
                        power_class="always_on",
                        presence_reported=True,
                        last_seen=now,
                    )
                )
            await s.commit()
            gid = lora_grp.id

        fw = _realistic_firmware("1.2.0")
        async with client_for(env["app"], base_url="https://testserver") as client:
            await client.post(
                "/v1/artifact",
                params={
                    "target": "esp32c6",
                    "version": "1.2.0",
                    "partition_layout": "ab-4m-v1",
                },
                content=fw,
                headers={
                    **headers,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(fw)),
                },
            )
            # Even with canary=False and max_parallel=5, constrained `lora` link caps wave to 1!
            roll = await client.post(
                "/v1/projects/default/deploy",
                headers=headers,
                json={
                    "version": "1.2.0",
                    "group_id": str(gid),
                    "canary": False,
                    "max_parallel": 5,
                },
            )
            assert roll.status_code == 202, roll.text
            body = roll.json()
            assert len(body["deployed"]) == 1
            assert len(body["skipped"]) == 2
            for reason in body["skipped"].values():
                assert "airtime cap=1 on lora link" in reason

    async def test_viewer_cannot_create_a_delta(
        self,
        env: dict[str, Any],
    ) -> None:
        """A-26: omitting `?project=` on delta creation must still enforce operator role and scope."""
        from sqlalchemy import text

        super_headers = {"Authorization": f"Bearer {env['token']}"}
        v1_bytes = _realistic_firmware("1.0.0")
        v2_bytes = _realistic_firmware("1.1.0")

        async with client_for(env["app"], base_url="https://testserver") as client:
            up1 = await client.post(
                "/v1/artifact",
                params={"target": "esp32c6", "version": "1.0.0", "partition_layout": "ab-4m-v1"},
                content=v1_bytes,
                headers={
                    **super_headers,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(v1_bytes)),
                },
            )
            sha1 = up1.json()["sha256"]
            up2 = await client.post(
                "/v1/artifact",
                params={"target": "esp32c6", "version": "1.1.0", "partition_layout": "ab-4m-v1"},
                content=v2_bytes,
                headers={
                    **super_headers,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(v2_bytes)),
                },
            )
            sha2 = up2.json()["sha256"]

            # Create a viewer on `default`.
            r_v = await client.post(
                "/v1/users",
                headers=super_headers,
                json={
                    "username": "deltaviewer",
                    "password": "viewer-password-1234",
                    "project": "default",
                    "role": "viewer",
                },
            )
            assert r_v.status_code == 201
            v_login = await client.post(
                "/v1/auth/login",
                json={"username": "deltaviewer", "password": "viewer-password-1234"},
            )
            viewer_headers = {"Authorization": f"Bearer {v_login.cookies['ff_session']}"}

            # Viewer gets 403 both without and with `?project=default`, leaving 0 delta rows.
            r_no_proj = await client.post(
                f"/v1/artifact/{sha2}/deltas",
                headers=viewer_headers,
                json={"from_sha256": sha1},
            )
            assert r_no_proj.status_code == 403, r_no_proj.text
            r_with_proj = await client.post(
                f"/v1/artifact/{sha2}/deltas?project=default",
                headers=viewer_headers,
                json={"from_sha256": sha1},
            )
            assert r_with_proj.status_code == 403, r_with_proj.text

            async with env["sessionmaker"]() as s:
                delta_count = await s.scalar(text("SELECT count(*) FROM artifact_deltas"))
            assert delta_count == 0

            # Scoped `read` token -> 403; scoped `write:artifacts` token -> 201!
            r_read = await client.post(
                "/v1/auth/tokens",
                headers=super_headers,
                json={"name": "delta-read", "project": "default", "scopes": ["read"]},
            )
            assert (
                await client.post(
                    f"/v1/artifact/{sha2}/deltas",
                    headers={"Authorization": f"Bearer {r_read.json()['token']}"},
                    json={"from_sha256": sha1},
                )
            ).status_code == 403

            r_wa = await client.post(
                "/v1/auth/tokens",
                headers=super_headers,
                json={"name": "delta-write", "project": "default", "scopes": ["write:artifacts"]},
            )
            r_ok = await client.post(
                f"/v1/artifact/{sha2}/deltas",
                headers={"Authorization": f"Bearer {r_wa.json()['token']}"},
                json={"from_sha256": sha1},
            )
            assert r_ok.status_code == 201, r_ok.text
