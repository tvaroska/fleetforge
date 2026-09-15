"""`GET /v1/agent/manifest` and `GET /v1/agent/{target}/{part}`.

What `R0-fe-3`'s `esptool-js` page calls. Two things it must never get wrong, because
both end in a board that flashes cleanly and does not boot:

* the **bytes** must be byte-identical to what was published (no encoding, no truncation),
* the **offsets** must be the per-chip ones from the build, not a normalised default.

Since S0-infra-6 the fixture is a real publish into an in-memory `ObjectStore` rather
than a directory: `get_object_store` is overridden, `firmware_catalog` is not, so every
test here exercises the index read, the manifest re-check and the blob fetch the
production path takes. The store fake is the one in `conftest.py`, which is also what
the publish and catalog suites use.
"""

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI

from fleetforge.api.deps import get_object_store
from fleetforge.api.routers.agent import NOTHING_PUBLISHED
from fleetforge.firmware import DEFAULT_INDEX_KEY, CatalogCache, load_bundle_dir
from fleetforge.firmware.publish import publish_bundle, read_index
from fleetforge.storage.blobs import blob_key
from fleetforge.storage.objectstore import ObjectStoreError
from tests.conftest import MemoryObjectStore, client_for, login_admin
from tests.test_firmware_catalog import PART_FILES, write_bundle


@pytest.fixture
def bundles(tmp_path: Path) -> Path:
    """Two targets whose bootloaders sit at different offsets, as real builds do."""
    write_bundle(tmp_path, "esp32")
    write_bundle(tmp_path, "esp32c6")
    return tmp_path


def use_store(app: FastAPI, store: MemoryObjectStore) -> None:
    """Point the app at `store` and give it a cache that has not read anything yet."""
    app.dependency_overrides[get_object_store] = lambda: store
    # A TTL of 0 keeps each request honest: the assertions below are about what the
    # store holds now, not about what some earlier test left in a snapshot.
    app.state.agent_catalog = CatalogCache(ttl_s=0.0)


@pytest.fixture
async def store(admin_app: FastAPI, bundles: Path) -> MemoryObjectStore:
    """Both targets published, exactly as `just agent-publish` would."""
    store = MemoryObjectStore()
    for target in ("esp32", "esp32c6"):
        await publish_bundle(store, load_bundle_dir(bundles / target))
    use_store(admin_app, store)
    return store


@pytest.fixture
def agent_app(admin_app: FastAPI, store: MemoryObjectStore) -> Iterator[FastAPI]:
    yield admin_app
    admin_app.dependency_overrides.pop(get_object_store, None)


@pytest.fixture
def empty_agent_app(admin_app: FastAPI) -> Iterator[FastAPI]:
    """A reachable store with nothing published — no index object at all."""
    use_store(admin_app, MemoryObjectStore())
    yield admin_app
    admin_app.dependency_overrides.pop(get_object_store, None)


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

    async def test_an_anonymous_request_never_touches_the_store(
        self, agent_app: FastAPI, store: MemoryObjectStore
    ) -> None:
        """The credential is checked before a byte is read: no unauthenticated egress."""
        store.gets.clear()
        async with client_for(agent_app) as client:
            await client.get("/v1/agent/esp32/app")
        assert store.gets == []


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
        # S0-infra-3: served, because the diagnostic bundle the flasher page builds is
        # downstream of this response and nothing else tells it which build it wrote.
        assert build["config_sha256"] == "c0" * 32
        assert build["build_digest"] == "b1" * 32
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

    async def test_the_manifest_carries_no_path_bucket_or_key(
        self, agent_app: FastAPI, tmp_path: Path
    ) -> None:
        """Neither the server's filesystem nor its object layout is the flasher's business.

        Leaking `blobs/sha256/…` here would invite the browser to build its own key, which
        is the one thing the "no key is ever built from a request" rule in the router rules
        out.
        """
        token = await login_admin(agent_app)
        async with client_for(agent_app) as client:
            response = await client.get(
                "/v1/agent/manifest", headers={"Authorization": f"Bearer {token}"}
            )
        assert "path" not in response.text
        assert str(tmp_path) not in response.text
        assert "blobs/" not in response.text


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
    async def test_a_hostile_part_never_reads_an_object(
        self, agent_app: FastAPI, store: MemoryObjectStore, hostile: str
    ) -> None:
        """404 or 422 — never a 200, never a 500. The lookup is a dict hit on names the
        published manifest produced, and the only key that reaches the store is
        `blob_key(<a digest that manifest carried>)`, so there is nothing to escape from;
        this is the assertion that keeps it that way."""
        token = await login_admin(agent_app)
        store.gets.clear()
        async with client_for(agent_app) as client:
            response = await client.get(
                f"/v1/agent/esp32/{hostile}", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code in (404, 422), response.text
        assert "root:" not in response.text
        assert "manifest" not in response.text.lower() or response.status_code == 422
        # Only the index and the two published manifests may have been read — no key
        # derived from the request ever reached the store.
        index = await read_index(store)
        allowed = {DEFAULT_INDEX_KEY} | {blob_key(e.manifest_sha256) for e in index.bundles}
        assert set(store.gets) <= allowed, store.gets

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


class TestPublishedWhileRunning:
    """The point of the whole task: a publish reaches the flasher with nothing restarted."""

    async def test_a_target_published_after_startup_appears(
        self, admin_app: FastAPI, tmp_path: Path
    ) -> None:
        store = MemoryObjectStore()
        use_store(admin_app, store)
        await publish_bundle(store, load_bundle_dir(write_bundle(tmp_path, "esp32")))
        token = await login_admin(admin_app)
        headers = {"Authorization": f"Bearer {token}"}

        async with client_for(admin_app) as client:
            first = await client.get("/v1/agent/manifest", headers=headers)
            assert [b["target"] for b in first.json()["builds"]] == ["esp32"]

            await publish_bundle(store, load_bundle_dir(write_bundle(tmp_path, "esp32c6")))
            second = await client.get("/v1/agent/manifest", headers=headers)

        assert [b["target"] for b in second.json()["builds"]] == ["esp32", "esp32c6"]
        admin_app.dependency_overrides.pop(get_object_store, None)


class TestStoreFaults:
    """Three different faults, three different answers, all in plain language.

    `api.ts::detailOf` lifts `detail` straight into the flasher's banner, so these
    assertions are also the copy review: no exception class, no bucket, no key.
    """

    async def test_an_unreachable_store_is_503_naming_the_store(
        self, agent_app: FastAPI, store: MemoryObjectStore
    ) -> None:
        token = await login_admin(agent_app)
        store.fail_with = ObjectStoreError("connection refused to minio:9000/ff-artifacts")
        async with client_for(agent_app) as client:
            response = await client.get(
                "/v1/agent/manifest", headers={"Authorization": f"Bearer {token}"}
            )

        assert response.status_code == 503
        detail = response.json()["detail"]
        assert "store" in detail
        assert "cannot be reached" in detail
        assert detail != NOTHING_PUBLISHED, "down and empty are different answers"
        for leak in ("minio", "ff-artifacts", "Traceback", "ObjectStoreError", "blobs/"):
            assert leak not in detail, detail

    async def test_an_unreachable_store_is_503_on_a_part_too(
        self, agent_app: FastAPI, store: MemoryObjectStore
    ) -> None:
        token = await login_admin(agent_app)
        store.fail_with = ObjectStoreError("connection refused")
        async with client_for(agent_app) as client:
            response = await client.get(
                "/v1/agent/esp32/app", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 503
        assert "cannot be reached" in response.json()["detail"]

    async def test_a_missing_part_blob_is_503_naming_the_part(
        self, agent_app: FastAPI, store: MemoryObjectStore
    ) -> None:
        """A publish-integrity bug, not a transport blip — and it must say which part."""
        token = await login_admin(agent_app)
        headers = {"Authorization": f"Bearer {token}"}
        async with client_for(agent_app) as client:
            # Locate the blob through the API's own view of the catalog, so the test
            # cannot pass by deleting something the router never asks for.
            manifest = await client.get("/v1/agent/manifest", headers=headers)
            build = next(b for b in manifest.json()["builds"] if b["target"] == "esp32")
            app_key = blob_key(next(p["sha256"] for p in build["parts"] if p["name"] == "app"))
            await store.delete(app_key)

            response = await client.get("/v1/agent/esp32/app", headers=headers)

        assert response.status_code == 503
        detail = response.json()["detail"]
        assert "app" in detail and "esp32" in detail
        assert app_key not in detail, "the key is for the log line, not the banner"

    async def test_tampered_bytes_are_502_and_never_reach_the_board(
        self, agent_app: FastAPI, store: MemoryObjectStore
    ) -> None:
        """Content addressing makes this impossible, which is why it is worth one hash:
        the failure that happens in the field is a truncated read, and without this check
        the symptom is a board that flashes cleanly and never boots."""
        token = await login_admin(agent_app)
        headers = {"Authorization": f"Bearer {token}"}
        async with client_for(agent_app) as client:
            manifest = await client.get("/v1/agent/manifest", headers=headers)
            build = next(b for b in manifest.json()["builds"] if b["target"] == "esp32")
            app_key = blob_key(next(p["sha256"] for p in build["parts"] if p["name"] == "app"))
            store.objects[app_key] = store.objects[app_key][:-1]

            response = await client.get("/v1/agent/esp32/app", headers=headers)

        assert response.status_code == 502
        assert response.content != store.objects[app_key], "truncated bytes must not be served"
        assert "do not match the manifest" in response.json()["detail"]


class TestNothingPublished:
    """A reachable store with no index: the answer is "publish something", not "look at
    the network". Reachable-but-empty is now a real production state — the image ships
    no bundles at all — so this is the first thing a fresh deployment shows."""

    async def test_manifest_is_503(self, empty_agent_app: FastAPI) -> None:
        token = await login_admin(empty_agent_app)
        async with client_for(empty_agent_app) as client:
            response = await client.get(
                "/v1/agent/manifest", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 503
        assert response.json()["detail"] == NOTHING_PUBLISHED
        assert "agent-publish" in NOTHING_PUBLISHED, "the message must say what to do"

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
