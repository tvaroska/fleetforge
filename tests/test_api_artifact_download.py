"""`GET /v1/artifact/{sha256}/bin` — the public download. **CRITICAL.** R1-be-3.

The endpoint is unauthenticated by design, so these tests are mostly about what it
refuses and what a refusal *costs*:

* **A refusal costs no signing call.** `store.signed_urls == []` is asserted next to
  every 403 — "refused" and "refused before it reached the object store" are different
  promises, and only the second one keeps an anonymous caller from driving IAM
  `signBlob` traffic (S0-infra-5) or enumerating which digests exist.
* **No status code leaks which digests exist.** A well-formed digest that was never
  uploaded and one that was look identical without a signature.
* **422 never happens.** A schema-error body is an oracle; a malformed digest is 404 and
  a malformed `exp` is 403.
* **Ten ranged requests cost one upstream signature**, and the cache is still a cache:
  once the refresh margin bites, the next request signs again.
* **The redirect is not cacheable.** Its target is a credential with minutes of life.

No database is touched anywhere below — that is the point of `app_no_db`, and a
regression that adds a query here will fail against its deliberately unusable URL.
"""

import datetime as dt
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI

from fleetforge.api.deps import get_object_store, get_settings
from fleetforge.artifact_urls import mint_artifact_url
from fleetforge.clock import now_utc
from fleetforge.storage.blobs import blob_key
from fleetforge.storage.objectstore import ObjectStoreError
from fleetforge.storage.urlcache import SignedUrlCache
from tests.conftest import (
    TEST_ARTIFACT_URL_SECRET,
    TEST_PUBLIC_BASE_URL,
    MemoryObjectStore,
    capture_logs,
    client_for,
    settings_for_tests,
)

SHA256 = "5" * 63 + "e"
KEY = blob_key(SHA256)

# The cache the app is rebuilt with in every test below: long enough that a test never
# re-signs by accident, with a margin that leaves an obvious reuse window.
TTL_S = 900
MARGIN_S = 60


@pytest.fixture
def store(app_no_db: FastAPI) -> Iterator[MemoryObjectStore]:
    store = MemoryObjectStore()
    store.objects[KEY] = b"firmware"
    app_no_db.dependency_overrides[get_object_store] = lambda: store
    yield store
    app_no_db.dependency_overrides.pop(get_object_store, None)


@pytest.fixture
def app(app_no_db: FastAPI, store: MemoryObjectStore) -> FastAPI:
    """The app with a known secret, a fake store and a cache with known numbers.

    `app.state.artifact_url_cache` is replaced rather than reused: `create_app()` built
    one from the *environment's* settings, and a cache whose TTL depends on the
    developer's `.env` makes the signing-count assertions below mean nothing.
    """
    settings = settings_for_tests()
    app_no_db.dependency_overrides[get_settings] = lambda: settings
    app_no_db.state.artifact_url_cache = SignedUrlCache(ttl_s=TTL_S, refresh_margin_s=MARGIN_S)
    return app_no_db


def url_for(sha256: str = SHA256, *, ttl_s: int = 600) -> str:
    return mint_artifact_url(
        TEST_PUBLIC_BASE_URL, sha256, secret=TEST_ARTIFACT_URL_SECRET, ttl_s=ttl_s
    )


def path_and_query(url: str) -> str:
    """The minted URL as the app sees it: everything after the origin."""
    return url[len(TEST_PUBLIC_BASE_URL) :]


async def get(app: FastAPI, target: str, **kwargs: object) -> httpx.Response:
    async with client_for(app) as client:
        return await client.get(target, **kwargs)  # type: ignore[arg-type]


class TestTheHappyPath:
    async def test_a_valid_link_redirects_to_the_store(
        self, app: FastAPI, store: MemoryObjectStore
    ) -> None:
        response = await get(app, path_and_query(url_for()))

        assert response.status_code == 307
        assert response.headers["location"] == f"https://memory.invalid/{KEY}?ttl={TTL_S}"
        assert store.signed_urls == [(KEY, TTL_S)]

    async def test_the_redirect_is_not_cacheable(self, app: FastAPI) -> None:
        """Its target is a bearer credential; a shared cache must not keep a copy."""
        response = await get(app, path_and_query(url_for()))

        assert response.headers["cache-control"] == "no-store"

    async def test_the_store_is_asked_for_the_blob_key_not_the_digest(
        self, app: FastAPI, store: MemoryObjectStore
    ) -> None:
        """The URL carries the digest; `storage/blobs.py` owns the key layout."""
        await get(app, path_and_query(url_for()))

        assert store.signed_urls == [(KEY, TTL_S)]
        assert KEY != SHA256

    async def test_it_does_not_need_the_artifact_to_be_known_to_the_store(
        self, app: FastAPI, store: MemoryObjectStore
    ) -> None:
        """No existence check, and no database: the store's own 404 answers that.

        A `HEAD`-style probe here would cost a round trip on every resume and would turn
        the endpoint into an existence oracle for anyone holding one valid signature.
        """
        store.objects.clear()

        response = await get(app, path_and_query(url_for()))

        assert response.status_code == 307
        assert store.gets == []

    async def test_a_range_header_is_not_the_api_s_business(
        self, app: FastAPI, store: MemoryObjectStore
    ) -> None:
        """We redirect; the store implements RFC 7233. See the module docstring."""
        response = await get(app, path_and_query(url_for()), headers={"Range": "bytes=1000-1099"})

        assert response.status_code == 307
        assert "content-range" not in response.headers


class TestRefusals:
    """Every one of these must be a 403/404 that cost the store nothing."""

    async def test_an_expired_link_is_403(self, app: FastAPI, store: MemoryObjectStore) -> None:
        # Minted an hour ago with a one-second life, rather than `sleep`ing for it.
        expired = mint_artifact_url(
            TEST_PUBLIC_BASE_URL,
            SHA256,
            secret=TEST_ARTIFACT_URL_SECRET,
            ttl_s=1,
            now=now_utc() - dt.timedelta(hours=1),
        )

        response = await get(app, path_and_query(expired))

        assert response.status_code == 403
        assert store.signed_urls == []

    async def test_a_tampered_signature_is_403_and_costs_no_signing_call(
        self, app: FastAPI, store: MemoryObjectStore
    ) -> None:
        url = url_for()
        tampered = url[:-1] + ("X" if url[-1] != "X" else "Y")

        response = await get(app, path_and_query(tampered))

        assert response.status_code == 403
        # The promise that matters: an anonymous caller cannot make us call the store.
        assert store.signed_urls == []

    async def test_a_signature_for_another_digest_is_403(
        self, app: FastAPI, store: MemoryObjectStore
    ) -> None:
        other = "9" * 63 + "a"
        query = url_for(other).split("?", 1)[1]

        response = await get(app, f"/v1/artifact/{SHA256}/bin?{query}")

        assert response.status_code == 403
        assert store.signed_urls == []

    @pytest.mark.parametrize(
        "query",
        [
            "",
            "?exp=1",
            "?sig=abc",
            "?exp=later&sig=abc",
            "?exp=&sig=",
            "?exp=99999999999&sig=forged",
        ],
        ids=["nothing", "no-sig", "no-exp", "garbage-exp", "empty", "forged"],
    )
    async def test_missing_or_garbage_parameters_are_403_never_422(
        self, app: FastAPI, store: MemoryObjectStore, query: str
    ) -> None:
        response = await get(app, f"/v1/artifact/{SHA256}/bin{query}")

        assert response.status_code == 403, response.text
        assert store.signed_urls == []

    @pytest.mark.parametrize(
        "digest",
        [SHA256.upper(), "abc", "g" * 64, f"sha256:{SHA256}", SHA256 + "0"],
        ids=["uppercase", "short", "not-hex", "prefixed", "long"],
    )
    async def test_a_malformed_digest_is_404_never_422(
        self, app: FastAPI, store: MemoryObjectStore, digest: str
    ) -> None:
        """One uniform answer, and no validation-error body to read the rules off."""
        query = url_for().split("?", 1)[1]

        response = await get(app, f"/v1/artifact/{digest}/bin?{query}")

        assert response.status_code == 404, response.text
        assert store.signed_urls == []

    async def test_a_refusal_says_nothing_about_which_digests_exist(
        self, app: FastAPI, store: MemoryObjectStore
    ) -> None:
        """The stored and the never-uploaded digest are indistinguishable without a sig."""
        unknown = "7" * 64

        stored = await get(app, f"/v1/artifact/{SHA256}/bin")
        missing = await get(app, f"/v1/artifact/{unknown}/bin")

        assert stored.status_code == missing.status_code == 403
        assert stored.json() == missing.json()

    async def test_the_signature_is_never_echoed_or_logged(
        self, app: FastAPI, store: MemoryObjectStore
    ) -> None:
        url = url_for()
        sig = url.split("sig=", 1)[1]

        with capture_logs() as records:
            response = await get(app, path_and_query(url[:-1] + "X"))

        assert response.status_code == 403
        assert sig[:-1] not in response.text
        assert records, "the refusal should have been logged at all"
        messages = [record.getMessage() for record in records]
        assert not any(sig[:-1] in message for message in messages)
        # The digest *is* logged: an operator has to be able to find the link's artifact.
        assert any(SHA256 in message for message in messages)

    async def test_only_get_is_served(self, app: FastAPI) -> None:
        """No implicit HEAD, no POST: an unused branch on a public endpoint is surface."""
        async with client_for(app) as client:
            head = await client.head(path_and_query(url_for()))
            post = await client.post(path_and_query(url_for()))

        assert head.status_code == 405
        assert post.status_code == 405


class TestOrdering:
    """Nothing above the signature check may touch the store. This is the order test."""

    async def test_a_bad_signature_is_403_even_with_no_store(self, app: FastAPI) -> None:
        """With **no** store backend configured, `ObjectStoreDep` raises 503.

        An unsigned caller must see 403 anyway, which is only true while FastAPI
        resolves `_authorized_digest` before `ObjectStoreDep` — i.e. while the store
        parameter is declared *after* it in `download_artifact`. Reordering those two
        parameters flips this to 503 and hands an anonymous caller a probe that reaches
        the store factory.
        """
        app.dependency_overrides.pop(get_object_store)  # back to the real factory

        response = await get(app, f"/v1/artifact/{SHA256}/bin?exp=99999999999&sig=forged")

        assert response.status_code == 403
        # ...and the store dependency really would have answered 503, so the assertion
        # above is about ordering and not about a store that happens to work.
        signed = await get(app, path_and_query(url_for()))
        assert signed.status_code == 503

    async def test_an_unconfigured_secret_is_503_before_any_signature_work(
        self, app: FastAPI, store: MemoryObjectStore
    ) -> None:
        """A server that cannot verify must say so, not refuse everyone as forgers."""
        settings = settings_for_tests(artifact_url_secret=None)
        app.dependency_overrides[get_settings] = lambda: settings

        response = await get(app, path_and_query(url_for()))

        assert response.status_code == 503
        assert store.signed_urls == []

    async def test_a_malformed_digest_is_404_even_when_unconfigured(self, app: FastAPI) -> None:
        """Shape first: an unconfigured server must not answer 503 to garbage paths."""
        app.dependency_overrides[get_settings] = lambda: settings_for_tests(
            artifact_url_secret=None
        )

        response = await get(app, "/v1/artifact/nope/bin")

        assert response.status_code == 404


class TestTheSigningBudget:
    """One upstream signature per artifact per cache lifetime — the S0-infra-5 budget."""

    async def test_ten_ranged_requests_cost_one_signing_call(
        self, app: FastAPI, store: MemoryObjectStore
    ) -> None:
        target = path_and_query(url_for())

        with capture_logs() as records:
            async with client_for(app) as client:
                for i in range(10):
                    response = await client.get(
                        target, headers={"Range": f"bytes={i * 20480}-{(i + 1) * 20480 - 1}"}
                    )
                    assert response.status_code == 307

        assert store.signed_urls == [(KEY, TTL_S)]
        signings = [r for r in records if "signed a fresh artifact URL" in r.getMessage()]
        assert len(signings) == 1, "the T2 log observable must fire exactly once"

    async def test_two_artifacts_are_two_entries(
        self, app: FastAPI, store: MemoryObjectStore
    ) -> None:
        other = "1" * 64

        await get(app, path_and_query(url_for()))
        await get(app, path_and_query(url_for(other)))
        await get(app, path_and_query(url_for()))

        assert store.signed_urls == [(KEY, TTL_S), (blob_key(other), TTL_S)]

    async def test_the_cache_is_a_cache_not_a_permanent_memo(
        self, app: FastAPI, store: MemoryObjectStore
    ) -> None:
        """With the margin at the TTL the reuse window is zero: every request re-signs.

        This is also the documented degradation for a misconfigured margin — slow, never
        a mid-transfer 403.
        """
        app.state.artifact_url_cache = SignedUrlCache(ttl_s=TTL_S, refresh_margin_s=TTL_S)

        await get(app, path_and_query(url_for()))
        await get(app, path_and_query(url_for()))

        assert len(store.signed_urls) == 2

    async def test_a_store_failure_is_503_and_is_not_cached(
        self, app: FastAPI, store: MemoryObjectStore
    ) -> None:
        store.fail_with = ObjectStoreError("connect timeout to bucket fleetforge-prod")

        failed = await get(app, path_and_query(url_for()))

        assert failed.status_code == 503
        detail = failed.json()["detail"]
        assert "fleetforge-prod" not in detail
        assert KEY not in detail

        # Not cached, and not poisoned: the very next request succeeds.
        store.fail_with = None
        recovered = await get(app, path_and_query(url_for()))
        assert recovered.status_code == 307
