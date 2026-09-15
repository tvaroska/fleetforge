"""Object-store seam: key confinement, backend selection, and both adapters.

**No network, no database.** `S3ObjectStore` is driven with `botocore.stub.Stubber` and
`GcsObjectStore` with a duck-typed fake bucket — which is the entire reason both take
client factories rather than clients. The real round trip lives in
`tests/test_object_store_minio.py`.

The presigned-URL host assertion below is the regression guard for the trap that a
working test cannot otherwise catch: a URL signed against the container-internal
endpoint works from inside the compose network and fails on every real device.
"""

import datetime as dt
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber
from fastapi import FastAPI, HTTPException
from google.api_core import exceptions as gcs_exceptions
from google.auth import exceptions as auth_exceptions

from fleetforge.api.deps import ObjectStoreDep, get_object_store
from fleetforge.config import Settings, get_settings
from fleetforge.storage import (
    BLOB_CACHE_CONTROL,
    MAX_KEY_LEN,
    SIGNED_URL_MAX_TTL_S,
    ObjectKeyError,
    ObjectNotFound,
    ObjectStoreConfigError,
    ObjectStoreError,
    ObjectTooLarge,
    blob_key,
    digest_bytes,
    put_blob,
    resolve_key,
    validate_prefix,
)
from fleetforge.storage.factory import (
    build_s3_config,
    create_object_store,
    object_store_configured,
    select_backend,
)
from fleetforge.storage.gcs import GcsObjectStore
from fleetforge.storage.s3 import S3ObjectStore
from tests.conftest import capture_logs, client_for, settings_for_tests

PREFIX = "fleetforge/"
BUCKET = "artifacts"
SECRET_KEY = "s3-secret-nobody-should-ever-log"  # noqa: S105 - a fake, and that is the point
# The prefix-scoped production principal (S0-infra-5). An EMAIL, never a credential.
TARGET_SA = "fleetforge-artifacts@btvaroska.iam.gserviceaccount.com"


# ---------------------------------------------------------------------------
# resolve_key — the security control. Escaping the prefix must be impossible.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "a.bin",
        "artifacts/esp32/1.5.0.bin",
        "a/b/c-d_e.bin",
        "selftest/0123456789abcdef.bin",
        "x" * MAX_KEY_LEN,
    ],
)
def test_resolve_key_accepts_relative_keys(key: str) -> None:
    assert resolve_key(PREFIX, key) == f"{PREFIX}{key}"


@pytest.mark.parametrize(
    ("key", "why"),
    [
        ("", "empty"),
        ("/abs", "leading slash"),
        ("a/../../b", "parent segment"),
        ("../x", "parent segment"),
        ("..", "bare parent"),
        (".", "bare current"),
        ("a/./b", "current segment"),
        ("a//b", "empty segment"),
        ("a\\b", "backslash"),
        ("a\nb", "control character"),
        ("a\x00b", "NUL"),
        ("a\x7fb", "DEL"),
        ("a?b", "query separator"),
        ("a#b", "fragment separator"),
        ("é.bin", "non-ASCII"),
        ("x" * 600, "too long"),
    ],
)
def test_resolve_key_rejects(key: str, why: str) -> None:
    """Every one of these raises — none of them ever returns a string."""
    with pytest.raises(ObjectKeyError):
        resolve_key(PREFIX, key)


@pytest.mark.parametrize(
    "key",
    ["", "/abs", "a/../../b", "../x", "..", ".", "a/./b", "a//b", "a\\b", "a?b", "é.bin"],
)
def test_resolve_key_never_returns_outside_the_prefix(key: str) -> None:
    """The property, stated as a property: a bad key produces no key at all."""
    try:
        resolved = resolve_key(PREFIX, key)
    except ObjectKeyError:
        return
    pytest.fail(f"{key!r} was accepted and resolved to {resolved!r}")


def test_resolve_key_with_no_prefix_still_rejects_escapes() -> None:
    """A dedicated bucket (`s3_prefix=""`) gets the same rules, not weaker ones."""
    with pytest.raises(ObjectKeyError):
        resolve_key("", "../escape.bin")
    assert resolve_key("", "a.bin") == "a.bin"


@pytest.mark.parametrize("prefix", ["", "fleetforge/", "a/b/"])
def test_validate_prefix_accepts(prefix: str) -> None:
    assert validate_prefix(prefix) == prefix


@pytest.mark.parametrize(
    "prefix", ["fleetforge", "/fleetforge/", "../", "a//b/", "a\\b/", "flé/", "a?b/"]
)
def test_validate_prefix_rejects(prefix: str) -> None:
    with pytest.raises(ObjectKeyError):
        validate_prefix(prefix)


def test_constructor_rejects_a_bad_prefix() -> None:
    """A misconfigured GCS_PREFIX fails at construction, not next to `secrets/`."""
    with pytest.raises(ObjectKeyError):
        GcsObjectStore(
            lambda: None,
            bucket=BUCKET,
            prefix="fleetforge",  # no trailing slash
            default_ttl_s=60,
            max_get_bytes=1024,
            timeout_s=1.0,
        )


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def _settings(**overrides: object) -> Settings:
    return settings_for_tests(**overrides)


def test_select_backend_infers_s3() -> None:
    settings = _settings(s3_endpoint_url="http://minio:9000", s3_bucket=BUCKET)
    assert select_backend(settings) == "s3"
    assert object_store_configured(settings)


def test_select_backend_infers_gcs() -> None:
    settings = _settings(gcs_bucket="btvaroska")
    assert select_backend(settings) == "gcs"
    assert object_store_configured(settings)


def test_select_backend_refuses_both() -> None:
    """Never a precedence rule: "which bucket did my firmware go to?" must be answerable."""
    settings = _settings(
        s3_endpoint_url="http://minio:9000", s3_bucket=BUCKET, gcs_bucket="btvaroska"
    )
    assert not object_store_configured(settings)
    with pytest.raises(ObjectStoreConfigError, match="both"):
        select_backend(settings)


def test_select_backend_refuses_neither() -> None:
    settings = _settings()
    assert not object_store_configured(settings)
    with pytest.raises(ObjectStoreConfigError, match="no object-store backend"):
        select_backend(settings)


def test_explicit_backend_with_incomplete_settings_raises() -> None:
    with pytest.raises(ObjectStoreConfigError, match="OBJECT_STORE_BACKEND=s3"):
        select_backend(_settings(object_store_backend="s3", gcs_bucket="btvaroska"))
    with pytest.raises(ObjectStoreConfigError, match="OBJECT_STORE_BACKEND=gcs"):
        select_backend(
            _settings(
                object_store_backend="gcs", s3_endpoint_url="http://minio:9000", s3_bucket=BUCKET
            )
        )


def test_explicit_backend_disambiguates_both() -> None:
    settings = _settings(
        object_store_backend="gcs",
        s3_endpoint_url="http://minio:9000",
        s3_bucket=BUCKET,
        gcs_bucket="btvaroska",
    )
    assert select_backend(settings) == "gcs"
    assert object_store_configured(settings)


def test_empty_strings_count_as_unset() -> None:
    """Compose interpolation of an unset variable yields "", not absence."""
    settings = _settings(s3_endpoint_url="", s3_bucket="", gcs_bucket="")
    assert not object_store_configured(settings)


def test_gcs_with_neither_credential_raises_rather_than_falling_back_to_adc() -> None:
    """Plain ADC in prod is the estate's shared VM account and can read `secrets/`."""
    with pytest.raises(ObjectStoreConfigError, match="GCS_CREDENTIALS_FILE is required") as excinfo:
        create_object_store(_settings(gcs_bucket="btvaroska"))
    message = str(excinfo.value)
    assert "Application Default Credentials" in message
    assert "GCS_IMPERSONATE_SERVICE_ACCOUNT" in message


def test_gcs_empty_impersonation_string_counts_as_unset() -> None:
    """Compose interpolation of an unset variable yields "", not absence."""
    settings = _settings(gcs_bucket="btvaroska", gcs_impersonate_service_account="   ")
    with pytest.raises(ObjectStoreConfigError, match="GCS_CREDENTIALS_FILE is required"):
        create_object_store(settings)


def test_gcs_refuses_a_key_file_and_impersonation_together() -> None:
    """Ambiguous CREDENTIALS are refused for the same reason ambiguous backends are."""
    settings = _settings(
        gcs_bucket="btvaroska",
        gcs_credentials_file="/dev/null",
        gcs_impersonate_service_account=TARGET_SA,
    )
    with pytest.raises(ObjectStoreConfigError, match="mutually exclusive"):
        create_object_store(settings)


@pytest.mark.parametrize(
    "principal",
    [
        "boris@gmail.com",
        "fleetforge-artifacts",
        "a@b.com",
        "FLEETFORGE-ARTIFACTS@btvaroska.iam.gserviceaccount.com",
    ],
)
def test_gcs_impersonation_rejects_a_principal_that_is_not_a_service_account(
    principal: str,
) -> None:
    """Rejected, never normalised — uppercase included. `resolve_key`'s standing rule."""
    settings = _settings(gcs_bucket="btvaroska", gcs_impersonate_service_account=principal)
    with pytest.raises(ObjectStoreConfigError, match="not a service-account email"):
        create_object_store(settings)


def test_gcs_with_a_missing_key_file_raises() -> None:
    settings = _settings(gcs_bucket="btvaroska", gcs_credentials_file="/nonexistent/key.json")
    with pytest.raises(ObjectStoreConfigError, match="does not exist"):
        create_object_store(settings)


def test_s3_without_credentials_raises() -> None:
    settings = _settings(s3_endpoint_url="http://minio:9000", s3_bucket=BUCKET)
    with pytest.raises(ObjectStoreConfigError, match="S3_ACCESS_KEY"):
        create_object_store(settings)


# ---------------------------------------------------------------------------
# S3ObjectStore, driven with botocore's Stubber — no MinIO required.
# ---------------------------------------------------------------------------


def _s3_client(endpoint_url: str = "http://minio:9000") -> Any:
    return boto3.session.Session().client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id="fleetforge",
        aws_secret_access_key=SECRET_KEY,
        region_name="us-east-1",
        config=build_s3_config(),
    )


def _s3_store(client: Any, presign_client: Any | None = None) -> S3ObjectStore:
    return S3ObjectStore(
        lambda: client,
        lambda: presign_client or client,
        bucket=BUCKET,
        prefix=PREFIX,
        default_ttl_s=1800,
        max_get_bytes=1024,
        timeout_s=5.0,
    )


async def test_s3_put_sends_the_prefixed_key_and_content_type() -> None:
    client = _s3_client()
    store = _s3_store(client)
    with Stubber(client) as stub:
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": BUCKET,
                "Key": f"{PREFIX}esp32/a.bin",
                "Body": b"firmware",
                "ContentType": "application/octet-stream",
            },
        )
        await store.put("esp32/a.bin", b"firmware")
        stub.assert_no_pending_responses()


async def test_s3_put_without_cache_control_sends_no_cache_control_at_all() -> None:
    """`Stubber` matches `expected_params` exactly, so an extra `CacheControl` fails here.

    Stated as its own test because the temptation is to always pass `CacheControl=...`:
    an empty string is not the same object metadata as no header, and it would change
    the request every existing caller makes.
    """
    client = _s3_client()
    store = _s3_store(client)
    with Stubber(client) as stub:
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": BUCKET,
                "Key": f"{PREFIX}plain.bin",
                "Body": b"x",
                "ContentType": "application/octet-stream",
            },
        )
        await store.put("plain.bin", b"x")
        stub.assert_no_pending_responses()


async def test_s3_put_blob_sends_the_content_addressed_key_and_immutable_cache_control() -> None:
    """The whole S0-infra-4 wire shape, in one assertion: the key and the header."""
    payload = b"firmware"
    digest = digest_bytes(payload)
    client = _s3_client()
    store = _s3_store(client)
    with Stubber(client) as stub:
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": BUCKET,
                # The store's prefix, then the store-relative blob key — never
                # `fleetforge/fleetforge/`.
                "Key": f"{PREFIX}blobs/sha256/{digest}",
                "Body": payload,
                "ContentType": "application/octet-stream",
                "CacheControl": BLOB_CACHE_CONTROL,
            },
        )
        assert await put_blob(store, payload) == blob_key(digest)
        stub.assert_no_pending_responses()


async def test_s3_get_returns_the_bytes() -> None:
    import io

    client = _s3_client()
    store = _s3_store(client)
    with Stubber(client) as stub:
        stub.add_response(
            "head_object", {"ContentLength": 8}, {"Bucket": BUCKET, "Key": f"{PREFIX}a.bin"}
        )
        stub.add_response(
            "get_object",
            {"Body": io.BytesIO(b"firmware")},
            {"Bucket": BUCKET, "Key": f"{PREFIX}a.bin"},
        )
        assert await store.get("a.bin") == b"firmware"
        stub.assert_no_pending_responses()


async def test_s3_get_of_a_missing_key_raises_object_not_found() -> None:
    client = _s3_client()
    store = _s3_store(client)
    with Stubber(client) as stub:
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
        with pytest.raises(ObjectNotFound):
            await store.get("missing.bin")


async def test_s3_get_refuses_an_oversized_object_before_downloading() -> None:
    """The cap is enforced from metadata: no `get_object` is stubbed, so a download fails the test."""
    client = _s3_client()
    store = _s3_store(client)
    with Stubber(client) as stub:
        stub.add_response(
            "head_object", {"ContentLength": 1025}, {"Bucket": BUCKET, "Key": f"{PREFIX}big.bin"}
        )
        with pytest.raises(ObjectTooLarge):
            await store.get("big.bin")
        stub.assert_no_pending_responses()


async def test_s3_delete_of_a_missing_key_is_silent() -> None:
    """S3 answers 204 for a key that is not there, so idempotency is free — assert it."""
    client = _s3_client()
    store = _s3_store(client)
    with Stubber(client) as stub:
        stub.add_response("delete_object", {}, {"Bucket": BUCKET, "Key": f"{PREFIX}gone.bin"})
        await store.delete("gone.bin")
        stub.assert_no_pending_responses()


async def test_s3_transport_failure_becomes_object_store_error() -> None:
    """A botocore exception escaping into a handler is a 500 where a 503 belongs."""

    class Exploding:
        def put_object(self, **kwargs: object) -> None:
            raise EndpointConnectionError(endpoint_url="http://minio:9000")

    store = S3ObjectStore(
        Exploding,
        Exploding,
        bucket=BUCKET,
        prefix=PREFIX,
        default_ttl_s=60,
        max_get_bytes=1024,
        timeout_s=5.0,
    )
    with pytest.raises(ObjectStoreError, match="s3 put"):
        await store.put("a.bin", b"x")


async def test_s3_client_error_that_is_not_404_becomes_object_store_error() -> None:
    client = _s3_client()
    store = _s3_store(client)
    with Stubber(client) as stub:
        stub.add_client_error("put_object", service_error_code="AccessDenied", http_status_code=403)
        with pytest.raises(ObjectStoreError, match="AccessDenied"):
            await store.put("a.bin", b"x")


async def test_s3_missing_bucket_is_a_config_failure_not_a_missing_object() -> None:
    """`NoSuchBucket` must NOT become `ObjectNotFound`.

    A misconfigured `S3_BUCKET` would otherwise answer 404 for every key in the store,
    sending the operator to look for the firmware instead of for the bucket name. It is
    a 503 ("the backend is wrong"), and `storage/s3.NOT_FOUND_CODES` says so.
    """
    client = _s3_client()
    store = _s3_store(client)
    with Stubber(client) as stub:
        stub.add_client_error(
            "head_object", service_error_code="NoSuchBucket", http_status_code=404
        )
        with pytest.raises(ObjectStoreError, match="NoSuchBucket") as caught:
            await store.get("a.bin")
    assert not isinstance(caught.value, ObjectNotFound)


async def test_s3_verbs_reject_an_escaping_key_before_any_call() -> None:
    """No stub is armed: a call reaching the client would raise `StubAssertionError`."""
    client = _s3_client()
    store = _s3_store(client)
    with Stubber(client):
        for key in ("../escape.bin", "/abs.bin", "a/../../b.bin"):
            with pytest.raises(ObjectKeyError):
                await store.put(key, b"x")
            with pytest.raises(ObjectKeyError):
                await store.get(key)
            with pytest.raises(ObjectKeyError):
                await store.delete(key)
            with pytest.raises(ObjectKeyError):
                await store.signed_url(key)


# --- Presigned URLs: the Host-signing trap ---------------------------------


async def test_signed_url_is_generated_against_the_public_endpoint() -> None:
    """The regression guard for storage/s3.py property 1.

    A URL signed against `http://minio:9000` works from inside the compose network and
    fails on every device outside it, so no test that only exercises the adapter would
    catch it. Assert the host.
    """
    store = _s3_store(_s3_client("http://minio:9000"), _s3_client("http://localhost:9000"))
    url = await store.signed_url("esp32/a.bin", ttl_s=300)
    parsed = urlparse(url)
    assert parsed.netloc == "localhost:9000"
    # Path-style addressing: `/<bucket>/<prefix><key>`, never `<bucket>.host`.
    assert parsed.path == f"/{BUCKET}/{PREFIX}esp32/a.bin"
    query = parse_qs(parsed.query)
    assert query["X-Amz-Expires"] == ["300"]
    assert "X-Amz-Signature" in query
    # The secret signs the URL; it never appears in it.
    assert SECRET_KEY not in url


async def test_signed_url_defaults_to_the_configured_ttl() -> None:
    store = _s3_store(_s3_client())
    url = await store.signed_url("a.bin")
    assert parse_qs(urlparse(url).query)["X-Amz-Expires"] == ["1800"]


async def test_signed_url_refuses_a_ttl_over_the_backend_maximum() -> None:
    """Better a `ValueError` here than a URL the device gets a 400 from."""
    store = _s3_store(_s3_client())
    with pytest.raises(ValueError, match="exceeds"):
        await store.signed_url("a.bin", ttl_s=SIGNED_URL_MAX_TTL_S + 1)
    with pytest.raises(ValueError, match="positive"):
        await store.signed_url("a.bin", ttl_s=0)


# ---------------------------------------------------------------------------
# GcsObjectStore, driven with a duck-typed fake bucket.
# ---------------------------------------------------------------------------


class FakeBlob:
    """The handful of `google.cloud.storage.Blob` members the adapter touches."""

    def __init__(self, bucket: "FakeBucket", name: str) -> None:
        self._bucket = bucket
        self.name = name
        self.size: int | None = None
        # Real `Blob.cache_control` is an assignable property sent with the upload; the
        # adapter sets it before `upload_from_string`, so record what it was at upload.
        self.cache_control: str | None = None
        self.signed_url_calls: list[dict[str, Any]] = []

    def upload_from_string(self, data: bytes, content_type: str = "") -> None:
        self._bucket.objects[self.name] = (data, content_type)
        self._bucket.cache_control_at_upload[self.name] = self.cache_control

    def reload(self) -> None:
        stored = self._bucket.objects.get(self.name)
        if stored is None:
            raise gcs_exceptions.NotFound(self.name)
        self.size = self._bucket.reported_size or len(stored[0])

    def download_as_bytes(self) -> bytes:
        stored = self._bucket.objects.get(self.name)
        if stored is None:
            raise gcs_exceptions.NotFound(self.name)
        return stored[0]

    def delete(self) -> None:
        if self.name not in self._bucket.objects:
            raise gcs_exceptions.NotFound(self.name)
        del self._bucket.objects[self.name]

    def generate_signed_url(self, **kwargs: Any) -> str:
        self._bucket.signed_url_calls.append({"name": self.name, **kwargs})
        return f"https://storage.googleapis.com/{self._bucket.name}/{self.name}?X-Goog-Signature=x"


class FakeBucket:
    def __init__(self, name: str = "btvaroska") -> None:
        self.name = name
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.cache_control_at_upload: dict[str, str | None] = {}
        self.signed_url_calls: list[dict[str, Any]] = []
        self.reported_size: int | None = None

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)


def _gcs_store(bucket: FakeBucket, *, max_get_bytes: int = 1024) -> GcsObjectStore:
    return GcsObjectStore(
        lambda: bucket,
        bucket=bucket.name,
        prefix=PREFIX,
        default_ttl_s=1800,
        max_get_bytes=max_get_bytes,
        timeout_s=5.0,
    )


async def test_gcs_round_trip_applies_the_prefix() -> None:
    bucket = FakeBucket()
    store = _gcs_store(bucket)
    await store.put("esp32/a.bin", b"firmware", content_type="application/octet-stream")
    assert f"{PREFIX}esp32/a.bin" in bucket.objects
    assert await store.get("esp32/a.bin") == b"firmware"


async def test_gcs_put_blob_marks_the_object_immutable() -> None:
    """`Cache-Control` is metadata on the blob, set before the upload writes it."""
    payload = b"firmware"
    digest = digest_bytes(payload)
    bucket = FakeBucket()
    store = _gcs_store(bucket)
    assert await put_blob(store, payload) == blob_key(digest)
    resolved = f"{PREFIX}blobs/sha256/{digest}"
    assert resolved in bucket.objects
    assert bucket.cache_control_at_upload[resolved] == BLOB_CACHE_CONTROL


async def test_gcs_ordinary_put_sets_no_cache_control() -> None:
    bucket = FakeBucket()
    await _gcs_store(bucket).put("plain.bin", b"x")
    assert bucket.cache_control_at_upload[f"{PREFIX}plain.bin"] is None


async def test_gcs_get_of_a_missing_key_raises_object_not_found() -> None:
    with pytest.raises(ObjectNotFound):
        await _gcs_store(FakeBucket()).get("missing.bin")


async def test_gcs_get_refuses_an_oversized_object() -> None:
    bucket = FakeBucket()
    bucket.reported_size = 1025
    store = _gcs_store(bucket)
    await store.put("big.bin", b"x")
    with pytest.raises(ObjectTooLarge):
        await store.get("big.bin")


async def test_gcs_delete_is_idempotent_even_though_the_sdk_raises() -> None:
    """GCS raises `NotFound` where S3 does not; the Protocol contract wins."""
    bucket = FakeBucket()
    store = _gcs_store(bucket)
    await store.put("a.bin", b"x")
    await store.delete("a.bin")
    await store.delete("a.bin")
    assert bucket.objects == {}


async def test_gcs_signed_url_is_v4_get_with_the_expected_expiry() -> None:
    bucket = FakeBucket()
    url = await _gcs_store(bucket).signed_url("a.bin", ttl_s=300)
    assert url.startswith("https://storage.googleapis.com/")
    (call,) = bucket.signed_url_calls
    assert call["name"] == f"{PREFIX}a.bin"
    assert call["version"] == "v4"
    assert call["method"] == "GET"
    assert call["expiration"] == dt.timedelta(seconds=300)


async def test_gcs_transport_failure_becomes_object_store_error() -> None:
    class Exploding:
        def blob(self, name: str) -> Any:
            return self

        def upload_from_string(self, data: bytes, content_type: str = "") -> None:
            raise gcs_exceptions.ServiceUnavailable("backend down")

    store = GcsObjectStore(
        Exploding,
        bucket="btvaroska",
        prefix=PREFIX,
        default_ttl_s=60,
        max_get_bytes=1024,
        timeout_s=5.0,
    )
    with pytest.raises(ObjectStoreError, match="gcs put"):
        await store.put("a.bin", b"x")


async def test_gcs_verbs_reject_an_escaping_key() -> None:
    bucket = FakeBucket()
    store = _gcs_store(bucket)
    for key in ("../escape.bin", "/abs.bin", "a/../../b.bin"):
        with pytest.raises(ObjectKeyError):
            await store.put(key, b"x")
    assert bucket.objects == {}


# ---------------------------------------------------------------------------
# The credential is an impersonation, not a key file (S0-infra-5).
#
# **No network in any of these.** The SDK entry points are patched by dotted
# path because `factory.py` imports them INSIDE the function that uses them, so
# the attribute is resolved at call time and a `monkeypatch.setattr` lands.
# ---------------------------------------------------------------------------

# The 403 body the real API returns; it carries an opaque troubleshooter id that must
# never reach the exception an operator (or a handler) sees.
REFUSED_403 = (
    "('Unable to acquire impersonated credentials', "
    '\'{"error":{"code":403,"message":"Permission \\\'iam.serviceAccounts.getAccessToken\\\' '
    'denied on resource …","status":"PERMISSION_DENIED"}}\')'
)


class FakeImpersonatedCredentials:
    """Stands in for `google.auth.impersonated_credentials.Credentials`."""

    def __init__(
        self,
        *,
        source_credentials: Any,
        target_principal: str,
        target_scopes: list[str],
        lifetime: int | None = None,
    ) -> None:
        self.source_credentials = source_credentials
        self.target_principal = target_principal
        self.target_scopes = target_scopes
        self.lifetime = lifetime
        self.refreshes = 0

    def refresh(self, request: Any) -> None:
        self.refreshes += 1


class RefusedCredentials(FakeImpersonatedCredentials):
    """The failure this dev box actually produces: ADC resolves, the token mint is 403."""

    def refresh(self, request: Any) -> None:
        raise auth_exceptions.RefreshError(REFUSED_403)


class GcsSdkSpy:
    """What the factory asked the SDK for."""

    def __init__(self, bucket: FakeBucket) -> None:
        self.bucket = bucket
        self.default_scopes: list[Any] = []
        self.credentials: list[FakeImpersonatedCredentials] = []
        self.clients: list[dict[str, Any]] = []


def _patch_gcs_sdk(
    monkeypatch: pytest.MonkeyPatch,
    bucket: FakeBucket,
    *,
    credentials_cls: type[FakeImpersonatedCredentials] = FakeImpersonatedCredentials,
    default_raises: Exception | None = None,
) -> GcsSdkSpy:
    spy = GcsSdkSpy(bucket)

    def fake_default(scopes: Any = None) -> tuple[str, str]:
        spy.default_scopes.append(scopes)
        if default_raises is not None:
            raise default_raises
        return ("source-adc", "btvaroska")

    def fake_credentials(**kwargs: Any) -> FakeImpersonatedCredentials:
        creds = credentials_cls(**kwargs)
        spy.credentials.append(creds)
        return creds

    class FakeClient:
        def __init__(self, project: Any = None, credentials: Any = None) -> None:
            spy.clients.append({"project": project, "credentials": credentials})

        def bucket(self, name: str) -> FakeBucket:
            assert name == spy.bucket.name
            return spy.bucket

    monkeypatch.setattr("google.auth.default", fake_default)
    monkeypatch.setattr("google.auth.impersonated_credentials.Credentials", fake_credentials)
    monkeypatch.setattr("google.cloud.storage.Client", FakeClient)
    return spy


def _impersonating_settings(**overrides: object) -> Settings:
    return _settings(gcs_bucket="btvaroska", gcs_impersonate_service_account=TARGET_SA, **overrides)


def test_gcs_impersonation_construction_does_not_resolve_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`create_app()` must stay cheap: no metadata-server round trip at construction."""

    def exploding_default(scopes: Any = None) -> tuple[str, str]:
        raise AssertionError("google.auth.default() must not be called at construction")

    monkeypatch.setattr("google.auth.default", exploding_default)
    store = create_object_store(_impersonating_settings())
    assert isinstance(store, GcsObjectStore)


async def test_gcs_impersonation_targets_the_configured_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One credential, minted once, for the configured SA and storage scopes only."""
    spy = _patch_gcs_sdk(monkeypatch, FakeBucket())
    store = create_object_store(_impersonating_settings())

    await store.put("a.bin", b"x")
    assert await store.get("a.bin") == b"x"

    (creds,) = spy.credentials
    assert creds.target_principal == TARGET_SA
    assert creds.target_scopes == ["https://www.googleapis.com/auth/devstorage.read_write"]
    assert creds.source_credentials == "source-adc"
    # The SOURCE credential is the one that needs cloud-platform; signing needs no scope.
    assert spy.default_scopes == [["https://www.googleapis.com/auth/cloud-platform"]]
    # Eagerly refreshed once, so a refused token is diagnosed where it can be explained.
    assert creds.refreshes == 1
    # project=None, or google-cloud-storage goes looking for one through ADC.
    assert spy.clients == [{"project": None, "credentials": creds}]


async def test_gcs_impersonation_signs_without_a_private_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The impersonated credential signs V4 itself — no extra kwargs, no key file."""
    bucket = FakeBucket()
    _patch_gcs_sdk(monkeypatch, bucket)
    store = create_object_store(_impersonating_settings())
    url = await store.signed_url("a.bin", ttl_s=300)
    assert url.startswith("https://storage.googleapis.com/")
    (call,) = bucket.signed_url_calls
    assert call == {
        "name": f"{PREFIX}a.bin",
        "version": "v4",
        "expiration": dt.timedelta(seconds=300),
        "method": "GET",
    }


async def test_gcs_missing_adc_fails_the_verb_as_a_backend_error_naming_adc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ADC to impersonate FROM is a 503, not a 500: it is discovered mid-verb.

    `ObjectStoreConfigError` is translated only around `create_object_store`, so one
    raised inside a verb escapes an already-started request as a 500.
    """
    _patch_gcs_sdk(
        monkeypatch,
        FakeBucket(),
        default_raises=auth_exceptions.DefaultCredentialsError("no ADC here"),
    )
    store = create_object_store(_impersonating_settings())
    with pytest.raises(ObjectStoreError) as excinfo:
        await store.get("a.bin")
    assert "Application Default Credentials" in str(excinfo.value)
    assert not isinstance(excinfo.value, ObjectStoreConfigError)


async def test_gcs_impersonation_refused_names_the_principal_and_the_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runtime identity with no tokenCreator binding is the measured failure.

    Untranslated it reads `gcs get of … failed: RefreshError`, which names neither the
    principal nor the role. The 403 body stays out of the message.
    """
    _patch_gcs_sdk(monkeypatch, FakeBucket(), credentials_cls=RefusedCredentials)
    store = create_object_store(_impersonating_settings())
    with pytest.raises(ObjectStoreError) as excinfo:
        await store.get("a.bin")
    message = str(excinfo.value)
    assert TARGET_SA in message
    assert "serviceAccountTokenCreator" in message
    assert not isinstance(excinfo.value, ObjectStoreConfigError)
    assert "PERMISSION_DENIED" not in message
    assert "getAccessToken" not in message


async def test_gcs_describe_names_the_credential_mode_and_never_a_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`describe` is what the selftest prints. An SA email is not a credential; a path is."""
    _patch_gcs_sdk(monkeypatch, FakeBucket())
    impersonated = create_object_store(_impersonating_settings())
    assert f"creds=impersonated({TARGET_SA})" in impersonated.describe

    key_file = tmp_path / "fleetforge-artifacts.json"
    key_file.write_text("{}")
    with_key = create_object_store(
        _settings(gcs_bucket="btvaroska", gcs_credentials_file=str(key_file))
    )
    assert "creds=key-file" in with_key.describe
    assert str(key_file) not in with_key.describe


async def test_gcs_signed_url_does_not_block_the_event_loop() -> None:
    """Under impersonation, signing is an IAM `signBlob` call — it must not sit on the loop.

    The elapsed assertion is the real guard: a signing call left on the loop would only
    time out after the full sleep, because the timeout could never fire during it.
    """

    class SleepyBlob:
        def generate_signed_url(self, **kwargs: Any) -> str:
            time.sleep(2)
            return "never-returned"

    class SleepyBucket:
        def blob(self, name: str) -> SleepyBlob:
            return SleepyBlob()

    store = GcsObjectStore(
        SleepyBucket,
        bucket="btvaroska",
        prefix=PREFIX,
        default_ttl_s=60,
        max_get_bytes=1024,
        timeout_s=0.2,
    )
    started = time.monotonic()
    with pytest.raises(ObjectStoreError, match="gcs signed_url of .* timed out"):
        await store.signed_url("a.bin")
    assert time.monotonic() - started < 1.0


async def test_gcs_a_slow_bucket_factory_times_out() -> None:
    """The FIRST `bucket_factory()` call resolves ADC and mints a token; it can hang."""

    def slow_factory() -> FakeBucket:
        time.sleep(2)
        return FakeBucket()

    store = GcsObjectStore(
        slow_factory,
        bucket="btvaroska",
        prefix=PREFIX,
        default_ttl_s=60,
        max_get_bytes=1024,
        timeout_s=0.2,
    )
    started = time.monotonic()
    with pytest.raises(ObjectStoreError, match="gcs get of .* timed out"):
        await store.get("a.bin")
    assert time.monotonic() - started < 1.0


# ---------------------------------------------------------------------------
# Credentials never reach a log or an exception message.
# ---------------------------------------------------------------------------


async def test_no_credential_appears_in_a_log_or_an_error() -> None:
    settings = _settings(
        s3_endpoint_url="http://127.0.0.1:1",
        s3_bucket=BUCKET,
        s3_access_key="fleetforge",
        s3_secret_key=SECRET_KEY,
        object_store_timeout_s=2.0,
    )
    store = create_object_store(settings)
    with capture_logs() as records:
        with pytest.raises(ObjectStoreError) as excinfo:
            await store.put("a.bin", b"x")
        # A failing dependency, to force the 503 log line as well.
        with pytest.raises(HTTPException, match="503"):
            get_object_store(_settings())

    assert records, "expected at least one record — a vacuous pass is worse than no test"
    rendered = " ".join(record.getMessage() for record in records) + str(excinfo.value)
    assert SECRET_KEY not in rendered
    assert "fleetforge-artifacts.json" not in rendered


# ---------------------------------------------------------------------------
# The dependency: unconfigured is a 503, not a crash.
# ---------------------------------------------------------------------------


async def test_object_store_dependency_answers_503_when_unconfigured() -> None:
    """A throwaway route rather than a real endpoint — R1 owns the first real one."""
    app = FastAPI()
    settings = _settings()
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/probe")
    async def probe(store: ObjectStoreDep) -> dict[str, str]:  # pragma: no cover - never reached
        return {"ok": type(store).__name__}

    async with client_for(app) as client:
        response = await client.get("/probe")
    assert response.status_code == 503
    assert response.json()["detail"] == "object store not configured"


async def test_object_store_dependency_builds_a_store_when_configured() -> None:
    settings = _settings(
        s3_endpoint_url="http://minio:9000",
        s3_bucket=BUCKET,
        s3_access_key="fleetforge",
        s3_secret_key=SECRET_KEY,
    )
    app = FastAPI()
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/probe")
    async def probe(store: ObjectStoreDep) -> dict[str, str]:
        return {"kind": type(store).__name__}

    async with client_for(app) as client:
        response = await client.get("/probe")
    assert response.status_code == 200
    assert response.json() == {"kind": "S3ObjectStore"}


def test_create_app_warns_but_does_not_raise_without_a_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unconfigured storage is one WARNING plus a 503 at use time — never a crash.

    `create_app()` must stay constructible with no environment at all (the health tests
    depend on it), so this is the same shape as the `ADMIN_PASSWORD_HASH` warning.
    `get_settings` is patched in `api.main`'s namespace because it is `lru_cache`d and
    poking `os.environ` would either do nothing or poison the rest of the session.
    """
    from fleetforge.api import main as api_main

    monkeypatch.setattr(api_main, "get_settings", settings_for_tests)
    with capture_logs("fleetforge.api.main") as records:
        app = api_main.create_app()

    assert app is not None
    messages = [record.getMessage() for record in records]
    assert records, "expected at least one record — a vacuous pass is worse than no test"
    assert any("no object store configured" in message for message in messages)
