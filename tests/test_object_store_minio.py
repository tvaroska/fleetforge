"""The real S3 round trip, against the dev stack's MinIO.

`tests/test_object_store.py` proves the wire shape with a `Stubber`; this proves the
thing the stub cannot — that a presigned URL is actually redeemable, and that it
actually stops being redeemable. Signing is precisely the part that only fails against
a real backend.

Skips loudly (never silently) when MinIO is not up: `just minio-up`.
"""

import contextlib
import socket
import time
import urllib.error
import urllib.request
import uuid

import pytest

from fleetforge.config import Settings
from fleetforge.storage import (
    BLOB_CACHE_CONTROL,
    ObjectNotFound,
    digest_bytes,
    parse_blob_key,
    put_blob,
)
from fleetforge.storage.factory import create_object_store
from tests.conftest import settings_for_tests

MINIO_HOST = "localhost"
MINIO_PORT = 9000
ENDPOINT = f"http://{MINIO_HOST}:{MINIO_PORT}"
BUCKET = "fleetforge"


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


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=15) as response:  # noqa: S310 - http URL from the adapter
        return bytes(response.read())


def _fetch_headers(url: str) -> dict[str, str]:
    """The response headers a device would get. `Cache-Control` is the one that matters."""
    with urllib.request.urlopen(url, timeout=15) as response:  # noqa: S310 - http URL from the adapter
        return {name.lower(): value for name, value in response.headers.items()}


async def test_minio_round_trip() -> None:
    """put → get → signed_url (fetched over HTTP) → delete → ObjectNotFound → delete again."""
    store = create_object_store(_settings())
    key = f"pytest/{uuid.uuid4().hex}.bin"
    payload = b"fleetforge-artifact-" + uuid.uuid4().bytes

    try:
        await store.put(key, payload)
        assert await store.get(key) == payload

        url = await store.signed_url(key, ttl_s=300)
        assert _fetch(url) == payload

        await store.delete(key)
        with pytest.raises(ObjectNotFound):
            await store.get(key)
        # Idempotent by contract — the retry path must not fail.
        await store.delete(key)
    finally:
        with contextlib.suppress(Exception):  # noqa: BLE001 - best-effort cleanup only
            await store.delete(key)


async def test_minio_signed_url_expires() -> None:
    """A 2 s URL is redeemable now and refused after it lapses. The whole authorization."""
    store = create_object_store(_settings())
    key = f"pytest/{uuid.uuid4().hex}.bin"
    payload = b"expiring"

    try:
        await store.put(key, payload)
        url = await store.signed_url(key, ttl_s=2)
        assert _fetch(url) == payload

        time.sleep(3)
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _fetch(url)
        assert excinfo.value.code == 403
    finally:
        with contextlib.suppress(Exception):  # noqa: BLE001 - best-effort cleanup only
            await store.delete(key)


async def test_minio_blob_round_trip_serves_the_immutable_cache_control() -> None:
    """The S0-infra-4 scheme against a real backend, checked from the device's side.

    `tests/test_object_store.py` asserts the adapter *sends* `CacheControl`; only a real
    object can show that it is stored and served back. `s3_prefix=""` here, so the key
    resolves to `blobs/sha256/<hex>` in the dedicated `fleetforge` bucket — the dev row
    of the table in `storage/blobs.py`.
    """
    store = create_object_store(_settings())
    payload = b"fleetforge-blob-" + uuid.uuid4().bytes
    digest = digest_bytes(payload)
    key = ""

    try:
        key = await put_blob(store, payload)
        assert key == f"blobs/sha256/{digest}"
        assert parse_blob_key(key) == digest
        assert await store.get(key) == payload

        url = await store.signed_url(key, ttl_s=300)
        headers = _fetch_headers(url)
        assert headers["cache-control"] == BLOB_CACHE_CONTROL
        assert _fetch(url) == payload

        # Write-once means a retried upload is a no-op, not a conflict — the property
        # `objectstore.py::put` claims. Prove it against the real backend.
        assert await put_blob(store, payload) == key
        assert await store.get(key) == payload
    finally:
        if key:
            with contextlib.suppress(Exception):  # noqa: BLE001 - best-effort cleanup only
                await store.delete(key)


async def test_minio_rejects_an_escaping_key_before_reaching_the_backend() -> None:
    """The confinement holds against the real client too, not only the stub."""
    from fleetforge.storage import ObjectKeyError

    store = create_object_store(_settings())
    for key in ("../escape.bin", "/abs.bin", "a/../../b.bin"):
        with pytest.raises(ObjectKeyError):
            await store.put(key, b"x")
