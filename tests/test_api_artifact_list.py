"""`GET /v1/artifact` — the read half of the artifact store. R1-fe-1.

The load-bearing property is the first class below, and it is not about listing at all:
**this route is admin-only.** `api/routers/artifact_download.py` owns a router with the
same `/v1/artifact` prefix and no auth dependencies — public by design, because there a
signature over `(sha256, exp)` is the authorization. A list route added to that router
instead of this one would publish the firmware catalog to the internet and every test
about ordering would still pass. Hence the 401 assertions, including one carrying an
`exp`/`sig` query string: a download signature must buy nothing here.

The rest is the shape the picker needs: an envelope even when empty, newest first within
a target, and two labels over one digest rendered as two rows with one `sha256` — the
many-to-one that makes `artifact_versions` a table (`db/models.py::ArtifactVersion`).
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
from tests.conftest import MemoryObjectStore, client_for, login_admin

IMAGE = b"\xe9\x06\x02\x20" + b"firmware bytes, opaque to the server" * 37
OTHER_IMAGE = IMAGE + b"!"
SHA256 = hashlib.sha256(IMAGE).hexdigest()
OTHER_SHA256 = hashlib.sha256(OTHER_IMAGE).hexdigest()


@pytest.fixture
def store(admin_app: FastAPI) -> Iterator[MemoryObjectStore]:
    store = MemoryObjectStore()
    admin_app.dependency_overrides[get_object_store] = lambda: store
    yield store
    admin_app.dependency_overrides.pop(get_object_store, None)


@pytest.fixture(autouse=True)
async def empty_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    """Start and finish with no artifacts — these tests commit through the API.

    Labels first: the FK to `artifacts` is `RESTRICT`, which is the point of it.
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
    target: str = "esp32c6",
    version: str = "1.5.0",
) -> httpx.Response:
    async with client_for(app, base_url="https://testserver") as client:
        return await client.post(
            "/v1/artifact",
            params={"target": target, "version": version},
            content=data,
            headers={"authorization": f"Bearer {token}"},
        )


async def listing(app: FastAPI, token: str) -> Any:
    async with client_for(app) as client:
        response = await client.get("/v1/artifact", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    return response.json()


class TestAuth:
    async def test_unauthenticated_list_is_401(self, admin_app: FastAPI) -> None:
        """The whole reason the upload router and the download router are two files."""
        async with client_for(admin_app) as client:
            response = await client.get("/v1/artifact")
        assert response.status_code == 401

    async def test_a_download_signature_does_not_open_the_list(self, admin_app: FastAPI) -> None:
        """`?exp=&sig=` is the public sibling's credential. It is not a credential here."""
        async with client_for(admin_app) as client:
            response = await client.get("/v1/artifact", params={"exp": "9999999999", "sig": "AAAA"})
        assert response.status_code == 401


class TestTheListing:
    async def test_an_empty_store_is_an_envelope_not_a_404(self, admin_app: FastAPI) -> None:
        assert await listing(admin_app, await login_admin(admin_app)) == {"artifacts": []}

    async def test_a_row_carries_the_label_and_the_digest_metadata(
        self, admin_app: FastAPI, store: MemoryObjectStore
    ) -> None:
        """`size_bytes`/`partition_layout` come from `artifacts` and match the 201 body."""
        token = await login_admin(admin_app)
        uploaded = await upload(admin_app, token, IMAGE)
        assert uploaded.status_code == 201, uploaded.text

        rows = (await listing(admin_app, token))["artifacts"]

        assert len(rows) == 1
        assert rows[0]["target"] == "esp32c6"
        assert rows[0]["version"] == "1.5.0"
        assert rows[0]["sha256"] == SHA256
        assert rows[0]["size_bytes"] == uploaded.json()["size_bytes"] == len(IMAGE)
        assert rows[0]["partition_layout"] == uploaded.json()["partition_layout"]
        assert rows[0]["kind"] == "user_firmware"
        assert rows[0]["created_at"] is not None

    async def test_there_is_no_url_in_a_listed_artifact(
        self, admin_app: FastAPI, store: MemoryObjectStore
    ) -> None:
        """A download link is a bearer credential minted per deploy, never listed."""
        token = await login_admin(admin_app)
        await upload(admin_app, token, IMAGE)

        row = (await listing(admin_app, token))["artifacts"][0]

        assert set(row) == {
            "target",
            "version",
            "sha256",
            "size_bytes",
            "partition_layout",
            "kind",
            "created_at",
        }

    async def test_two_labels_over_one_digest_are_two_rows_with_one_sha256(
        self, admin_app: FastAPI, store: MemoryObjectStore
    ) -> None:
        """Re-tagging identical bytes is ordinary, and the picker must offer both."""
        token = await login_admin(admin_app)
        assert (await upload(admin_app, token, IMAGE, version="1.5.0")).status_code == 201
        assert (await upload(admin_app, token, IMAGE, version="1.5.0-rc1")).status_code == 201

        rows = (await listing(admin_app, token))["artifacts"]

        assert sorted(row["version"] for row in rows) == ["1.5.0", "1.5.0-rc1"]
        assert {row["sha256"] for row in rows} == {SHA256}
        assert {row["size_bytes"] for row in rows} == {len(IMAGE)}

    async def test_targets_are_grouped_and_newest_first_within_a_target(
        self, admin_app: FastAPI, store: MemoryObjectStore, engine: AsyncEngine
    ) -> None:
        """The order the picker renders: one chip's versions together, newest at the top."""
        token = await login_admin(admin_app)
        await upload(admin_app, token, IMAGE, target="esp32c6", version="1.5.0")
        await upload(admin_app, token, OTHER_IMAGE, target="esp32c6", version="1.6.0")
        await upload(admin_app, token, IMAGE, target="esp32", version="0.9.0")
        # `created_at` defaults to `now()` and these three land inside one tick, so the
        # ordering claim is only tested if the timestamps really differ.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE artifact_versions SET created_at = now() - make_interval(secs => 60)"
                    " WHERE version = '1.5.0'"
                )
            )

        rows = (await listing(admin_app, token))["artifacts"]

        assert [(row["target"], row["version"]) for row in rows] == [
            ("esp32", "0.9.0"),
            ("esp32c6", "1.6.0"),
            ("esp32c6", "1.5.0"),
        ]

    async def test_distinct_bytes_keep_distinct_sizes(
        self, admin_app: FastAPI, store: MemoryObjectStore
    ) -> None:
        token = await login_admin(admin_app)
        await upload(admin_app, token, IMAGE, version="1.5.0")
        await upload(admin_app, token, OTHER_IMAGE, version="1.6.0")

        sizes = {
            row["version"]: (row["sha256"], row["size_bytes"])
            for row in (await listing(admin_app, token))["artifacts"]
        }

        assert sizes == {
            "1.5.0": (SHA256, len(IMAGE)),
            "1.6.0": (OTHER_SHA256, len(OTHER_IMAGE)),
        }
