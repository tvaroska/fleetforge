"""`GET /v1/agent/manifest` and `GET /v1/agent/{target}/{part}`.

What `R0-fe-3`'s `esptool-js` page will call. Two things it must never get wrong, because
both end in a board that flashes cleanly and does not boot:

* the **bytes** must be byte-identical to the file on disk (no encoding, no truncation),
* the **offsets** must be the per-chip ones from the build, not a normalised default.

The catalog is pointed at a tmp fixture through `dependency_overrides` on
`firmware_catalog`, so no test here needs a toolchain or a real `agent/dist`.
"""

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI

from fleetforge.api.routers.agent import firmware_catalog
from fleetforge.firmware import FirmwareCatalog
from tests.conftest import client_for, login_admin
from tests.test_firmware_catalog import PART_FILES, write_bundle


@pytest.fixture
def bundles(tmp_path: Path) -> Path:
    """Two targets whose bootloaders sit at different offsets, as real builds do."""
    write_bundle(tmp_path, "esp32")
    write_bundle(tmp_path, "esp32c6")
    return tmp_path


@pytest.fixture
def agent_app(admin_app: FastAPI, bundles: Path) -> Iterator[FastAPI]:
    catalog = FirmwareCatalog.load(bundles)
    assert catalog.targets == ("esp32", "esp32c6"), "fixture bundles must load"
    admin_app.dependency_overrides[firmware_catalog] = lambda: catalog
    yield admin_app
    admin_app.dependency_overrides.pop(firmware_catalog, None)


@pytest.fixture
def empty_agent_app(admin_app: FastAPI, tmp_path: Path) -> Iterator[FastAPI]:
    empty = FirmwareCatalog.load(tmp_path / "no-bundles-here")
    admin_app.dependency_overrides[firmware_catalog] = lambda: empty
    yield admin_app
    admin_app.dependency_overrides.pop(firmware_catalog, None)


class TestAuth:
    """Nothing here is secret; the credential is about bandwidth and blast radius."""

    async def test_manifest_unauthenticated_is_401(self, agent_app: FastAPI) -> None:
        async with client_for(agent_app) as client:
            response = await client.get("/v1/agent/manifest")
        assert response.status_code == 401

    async def test_download_unauthenticated_is_401(self, agent_app: FastAPI) -> None:
        async with client_for(agent_app) as client:
            response = await client.get("/v1/agent/esp32/app")
        assert response.status_code == 401

    async def test_a_bad_bearer_is_401(self, agent_app: FastAPI) -> None:
        async with client_for(agent_app) as client:
            response = await client.get(
                "/v1/agent/manifest", headers={"Authorization": "Bearer ffa_nope"}
            )
        assert response.status_code == 401

    async def test_401_before_404(self, agent_app: FastAPI) -> None:
        """An anonymous prober must not be able to enumerate targets by status code."""
        async with client_for(agent_app) as client:
            response = await client.get("/v1/agent/nosuchchip/app")
        assert response.status_code == 401


class TestManifest:
    async def test_shape(self, agent_app: FastAPI) -> None:
        token = await login_admin(agent_app)
        async with client_for(agent_app) as client:
            response = await client.get(
                "/v1/agent/manifest", headers={"Authorization": f"Bearer {token}"}
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["agent_version"] == "0.1.0"
        assert [build["target"] for build in body["builds"]] == ["esp32", "esp32c6"]

        build = body["builds"][0]
        assert build["chip_family"] == "ESP32"
        assert build["idf_version"] == "v5.5.5"
        assert build["partition_layout"] == "ab-4m-v1"
        assert build["ota_slot_size"] == 1966080
        assert build["flash_size"] == "4MB"
        assert [part["name"] for part in build["parts"]] == [
            "bootloader",
            "partition-table",
            "ota-data",
            "app",
        ]

    async def test_offsets_are_integers_not_hex_strings(self, agent_app: FastAPI) -> None:
        """`esptool-js` wants a number. A "0x1000" here is a runtime error in the browser
        at the worst possible moment — with a board already in bootloader mode."""
        token = await login_admin(agent_app)
        async with client_for(agent_app) as client:
            response = await client.get(
                "/v1/agent/manifest", headers={"Authorization": f"Bearer {token}"}
            )
        for build in response.json()["builds"]:
            for part in build["parts"]:
                assert isinstance(part["offset"], int), part
                assert isinstance(part["size"], int), part
                assert not isinstance(part["offset"], bool)

    async def test_the_per_chip_bootloader_offset_reaches_the_client(
        self, agent_app: FastAPI
    ) -> None:
        token = await login_admin(agent_app)
        async with client_for(agent_app) as client:
            response = await client.get(
                "/v1/agent/manifest", headers={"Authorization": f"Bearer {token}"}
            )
        offsets = {
            build["target"]: {part["name"]: part["offset"] for part in build["parts"]}
            for build in response.json()["builds"]
        }
        assert offsets["esp32"]["bootloader"] == 4096
        assert offsets["esp32c6"]["bootloader"] == 0

    async def test_the_manifest_carries_no_local_path(
        self, agent_app: FastAPI, tmp_path: Path
    ) -> None:
        """The filesystem layout of the server is not the flasher's business."""
        token = await login_admin(agent_app)
        async with client_for(agent_app) as client:
            response = await client.get(
                "/v1/agent/manifest", headers={"Authorization": f"Bearer {token}"}
            )
        assert "path" not in response.text
        assert str(tmp_path) not in response.text


class TestDownload:
    @pytest.mark.parametrize("part", ["bootloader", "partition-table", "ota-data", "app"])
    async def test_every_part_is_byte_exact(
        self, agent_app: FastAPI, bundles: Path, part: str
    ) -> None:
        token = await login_admin(agent_app)
        async with client_for(agent_app) as client:
            response = await client.get(
                f"/v1/agent/esp32/{part}", headers={"Authorization": f"Bearer {token}"}
            )

        assert response.status_code == 200, response.text
        expected = (bundles / "esp32" / PART_FILES[part]).read_bytes()
        assert response.content == expected
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["etag"] == f'"sha256-{hashlib.sha256(expected).hexdigest()}"'
        assert response.headers["cache-control"] == "private, max-age=3600"

    async def test_the_two_targets_do_not_serve_each_others_bytes(
        self, agent_app: FastAPI, bundles: Path
    ) -> None:
        token = await login_admin(agent_app)
        headers = {"Authorization": f"Bearer {token}"}
        async with client_for(agent_app) as client:
            esp32 = await client.get("/v1/agent/esp32/app", headers=headers)
            esp32c6 = await client.get("/v1/agent/esp32c6/app", headers=headers)
        assert esp32.content != esp32c6.content
        assert esp32c6.content == (bundles / "esp32c6" / "app.bin").read_bytes()

    async def test_the_download_filename_names_target_and_part(self, agent_app: FastAPI) -> None:
        token = await login_admin(agent_app)
        async with client_for(agent_app) as client:
            response = await client.get(
                "/v1/agent/esp32c6/partition-table",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert "esp32c6-partition-table.bin" in response.headers["content-disposition"]

    async def test_unknown_target_is_404(self, agent_app: FastAPI) -> None:
        token = await login_admin(agent_app)
        async with client_for(agent_app) as client:
            response = await client.get(
                "/v1/agent/esp32h2/app", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 404

    async def test_unknown_part_is_404(self, agent_app: FastAPI) -> None:
        token = await login_admin(agent_app)
        async with client_for(agent_app) as client:
            response = await client.get(
                "/v1/agent/esp32/sdkconfig", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 404

    @pytest.mark.parametrize(
        "hostile",
        [
            "%2e%2e%2fmanifest.json",
            "..%2Fmanifest.json",
            "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
            "app.bin",
            "APP",
            "app%00",
        ],
    )
    async def test_a_hostile_part_never_reads_a_file(
        self, agent_app: FastAPI, hostile: str
    ) -> None:
        """404 or 422 — never a 200, never a 500. The lookup is a dict hit on names the
        loader produced, so there is no filesystem call to escape from in the first
        place; this is the assertion that keeps it that way."""
        token = await login_admin(agent_app)
        async with client_for(agent_app) as client:
            response = await client.get(
                f"/v1/agent/esp32/{hostile}", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code in (404, 422), response.text
        assert "root:" not in response.text
        assert "manifest" not in response.text.lower() or response.status_code == 422

    @pytest.mark.parametrize("hostile", ["..", "%2e%2e", "esp32%2f..%2fesp32c6"])
    async def test_a_hostile_target_never_reads_a_file(
        self, agent_app: FastAPI, hostile: str
    ) -> None:
        token = await login_admin(agent_app)
        async with client_for(agent_app) as client:
            response = await client.get(
                f"/v1/agent/{hostile}/app", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code in (404, 422), response.text


class TestEmptyCatalog:
    """`just build` refuses to ship an app image with an empty `agent/dist`, so in
    production this means a broken bind mount — 503 is the honest answer."""

    async def test_manifest_is_503(self, empty_agent_app: FastAPI) -> None:
        token = await login_admin(empty_agent_app)
        async with client_for(empty_agent_app) as client:
            response = await client.get(
                "/v1/agent/manifest", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 503
        assert response.json()["detail"] == "no agent images available"

    async def test_download_is_503(self, empty_agent_app: FastAPI) -> None:
        async with client_for(empty_agent_app) as client:
            response = await client.get(
                "/v1/agent/esp32/app",
                headers={"Authorization": f"Bearer {await login_admin(empty_agent_app)}"},
            )
        assert response.status_code == 503

    async def test_still_401_before_503(self, empty_agent_app: FastAPI) -> None:
        async with client_for(empty_agent_app) as client:
            response = await client.get("/v1/agent/manifest")
        assert response.status_code == 401
