"""The download endpoint's range behaviour, for real, against the dev stack's MinIO. R1-be-3.

`tests/test_api_artifact_download.py` proves the *authorization* with a fake store. It
cannot prove the property the whole redirect design rests on: that a `Range:` request
survives the 307 and comes back as a correct 206 from the object store, with 416 for an
unsatisfiable range — because we deliberately do not implement RFC 7233 ourselves, this
is the only place that behaviour is observable at all.

`esp_https_ota` resumes with `Range:` on a flaky link (R5), so a broken slice here is a
board that flashes corrupt firmware and A/B-rolls back. Worth the integration cost.

The redirect is followed by hand with `urllib` rather than by an httpx transport: the
first hop is the ASGI app and the second is a real socket to MinIO, which no single
client can do. That is also exactly what `simulator/device.py::_fetch` does.

Skips loudly (never silently) when MinIO is not up: `just minio-up`.
"""

import contextlib
import socket
import urllib.error
import urllib.request

import pytest
from fastapi import FastAPI

from fleetforge.api.deps import get_settings
from fleetforge.api.main import create_app
from fleetforge.artifact_urls import mint_artifact_url
from fleetforge.config import Settings
from fleetforge.storage import digest_bytes, put_blob
from fleetforge.storage.factory import create_object_store
from fleetforge.storage.urlcache import SignedUrlCache
from tests.conftest import (
    TEST_ARTIFACT_URL_SECRET,
    TEST_PUBLIC_BASE_URL,
    client_for,
    settings_for_tests,
)

MINIO_HOST = "localhost"
MINIO_PORT = 9000
ENDPOINT = f"http://{MINIO_HOST}:{MINIO_PORT}"
BUCKET = "fleetforge"

# 200 KB, the size the T2 transcript uses: big enough that a range is a real range, and
# a round number so `Content-Range: …/204800` is readable in a failure message.
SIZE = 204_800


def _minio_reachable() -> bool:
    """A TCP connect, not an HTTP request: this decides whether to skip, nothing more."""
    try:
        with socket.create_connection((MINIO_HOST, MINIO_PORT), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _minio_reachable(),
    reason=f"MinIO unreachable at {ENDPOINT}; start it with `just minio-up`",
)


def _settings() -> Settings:
    """Dev-stack MinIO. The credentials are the obviously-fake ones from `.env.example`."""
    return settings_for_tests(
        s3_endpoint_url=ENDPOINT,
        s3_public_endpoint_url=ENDPOINT,
        s3_bucket=BUCKET,
        s3_access_key="fleetforge",
        s3_secret_key="fleetforge-dev-only",
        s3_prefix="",
        object_store_timeout_s=15.0,
    )


def _app(settings: Settings) -> FastAPI:
    """`create_app()` wired to MinIO, with no database override — none is needed."""
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.artifact_url_cache = SignedUrlCache(ttl_s=300, refresh_margin_s=30)
    return app


async def _location(app: FastAPI, digest: str, **kwargs: object) -> str:
    """The upstream URL the endpoint redirects a device to, for a freshly minted link."""
    url = mint_artifact_url(
        TEST_PUBLIC_BASE_URL, digest, secret=TEST_ARTIFACT_URL_SECRET, ttl_s=300
    )
    async with client_for(app) as client:
        response = await client.get(url[len(TEST_PUBLIC_BASE_URL) :], **kwargs)  # type: ignore[arg-type]
    assert response.status_code == 307, response.text
    return response.headers["location"]


def _fetch(url: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    """`(status, lowercased headers, body)`, the way a device's HTTP client sees it."""
    request = urllib.request.Request(url, headers=headers or {})  # noqa: S310 - http URL from the adapter
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - same
        return (
            response.status,
            {name.lower(): value for name, value in response.headers.items()},
            bytes(response.read()),
        )


@pytest.fixture
async def artifact() -> tuple[str, bytes]:
    """A 200 KB blob in MinIO, and its bytes. Removed afterwards.

    Deterministic content rather than `os.urandom`: a failing slice assertion should
    point at an offset, not at a random diff nobody can reproduce.
    """
    payload = bytes((i * 7 + 13) % 256 for i in range(SIZE))
    store = create_object_store(_settings())
    key = await put_blob(store, payload)
    try:
        yield digest_bytes(payload), payload
    finally:
        with contextlib.suppress(Exception):  # noqa: BLE001 - best-effort cleanup only
            await store.delete(key)


async def test_a_signed_link_serves_the_whole_artifact(artifact: tuple[str, bytes]) -> None:
    """The end-to-end shape: 307 from us, 200 from the store, byte-identical firmware."""
    digest, payload = artifact
    app = _app(_settings())

    status, headers, body = _fetch(await _location(app, digest))

    assert status == 200
    assert body == payload
    assert headers["content-length"] == str(SIZE)


async def test_a_byte_range_comes_back_as_a_correct_206(artifact: tuple[str, bytes]) -> None:
    """The property the redirect exists for: ranges are the store's, and they work."""
    digest, payload = artifact
    app = _app(_settings())
    location = await _location(app, digest, headers={"Range": "bytes=1000-1099"})

    status, headers, body = _fetch(location, {"Range": "bytes=1000-1099"})

    assert status == 206
    assert headers["content-range"] == f"bytes 1000-1099/{SIZE}"
    assert body == payload[1000:1100]


async def test_a_suffix_range_comes_back_as_the_tail(artifact: tuple[str, bytes]) -> None:
    """`bytes=-64`: the last 64 bytes, not the first 64. A hand-rolled parser's classic."""
    digest, payload = artifact
    app = _app(_settings())

    status, headers, body = _fetch(await _location(app, digest), {"Range": "bytes=-64"})

    assert status == 206
    assert headers["content-range"] == f"bytes {SIZE - 64}-{SIZE - 1}/{SIZE}"
    assert body == payload[-64:]


async def test_an_open_ended_range_resumes_to_the_end(artifact: tuple[str, bytes]) -> None:
    """`bytes=N-` is what `esp_https_ota` sends to resume an interrupted flash."""
    digest, payload = artifact
    app = _app(_settings())

    status, headers, body = _fetch(await _location(app, digest), {"Range": "bytes=200000-"})

    assert status == 206
    assert headers["content-range"] == f"bytes 200000-{SIZE - 1}/{SIZE}"
    assert body == payload[200_000:]


async def test_an_unsatisfiable_range_is_416(artifact: tuple[str, bytes]) -> None:
    """Past the end of the object: 416, and a device that learns to stop asking."""
    digest, _ = artifact
    app = _app(_settings())

    with pytest.raises(urllib.error.HTTPError) as caught:
        _fetch(await _location(app, digest), {"Range": "bytes=999999999-"})

    assert caught.value.code == 416


async def test_a_signed_link_for_an_absent_object_ends_as_the_store_s_404(
    artifact: tuple[str, bytes],
) -> None:
    """The endpoint deliberately does not check existence; this is what that looks like.

    A validly signed digest with nothing behind it is a 307 followed by the store's own
    404 — no database query, no existence oracle, and an honest answer to the device.
    """
    app = _app(_settings())
    never_uploaded = "4" * 64

    with pytest.raises(urllib.error.HTTPError) as caught:
        _fetch(await _location(app, never_uploaded))

    assert caught.value.code == 404
