"""`get_object_store` builds the store once per process (S0-bug-4).

It used to call `create_object_store` on every request, which built a new boto3 session and
client (or, on GCS, resolved ADC and minted a token) each time: ~4.4 MiB of RSS per request
on the v0.6.0 image, a new TLS connection per request, and prod's 256 MiB api container
sat at 84 %. These tests pin the three properties that keep the cache safe: it is reused,
it follows the settings, and a failed build is never cached.
"""

from collections.abc import Iterator

import pytest
from fastapi import HTTPException

from fleetforge.api import deps
from fleetforge.api.deps import get_object_store
from fleetforge.config import Settings
from fleetforge.storage.objectstore import ObjectStore
from tests.conftest import settings_for_tests

S3 = {
    "s3_endpoint_url": "http://minio:9000",
    "s3_bucket": "artifacts",
    "s3_access_key": "fleetforge",
    "s3_secret_key": "fake-not-a-secret",  # noqa: S106
}


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    deps._store_cache = None
    yield
    deps._store_cache = None


def test_the_same_settings_return_the_same_store() -> None:
    first = get_object_store(settings_for_tests(**S3))
    again = get_object_store(settings_for_tests(**S3))
    assert first is again


def test_create_object_store_runs_once_across_many_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fleetforge.storage.factory as factory

    calls = []
    real = factory.create_object_store

    def counting(settings: Settings) -> ObjectStore:
        calls.append(1)
        return real(settings)

    monkeypatch.setattr(factory, "create_object_store", counting)
    settings = settings_for_tests(**S3)
    stores = {id(get_object_store(settings)) for _ in range(50)}
    assert len(calls) == 1
    assert len(stores) == 1


def test_a_changed_store_setting_builds_a_new_store() -> None:
    first = get_object_store(settings_for_tests(**S3))
    other = get_object_store(settings_for_tests(**{**S3, "s3_bucket": "elsewhere"}))
    assert other is not first
    assert get_object_store(settings_for_tests(**{**S3, "s3_bucket": "elsewhere"})) is other


def test_a_failed_build_is_not_cached() -> None:
    unconfigured = settings_for_tests(s3_endpoint_url="", s3_bucket="", gcs_bucket="")
    for _ in range(2):
        with pytest.raises(HTTPException, match="503"):
            get_object_store(unconfigured)
    assert deps._store_cache is None
    assert get_object_store(settings_for_tests(**S3)) is not None
