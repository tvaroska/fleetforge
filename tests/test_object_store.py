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
from typing import Any
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber
from fastapi import FastAPI, HTTPException
from google.api_core import exceptions as gcs_exceptions

from fleetforge.api.deps import ObjectStoreDep, get_object_store
from fleetforge.config import Settings, get_settings
from fleetforge.storage import (
    MAX_KEY_LEN,
    SIGNED_URL_MAX_TTL_S,
    ObjectKeyError,
    ObjectNotFound,
    ObjectStoreConfigError,
    ObjectStoreError,
    ObjectTooLarge,
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


def test_gcs_without_a_key_file_raises_rather_than_falling_back_to_adc() -> None:
    """ADC cannot sign a V4 URL and would use the wrong service account."""
    with pytest.raises(ObjectStoreConfigError, match="GCS_CREDENTIALS_FILE is required"):
        create_object_store(_settings(gcs_bucket="btvaroska"))


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
        self.signed_url_calls: list[dict[str, Any]] = []

    def upload_from_string(self, data: bytes, content_type: str = "") -> None:
        self._bucket.objects[self.name] = (data, content_type)

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
