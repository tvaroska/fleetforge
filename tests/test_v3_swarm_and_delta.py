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
import struct
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
from fleetforge.simulator.errors import SimulatorError
from fleetforge.simulator.gateway import EdgeRelayGateway, apply_ffdelta_v1, fetch_and_apply_stage
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

    def test_host_gcc_ff_delta_c_rejects_hostile_patches(self, tmp_path: Path) -> None:
        """D-24: shared hostile `ffdelta-v1` vector table rejected across Python, C (`ff_delta.c`), and simulator."""
        _run_hostile_delta_vectors(tmp_path)

    def test_malformed_patches_are_rejected_by_python_and_c(self, tmp_path: Path) -> None:
        """T-18: malformed patches fail in Python (`apply_delta_patch`), C (`ff_delta.c`), and simulator."""
        _run_hostile_delta_vectors(tmp_path)


def _compile_ff_delta(tmp_path: Path) -> ctypes.CDLL:
    gcc = shutil.which("gcc")
    assert gcc is not None, "gcc is required for host firmware verification tests"
    so_path = tmp_path / "libff_delta_hostile.so"
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
    return lib


def _run_hostile_delta_vectors(tmp_path: Path) -> None:
    lib = _compile_ff_delta(tmp_path)
    base = b"ABCDEFGHIJKLMNOP"  # 16 bytes
    base_digest = hashlib.sha256(base).digest()
    target = b"ABCDEF0123456789"
    target_digest = hashlib.sha256(target).digest()
    target_hex = target_digest.hex()

    def _hdr(
        *,
        magic: bytes = b"FFDLTA01",
        from_d: bytes = base_digest,
        to_d: bytes = target_digest,
        size: int = len(target),
    ) -> bytes:
        return magic + from_d + to_d + struct.pack(">I", size)

    # Shared hostile vectors covering M3a (OP_COPY OOB read / wraparound), M3b (short write),
    # bad magic, zero target size, wrong base hash, bad opcode, truncated ops, trailing bytes.
    vectors: list[tuple[str, bytes, int]] = [
        ("bad_magic", _hdr(magic=b"BADMAGIC") + b"\x00", -2),
        ("short_header", b"FFDLTA01" + b"\x00" * 10, -2),
        ("zero_target_size", _hdr(size=0) + b"\x00", -2),
        ("wrong_from_hash", _hdr(from_d=b"\x00" * 32) + b"\x00", -3),
        # M3a: src_off < base_len, but src_off + len > base_len (heap overread!)
        ("copy_overread_past_base", _hdr() + b"\x01" + struct.pack(">II", 12, 8) + b"\x00", -4),
        # src_off > base_len
        ("copy_src_off_past_base", _hdr() + b"\x01" + struct.pack(">II", 32, 4) + b"\x00", -4),
        # uint32 wraparound: src_off + len wraps to small int if unchecked
        ("copy_u32_wrap", _hdr() + b"\x01" + struct.pack(">II", 0xFFFFFFFC, 8) + b"\x00", -4),
        ("copy_zero_len", _hdr() + b"\x01" + struct.pack(">II", 0, 0) + b"\x00", -4),
        # OP_COPY writing past target_size
        ("copy_past_target", _hdr(size=4) + b"\x01" + struct.pack(">II", 0, 8) + b"\x00", -4),
        ("copy_truncated", _hdr() + b"\x01\x00\x00\x00", -6),
        ("insert_zero_len", _hdr() + b"\x02" + struct.pack(">I", 0) + b"\x00", -6),
        ("insert_truncated_hdr", _hdr() + b"\x02\x00\x00", -6),
        ("insert_truncated_payload", _hdr() + b"\x02" + struct.pack(">I", 10) + b"abc", -6),
        ("insert_past_target", _hdr(size=4) + b"\x02" + struct.pack(">I", 8) + b"12345678\x00", -4),
        ("unknown_opcode", _hdr() + b"\x99\x00", -5),
        ("missing_end_opcode", _hdr() + b"\x01" + struct.pack(">II", 0, 16), -6),
        (
            "trailing_bytes_after_end",
            _hdr() + b"\x01" + struct.pack(">II", 0, 16) + b"\x00\x99",
            -6,
        ),
        # M3b: valid opcodes + OP_END, but written (4) != hdr.target_size (16)
        (
            "short_written_vs_target_size",
            _hdr(size=16) + b"\x01" + struct.pack(">II", 0, 4) + b"\x00",
            -7,
        ),
    ]

    for name, patch_bytes, expected_c_rc in vectors:
        with pytest.raises(DeltaPatchError, match=r".+"):
            apply_delta_patch(base, patch_bytes, expected_to_sha256=target_hex)
        with pytest.raises(SimulatorError, match=r".+"):
            apply_ffdelta_v1(base, patch_bytes, expected_to_sha256=target_hex)
        out_buf = ctypes.create_string_buffer(64)
        written = ctypes.c_size_t(0)
        rc = lib.ff_delta_apply_in_memory(
            base,
            len(base),
            base_digest,
            patch_bytes,
            len(patch_bytes),
            out_buf,
            64,
            ctypes.byref(written),
        )
        assert rc == expected_c_rc, f"vector {name!r}: expected C rc {expected_c_rc}, got {rc}"

    # Verify stage-digest binding (`T-18` `delta_ota.py:132`): valid patch targeting `target` MUST fail
    # if `expected_to_sha256` names a different digest.
    good_patch = create_delta_patch(base, target)
    wrong_to_hex = "0" * 64
    with pytest.raises(DeltaPatchError, match="target digest disagrees"):
        apply_delta_patch(base, good_patch, expected_to_sha256=wrong_to_hex)
    with pytest.raises(SimulatorError, match="to_sha256"):
        apply_ffdelta_v1(base, good_patch, expected_to_sha256=wrong_to_hex)


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
                # A-35: `handle_up_message` emits once per envelope; `relay-sync` must not double-emit.
                from unittest.mock import patch

                from fleetforge.events import emit as real_emit

                emit_count = 0

                async def _counting_emit(session: Any, event: Any) -> None:
                    nonlocal emit_count
                    emit_count += 1
                    await real_emit(session, event)

                cmd_1 = publisher.published[0][1]["id"]
                cmd_2 = publisher.published[1][1]["id"]
                with patch("fleetforge.ingest.handlers.emit", side_effect=_counting_emit):
                    sync_resp = await client.post(
                        "/v1/devices/a4cf12b30001/relay-sync",
                        headers=headers,
                        json={
                            "cached_artifacts": gateway.cached_digests(),
                            "children": {
                                "a4cf12b30011": {
                                    "statuses": [{"cmd_id": cmd_1, "state": "confirmed"}],
                                    "telemetry": [
                                        {
                                            "samples": [
                                                {"name": "battery_v", "value": 15.8, "age_ms": 0}
                                            ]
                                        }
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
                assert emit_count == 3, f"expected 1 emit per envelope (3), got {emit_count}"
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

    async def test_offline_gateway_falls_back_to_the_direct_url(
        self,
        env: dict[str, Any],
    ) -> None:
        """A-30: when a parent gateway is offline, child deploys fall back to the direct URL;
        gateways can also authenticate `/relay` and `/relay-sync` via per-device HTTP Basic."""
        import base64
        import datetime as dt

        headers = {"Authorization": f"Bearer {env['token']}"}
        publisher: FakeCommandPublisher = env["publisher"]
        now = now_utc()
        gw_pw = "gw-mqtt-secret-pw"
        gw_pw_hash = hashlib.sha256(gw_pw.encode("utf-8")).hexdigest()
        other_pw_hash = hashlib.sha256(b"other-pw").hexdigest()

        async with env["sessionmaker"]() as s:
            grp = DeviceGroup(project_id=DEFAULT_PROJECT_ID, name="offline-gw-swarm")
            s.add(grp)
            await s.flush()
            ugv = Device(
                device_id="a4cf12b30301",
                name="ugv-offline",
                project_id=DEFAULT_PROJECT_ID,
                platform_type="esp32s3",
                fw_version="2.0.0",
                partition_layout="ab-4m-v1",
                ota_slot_size=1966080,
                rollback_capable=True,
                capabilities=["ota", "sig", "edge_relay"],
                link_type="wifi",
                power_class="always_on",
                presence_reported=False,
                last_seen=now - dt.timedelta(hours=2),
                mqtt_password_hash=gw_pw_hash,
            )
            child = Device(
                device_id="a4cf12b30311",
                name="child-direct-fallback",
                project_id=DEFAULT_PROJECT_ID,
                group_id=grp.id,
                parent_device_id=ugv.device_id,
                platform_type="esp32c6",
                fw_version="1.0.0",
                partition_layout="ab-4m-v1",
                ota_slot_size=1966080,
                rollback_capable=True,
                capabilities=["ota", "sig"],
                link_type="wifi",
                power_class="always_on",
                presence_reported=True,
                last_seen=now,
                mqtt_password_hash=other_pw_hash,
            )
            s.add_all([ugv, child])
            await s.commit()
            gid = grp.id

        gw_basic = base64.b64encode(f"a4cf12b30301:{gw_pw}".encode()).decode("ascii")
        other_basic = base64.b64encode(b"a4cf12b30311:other-pw").decode("ascii")
        bad_basic = base64.b64encode(b"a4cf12b30301:wrong-pw").decode("ascii")

        fw = _realistic_firmware("1.4.0")
        async with client_for(env["app"], base_url="https://testserver") as client:
            up = await client.post(
                "/v1/artifact",
                params={"target": "esp32c6", "version": "1.4.0", "partition_layout": "ab-4m-v1"},
                content=fw,
                headers={
                    **headers,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(fw)),
                },
            )
            assert up.status_code == 201

            # Bad Basic password -> 401; valid Basic creds for a DIFFERENT device -> 403.
            assert (
                await client.put(
                    "/v1/devices/a4cf12b30301/relay",
                    headers={"Authorization": f"Basic {bad_basic}"},
                    json={"relay_base_url": "http://10.20.30.40:8080", "cached_artifacts": []},
                )
            ).status_code == 401
            assert (
                await client.put(
                    "/v1/devices/a4cf12b30301/relay",
                    headers={"Authorization": f"Basic {other_basic}"},
                    json={"relay_base_url": "http://10.20.30.40:8080", "cached_artifacts": []},
                )
            ).status_code == 403

            # Gateway registers its relay using its own per-device Basic credential -> 200!
            reg = await client.put(
                "/v1/devices/a4cf12b30301/relay",
                headers={"Authorization": f"Basic {gw_basic}"},
                json={"relay_base_url": "http://10.20.30.40:8080", "cached_artifacts": []},
            )
            assert reg.status_code == 200, reg.text

            # Because parent gateway `a4cf12b30301` is OFFLINE, child deploy falls back to direct URL!
            roll = await client.post(
                "/v1/projects/default/deploy",
                headers=headers,
                json={"version": "1.4.0", "group_id": str(gid), "canary": False, "max_parallel": 5},
            )
            assert roll.status_code == 202, roll.text
            assert len(publisher.published) == 1
            staged_url = str(publisher.published[0][1]["artifact"]["url"])
            assert staged_url.startswith(env["settings"].public_base_url)
            assert not staged_url.startswith("http://10.20.30.40:8080")

            # Gateway can also authenticate `POST /relay-sync` via its own Basic credential.
            sync = await client.post(
                "/v1/devices/a4cf12b30301/relay-sync",
                headers={"Authorization": f"Basic {gw_basic}"},
                json={"children": {"a4cf12b30311": {"statuses": []}}},
            )
            assert sync.status_code == 200, sync.text

    async def test_airtime_cap_counts_in_flight_boards_of_other_versions(
        self,
        env: dict[str, Any],
    ) -> None:
        """A-30: a `lora` board actively downloading `1.2.0` occupies the link's airtime cap=1
        so rolling out `1.3.0` onto a second `lora` board waits instead of saturating the radio."""
        from fleetforge.deploys import DeploySender, record_observed_status, record_requested

        headers = {"Authorization": f"Bearer {env['token']}"}
        now = now_utc()
        async with env["sessionmaker"]() as s:
            lora_grp = DeviceGroup(project_id=DEFAULT_PROJECT_ID, name="lora-cross-ver")
            s.add(lora_grp)
            await s.flush()
            for idx in (1, 2):
                s.add(
                    Device(
                        device_id=f"a4cf12b3040{idx}",
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
            await s.flush()
            await record_requested(
                s,
                device_id="a4cf12b30401",
                cmd_id="cmd-older-ver",
                artifact_version="1.2.0",
                from_version="1.0.0",
                sha256="a" * 64,
                size_bytes=4096,
                target="esp32c6",
                apply="auto",
                sent_by=DeploySender("admin", "tok-1", None),
            )
            await record_observed_status(
                s,
                device_id="a4cf12b30401",
                cmd_id="cmd-older-ver",
                state="downloading",
                pct=40,
                at=now,
            )
            await s.commit()
            gid = lora_grp.id

        fw = _realistic_firmware("1.3.0")
        async with client_for(env["app"], base_url="https://testserver") as client:
            up = await client.post(
                "/v1/artifact",
                params={"target": "esp32c6", "version": "1.3.0", "partition_layout": "ab-4m-v1"},
                content=fw,
                headers={
                    **headers,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(fw)),
                },
            )
            assert up.status_code == 201
            roll = await client.post(
                "/v1/projects/default/deploy",
                headers=headers,
                json={"version": "1.3.0", "group_id": str(gid), "canary": False, "max_parallel": 5},
            )
            assert roll.status_code == 202, roll.text
            body = roll.json()
            assert body["deployed"] == []
            assert "airtime cap=1 on lora link" in body["skipped"]["a4cf12b30402"]
