"""Parameterized `@pytest.mark.port` conformance suite for `ObjectStore` (`T-8` / `T-30`).

Runs the exact same behavioral contract against:
- `MemoryObjectStore` (the unit-test fake in `tests/conftest.py`),
- `S3ObjectStore` (backed by a stateful in-memory S3 client),
- `GcsObjectStore` (backed by a stateful in-memory GCS bucket), and
- live `S3ObjectStore` against MinIO when `FF_RUN_MINIO_TESTS=1`.

Any contract drift between the test fake and the real adapters fails here immediately.
"""

from __future__ import annotations

import contextlib
import io
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from botocore.exceptions import ClientError
from google.api_core import exceptions as gcs_exceptions

from fleetforge.storage.factory import create_object_store
from fleetforge.storage.gcs import GcsObjectStore
from fleetforge.storage.objectstore import (
    MAX_KEY_LEN,
    SIGNED_URL_MAX_TTL_S,
    ObjectKeyError,
    ObjectNotFound,
    ObjectStore,
    ObjectStoreError,
    ObjectTooLarge,
)
from fleetforge.storage.s3 import S3ObjectStore
from tests.conftest import MemoryObjectStore, settings_for_tests

pytestmark = pytest.mark.port

MINIO_HOST = os.environ.get("MINIO_HOST", "localhost")
MINIO_PORT = int(os.environ.get("MINIO_PORT", "9000"))
MINIO_ENDPOINT = f"http://{MINIO_HOST}:{MINIO_PORT}"
MINIO_BUCKET = os.environ.get("S3_BUCKET", "fleetforge")

CONTRACT_MAX_GET_BYTES = 64

INVALID_KEYS = (
    "",
    "/abs.bin",
    "../escape.bin",
    "a/../../b.bin",
    "a/./b.bin",
    "a//b.bin",
    "a\\b.bin",
    "a?sig=1",
    "a#frag",
    "a\nb.bin",
    "é.bin",
    "x" * (MAX_KEY_LEN + 1),
)


class _StatefulS3Client:
    """Minimal stateful S3/botocore-shaped double for offline `S3ObjectStore` contract runs."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str = "application/octet-stream",
        CacheControl: str | None = None,
    ) -> dict[str, Any]:
        del ContentType, CacheControl
        self.objects[(Bucket, Key)] = bytes(Body)
        return {}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        data = self.objects.get((Bucket, Key))
        if data is None:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}},
                "HeadObject",
            )
        return {"ContentLength": len(data)}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        data = self.objects.get((Bucket, Key))
        if data is None:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "NoSuchKey"}},
                "GetObject",
            )
        return {"Body": io.BytesIO(data)}

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self.objects.pop((Bucket, Key), None)
        return {}

    def generate_presigned_url(
        self,
        operation: str,
        *,
        Params: dict[str, str],
        ExpiresIn: int,
    ) -> str:
        assert operation == "get_object"
        return (
            f"https://s3-stub.invalid/{Params['Bucket']}/{Params['Key']}?X-Amz-Expires={ExpiresIn}"
        )


class _StatefulGcsBlob:
    def __init__(self, bucket: _StatefulGcsBucket, name: str) -> None:
        self._bucket = bucket
        self.name = name
        self.size: int | None = None
        self.cache_control: str | None = None

    def upload_from_string(self, data: bytes, content_type: str = "") -> None:
        del content_type
        self._bucket.objects[self.name] = bytes(data)

    def reload(self) -> None:
        data = self._bucket.objects.get(self.name)
        if data is None:
            raise gcs_exceptions.NotFound(self.name)
        self.size = len(data)

    def download_as_bytes(self) -> bytes:
        data = self._bucket.objects.get(self.name)
        if data is None:
            raise gcs_exceptions.NotFound(self.name)
        return data

    def delete(self) -> None:
        if self.name not in self._bucket.objects:
            raise gcs_exceptions.NotFound(self.name)
        del self._bucket.objects[self.name]

    def generate_signed_url(self, **kwargs: Any) -> str:
        exp = kwargs.get("expiration")
        return f"https://gcs-fake.invalid/{self._bucket.name}/{self.name}?exp={exp}"


class _StatefulGcsBucket:
    def __init__(self, name: str = "contract-bucket") -> None:
        self.name = name
        self.objects: dict[str, bytes] = {}

    def blob(self, name: str) -> _StatefulGcsBlob:
        return _StatefulGcsBlob(self, name)


@dataclass
class _ContractStoreHandle:
    store: ObjectStore
    prefix: str
    created_keys: list[str] = field(default_factory=list)

    def track(self, key: str) -> str:
        self.created_keys.append(key)
        return key


@pytest.fixture(params=["memory", "s3_stub", "gcs_fake", "minio"])
async def object_store_handle(
    request: pytest.FixtureRequest,
) -> AsyncIterator[_ContractStoreHandle]:
    kind = str(request.param)
    prefix = "fleetforge/"
    if kind == "memory":
        store: ObjectStore = MemoryObjectStore(
            prefix=prefix,
            default_ttl_s=300,
            max_get_bytes=CONTRACT_MAX_GET_BYTES,
        )
    elif kind == "s3_stub":
        client = _StatefulS3Client()
        store = S3ObjectStore(
            lambda: client,
            lambda: client,
            bucket="contract-bucket",
            prefix=prefix,
            default_ttl_s=300,
            max_get_bytes=CONTRACT_MAX_GET_BYTES,
            timeout_s=5.0,
        )
    elif kind == "gcs_fake":
        bucket = _StatefulGcsBucket()
        store = GcsObjectStore(
            lambda: bucket,
            bucket=bucket.name,
            prefix=prefix,
            default_ttl_s=300,
            max_get_bytes=CONTRACT_MAX_GET_BYTES,
            timeout_s=5.0,
        )
    elif kind == "minio":
        if os.environ.get("FF_RUN_MINIO_TESTS") != "1":
            pytest.skip(f"MinIO tests opt-in via FF_RUN_MINIO_TESTS=1 (target {MINIO_ENDPOINT})")
        prefix = "fleetforge-contract/"
        settings = settings_for_tests(
            s3_endpoint_url=MINIO_ENDPOINT,
            s3_public_endpoint_url=MINIO_ENDPOINT,
            s3_bucket=MINIO_BUCKET,
            s3_access_key=os.environ.get("S3_ACCESS_KEY", "fleetforge"),
            s3_secret_key=os.environ.get("S3_SECRET_KEY", "fleetforge-dev-only"),
            s3_prefix=prefix,
            signed_url_ttl_s=300,
            object_get_max_bytes=CONTRACT_MAX_GET_BYTES,
            object_store_timeout_s=15.0,
        )
        store = create_object_store(settings)
    else:
        raise AssertionError(f"unexpected backend {kind!r}")

    handle = _ContractStoreHandle(store=store, prefix=prefix)
    try:
        yield handle
    finally:
        for key in handle.created_keys:
            with contextlib.suppress(Exception):  # noqa: BLE001 - best-effort test cleanup
                await store.delete(key)


async def test_put_get_overwrite_and_idempotent_delete(
    object_store_handle: _ContractStoreHandle,
) -> None:
    store = object_store_handle.store
    key = object_store_handle.track(f"contract/{uuid.uuid4().hex}.bin")

    await store.put(key, b"v1-bytes", cache_control="public, max-age=60")
    assert await store.get(key) == b"v1-bytes"

    await store.put(key, b"v2-overwritten")
    assert await store.get(key) == b"v2-overwritten"

    await store.delete(key)
    with pytest.raises(ObjectNotFound):
        await store.get(key)

    # Deleting a missing key must succeed idempotently on every adapter.
    await store.delete(key)


async def test_get_missing_raises_object_not_found(
    object_store_handle: _ContractStoreHandle,
) -> None:
    with pytest.raises(ObjectNotFound):
        await object_store_handle.store.get(f"missing/{uuid.uuid4().hex}.bin")


async def test_resolve_key_rejects_invalid_keys_across_all_verbs(
    object_store_handle: _ContractStoreHandle,
) -> None:
    store = object_store_handle.store
    for bad_key in INVALID_KEYS:
        with pytest.raises(ObjectKeyError):
            await store.put(bad_key, b"x")
        with pytest.raises(ObjectKeyError):
            await store.get(bad_key)
        with pytest.raises(ObjectKeyError):
            await store.signed_url(bad_key)
        with pytest.raises(ObjectKeyError):
            await store.delete(bad_key)


async def test_signed_url_enforces_validate_ttl(
    object_store_handle: _ContractStoreHandle,
) -> None:
    store = object_store_handle.store
    key = f"signed/{uuid.uuid4().hex}.bin"

    default_url = await store.signed_url(key)
    explicit_url = await store.signed_url(key, ttl_s=60)
    assert default_url.startswith(("http://", "https://"))
    assert explicit_url.startswith(("http://", "https://"))
    assert f"{object_store_handle.prefix}{key}" in default_url
    assert f"{object_store_handle.prefix}{key}" in explicit_url

    for bad_ttl in (-1, 0, SIGNED_URL_MAX_TTL_S + 1):
        with pytest.raises(ValueError):
            await store.signed_url(key, ttl_s=bad_ttl)


@pytest.mark.parametrize("kind", ["memory", "s3_stub", "gcs_fake"])
async def test_backend_transport_failure_raises_object_store_error(kind: str) -> None:
    """Every adapter maps backend failures into `ObjectStoreError` (never raw SDK errors)."""
    if kind == "memory":
        mem = MemoryObjectStore(prefix="fleetforge/")
        mem.fail_with = ObjectStoreError("memory backend down")
        store: ObjectStore = mem
    elif kind == "s3_stub":

        class _BoomS3:
            def put_object(self, **kwargs: Any) -> Any:
                raise OSError("s3 down")

        store = S3ObjectStore(
            _BoomS3,
            _BoomS3,
            bucket="contract-bucket",
            prefix="fleetforge/",
            default_ttl_s=300,
            max_get_bytes=CONTRACT_MAX_GET_BYTES,
            timeout_s=5.0,
        )
    else:

        class _BoomGcs:
            def blob(self, name: str) -> Any:
                return self

            def upload_from_string(self, data: bytes, content_type: str = "") -> None:
                raise gcs_exceptions.ServiceUnavailable("gcs down")

        store = GcsObjectStore(
            _BoomGcs,
            bucket="contract-bucket",
            prefix="fleetforge/",
            default_ttl_s=300,
            max_get_bytes=CONTRACT_MAX_GET_BYTES,
            timeout_s=5.0,
        )

    with pytest.raises(ObjectStoreError):
        await store.put("ok.bin", b"bytes")


async def test_get_enforces_object_too_large(
    object_store_handle: _ContractStoreHandle,
) -> None:
    store = object_store_handle.store
    at_limit_key = object_store_handle.track(f"sized/{uuid.uuid4().hex}-ok.bin")
    over_limit_key = object_store_handle.track(f"sized/{uuid.uuid4().hex}-big.bin")

    await store.put(at_limit_key, b"a" * CONTRACT_MAX_GET_BYTES)
    assert await store.get(at_limit_key) == b"a" * CONTRACT_MAX_GET_BYTES

    await store.put(over_limit_key, b"b" * (CONTRACT_MAX_GET_BYTES + 1))
    with pytest.raises(ObjectTooLarge):
        await store.get(over_limit_key)
