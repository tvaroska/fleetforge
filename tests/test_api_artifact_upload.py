"""`POST /v1/artifact` — the upload endpoint. R1-be-1.

The properties worth holding, each one a class below:

* **The digest is computed from the bytes.** Nothing a client says about identity is
  trusted, so the blob can never land at a key that lies about its contents.
* **Nothing reaches the store until it is acceptable.** The oversize tests assert on
  `store.puts` being empty, not merely on the status code — "rejected" and "rejected
  before it cost anything" are different promises and only the second is the one the
  endpoint makes.
* **Two success cases, kept apart.** Re-uploading identical bytes under the same label
  is 200 and idempotent; a new label is 201. Re-pointing a label at *different* bytes
  is 409, and the old label survives it.
* **Labels are many-to-one over blobs.** Two versions of byte-identical firmware are
  two rows and one object.
"""

import hashlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fleetforge.api.deps import get_object_store
from fleetforge.firmware.manifest import EXPECTED_OTA_SLOT_SIZE
from fleetforge.storage.blobs import blob_key
from fleetforge.storage.objectstore import ObjectStoreError
from tests.conftest import MemoryObjectStore, client_for, login_admin

# Small, but not a round number — a length that could be confused with an offset or a
# chunk boundary makes a size assertion prove less than it looks like it does.
IMAGE = b"\xe9\x06\x02\x20" + b"firmware bytes, opaque to the server" * 37
OTHER_IMAGE = IMAGE + b"!"


@pytest.fixture
def store(admin_app: FastAPI) -> Iterator[MemoryObjectStore]:
    store = MemoryObjectStore()
    admin_app.dependency_overrides[get_object_store] = lambda: store
    yield store
    admin_app.dependency_overrides.pop(get_object_store, None)


@pytest.fixture(autouse=True)
async def empty_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    """Start and finish with no artifacts.

    The `engine` fixture is session-scoped and these tests commit through the API, so
    unlike the `session` fixture there is no transaction to roll back. Cleaning both
    before and after means a row left by a crashed test cannot make the next one pass
    or fail for the wrong reason. Labels first: the FK is `RESTRICT`, which is the
    point of it.
    """
    await truncate(engine)
    yield
    await truncate(engine)


async def truncate(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM artifact_versions"))
        await conn.execute(text("DELETE FROM artifacts"))


async def upload(
    app: FastAPI,
    token: str,
    data: bytes,
    *,
    target: str = "esp32",
    version: str = "1.5.0",
    layout: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST the raw body the way R1-fe-1 will."""
    params: dict[str, str] = {"target": target, "version": version}
    if layout is not None:
        params["partition_layout"] = layout
    async with client_for(app, base_url="https://testserver") as client:
        return await client.post(
            "/v1/artifact",
            params=params,
            content=data,
            headers={"authorization": f"Bearer {token}", **(headers or {})},
        )


async def rows(engine: AsyncEngine, sql: str) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        return [tuple(row) for row in (await conn.execute(text(sql))).all()]


class TestAuth:
    async def test_unauthenticated_upload_is_401(
        self, admin_app: FastAPI, store: MemoryObjectStore
    ) -> None:
        async with client_for(admin_app) as client:
            response = await client.post(
                "/v1/artifact", params={"target": "esp32", "version": "1.0.0"}, content=IMAGE
            )
        assert response.status_code == 401
        assert store.puts == [], "an unauthenticated upload must not reach the store"


class TestStoresTheBytes:
    async def test_blob_lands_at_its_own_digest_and_the_row_describes_it(
        self, admin_app: FastAPI, store: MemoryObjectStore, engine: AsyncEngine
    ) -> None:
        token = await login_admin(admin_app)
        response = await upload(admin_app, token, IMAGE)

        assert response.status_code == 201, response.text
        digest = hashlib.sha256(IMAGE).hexdigest()
        body = response.json()
        assert body == {
            "sha256": digest,
            "size_bytes": len(IMAGE),
            "target": "esp32",
            "version": "1.5.0",
            "partition_layout": "ab-4m-v1",
            "created": True,
        }
        # The key is a function of the bytes, not of anything the client sent.
        assert store.objects[blob_key(digest)] == IMAGE
        assert await rows(engine, "SELECT sha256, size_bytes, kind, target FROM artifacts") == [
            (digest, len(IMAGE), "user_firmware", "esp32")
        ]
        assert await rows(engine, "SELECT target, version, sha256 FROM artifact_versions") == [
            ("esp32", "1.5.0", digest)
        ]

    async def test_the_blob_is_written_immutable(
        self, admin_app: FastAPI, store: MemoryObjectStore
    ) -> None:
        """`put_blob` owns the cache header; this is the guard that it is still used."""
        token = await login_admin(admin_app)
        await upload(admin_app, token, IMAGE)
        (_key, _data, _content_type, cache_control) = store.puts[0]
        assert cache_control == "public, max-age=31536000, immutable"

    async def test_an_unreachable_store_is_503_with_no_bucket_in_the_message(
        self, admin_app: FastAPI, store: MemoryObjectStore
    ) -> None:
        store.fail_with = ObjectStoreError("s3://private-bucket/secret: connection refused")
        token = await login_admin(admin_app)
        response = await upload(admin_app, token, IMAGE)
        assert response.status_code == 503
        assert "bucket" not in response.json()["detail"]
        assert "refused" not in response.json()["detail"]


class TestIdempotenceAndConflict:
    async def test_the_same_bytes_under_the_same_label_twice_is_200_and_one_row(
        self, admin_app: FastAPI, store: MemoryObjectStore, engine: AsyncEngine
    ) -> None:
        token = await login_admin(admin_app)
        first = await upload(admin_app, token, IMAGE)
        second = await upload(admin_app, token, IMAGE)

        assert first.status_code == 201
        assert first.json()["created"] is True
        assert second.status_code == 200, second.text
        assert second.json()["created"] is False
        assert second.json()["sha256"] == first.json()["sha256"]
        assert len(store.objects) == 1
        assert len(await rows(engine, "SELECT 1 FROM artifact_versions")) == 1

    async def test_two_labels_for_identical_bytes_are_two_rows_and_one_object(
        self, admin_app: FastAPI, store: MemoryObjectStore, engine: AsyncEngine
    ) -> None:
        """The reason `version` is a table and not a column on a digest-keyed row."""
        token = await login_admin(admin_app)
        assert (await upload(admin_app, token, IMAGE, version="1.5.0")).status_code == 201
        assert (await upload(admin_app, token, IMAGE, version="1.5.1")).status_code == 201

        digest = hashlib.sha256(IMAGE).hexdigest()
        assert len(store.objects) == 1
        assert await rows(
            engine, "SELECT version, sha256 FROM artifact_versions ORDER BY version"
        ) == [("1.5.0", digest), ("1.5.1", digest)]

    async def test_the_same_label_for_different_bytes_is_409_and_the_label_is_unchanged(
        self, admin_app: FastAPI, store: MemoryObjectStore, engine: AsyncEngine
    ) -> None:
        token = await login_admin(admin_app)
        await upload(admin_app, token, IMAGE, version="1.5.0")
        response = await upload(admin_app, token, OTHER_IMAGE, version="1.5.0")

        assert response.status_code == 409
        original = hashlib.sha256(IMAGE).hexdigest()
        assert await rows(engine, "SELECT sha256 FROM artifact_versions") == [(original,)]
        # Refused before the upload: the pre-check exists so a conflict costs no transfer.
        assert len(store.objects) == 1

    async def test_the_same_label_on_a_different_target_is_a_different_artifact(
        self, admin_app: FastAPI, store: MemoryObjectStore, engine: AsyncEngine
    ) -> None:
        """`(target, version)` is the key — two chips may both ship a "1.5.0"."""
        token = await login_admin(admin_app)
        assert (await upload(admin_app, token, IMAGE, target="esp32")).status_code == 201
        assert (await upload(admin_app, token, OTHER_IMAGE, target="esp32s3")).status_code == 201
        assert len(await rows(engine, "SELECT 1 FROM artifact_versions")) == 2


class TestRefusedBeforeItCosts:
    async def test_an_oversize_declaration_is_413_and_nothing_is_stored(
        self, admin_app: FastAPI, store: MemoryObjectStore
    ) -> None:
        token = await login_admin(admin_app)
        too_big = b"\x00" * (EXPECTED_OTA_SLOT_SIZE + 1)
        response = await upload(admin_app, token, too_big)

        assert response.status_code == 413
        assert str(EXPECTED_OTA_SLOT_SIZE) in response.json()["detail"]
        assert store.puts == [], "an oversize upload must never reach the store"

    async def test_exactly_the_slot_size_fits(
        self, admin_app: FastAPI, store: MemoryObjectStore
    ) -> None:
        """The limit is the slot, so an image that exactly fills it is legal."""
        token = await login_admin(admin_app)
        response = await upload(admin_app, token, b"\x00" * EXPECTED_OTA_SLOT_SIZE)
        assert response.status_code == 201, response.text

    async def test_a_lying_content_length_is_still_refused(
        self, admin_app: FastAPI, store: MemoryObjectStore
    ) -> None:
        """The declared length is a courtesy; the read cap is the guarantee."""
        token = await login_admin(admin_app)
        response = await upload(
            admin_app,
            token,
            b"\x00" * (EXPECTED_OTA_SLOT_SIZE + 1),
            headers={"content-length": "10"},
        )
        assert response.status_code in (400, 413)
        assert store.puts == []

    async def test_an_empty_body_is_400_not_a_constraint_violation(
        self, admin_app: FastAPI, store: MemoryObjectStore
    ) -> None:
        token = await login_admin(admin_app)
        response = await upload(admin_app, token, b"")
        assert response.status_code == 400
        assert "empty" in response.json()["detail"]
        assert store.puts == []

    async def test_an_unknown_partition_layout_is_400_naming_what_exists(
        self, admin_app: FastAPI, store: MemoryObjectStore
    ) -> None:
        token = await login_admin(admin_app)
        response = await upload(admin_app, token, IMAGE, layout="ab-16m-v9")
        assert response.status_code == 400
        assert "ab-4m-v1" in response.json()["detail"]
        assert store.puts == []


class TestLabelsAreRejectedNeverNormalised:
    @pytest.mark.parametrize(
        "version",
        [
            "1.5.0 ",  # a trailing space: the second spelling this rule exists to stop
            " 1.5.0",
            "1.5.0\n",
            "-leading-dash",
            "with/slash",
            "with space",
            "",
            "v" * 65,
        ],
    )
    async def test_a_hostile_or_ambiguous_version_is_refused(
        self, admin_app: FastAPI, store: MemoryObjectStore, version: str
    ) -> None:
        token = await login_admin(admin_app)
        response = await upload(admin_app, token, IMAGE, version=version)
        assert response.status_code in (400, 422), response.text
        assert store.puts == []

    @pytest.mark.parametrize("version", ["1.5.0", "2026.09.16", "v1.5.0-rc.1+build.7", "ci_4821"])
    async def test_the_shapes_a_user_actually_uses_are_accepted(
        self, admin_app: FastAPI, store: MemoryObjectStore, version: str
    ) -> None:
        """The vocabulary is the user's: semver, a date, a CI number all pass."""
        token = await login_admin(admin_app)
        response = await upload(admin_app, token, IMAGE, version=version)
        assert response.status_code == 201, response.text
