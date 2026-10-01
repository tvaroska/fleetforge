"""`python -m fleetforge.deploycheck` — the client half, against a fake API. S0-infra-9.

The real proof is the live run (`just deploy-check` against `just up`); these pin the
diagnosis each failure gets, because "HTTP 403" alone sends an operator to the wrong
place. The fake API below does what `GET /v1/artifact/{sha256}/bin` does: verify the
signature with ITS OWN secret, then serve the blob from the store.
"""

import io
import json
import urllib.error
import urllib.parse
from email.message import Message
from typing import Any

import pytest

from fleetforge.artifact_urls import ArtifactUrlRefused, verify_artifact_url
from fleetforge.deploycheck import PROBE, main, run
from fleetforge.storage.blobs import blob_key, digest_bytes
from tests.conftest import (
    TEST_ARTIFACT_URL_SECRET,
    TEST_PUBLIC_BASE_URL,
    MemoryObjectStore,
    settings_for_tests,
)


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, url: str) -> None:
        super().__init__(body)
        self._url = url

    def geturl(self) -> str:
        return self._url


def fake_api(
    store: MemoryObjectStore, *, secret: str = TEST_ARTIFACT_URL_SECRET, not_ready: str = ""
) -> Any:
    """An opener that answers the way our download endpoint plus the store would."""

    def opener(url: str) -> Any:
        parts = urllib.parse.urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}" == TEST_PUBLIC_BASE_URL
        if parts.path == "/v1/readyz":
            if not_ready:
                body = io.BytesIO(json.dumps({"detail": not_ready}).encode())
                raise urllib.error.HTTPError(url, 503, "Service Unavailable", Message(), body)
            return FakeResponse(b'{"status": "ready"}', url)
        sha256 = parts.path.split("/")[3]
        query = dict(urllib.parse.parse_qsl(parts.query))
        try:
            verify_artifact_url(sha256, exp=query.get("exp"), sig=query.get("sig"), secret=secret)
        except ArtifactUrlRefused:
            raise urllib.error.HTTPError(url, 403, "Forbidden", Message(), None) from None
        body = store.objects.get(blob_key(sha256))
        if body is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)
        return FakeResponse(body, f"https://store.invalid/{blob_key(sha256)}?X-Sig=secret")

    return opener


async def test_a_working_stack_serves_the_probe(capsys: pytest.CaptureFixture[str]) -> None:
    store = MemoryObjectStore()
    await run(settings_for_tests(), store, fake_api(store))
    assert store.objects == {blob_key(digest_bytes(PROBE)): PROBE}
    out = capsys.readouterr().out
    assert "a board could download firmware" in out
    # The link and the store's redirect target are both credentials.
    assert "sig=" not in out.replace("sig=…", "")
    assert "X-Sig" not in out


async def test_rerunning_writes_the_same_single_object() -> None:
    store = MemoryObjectStore()
    for _ in range(2):
        await run(settings_for_tests(), store, fake_api(store))
    assert len(store.objects) == 1


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"public_base_url": None}, "PUBLIC_BASE_URL is not set"),  # F-2026-09-23-001
        ({"artifact_url_secret": ""}, "ARTIFACT_URL_SECRET is not set"),
    ],
)
async def test_a_missing_setting_fails_before_any_io(
    overrides: dict[str, object], message: str
) -> None:
    store = MemoryObjectStore()
    with pytest.raises(RuntimeError, match=message):
        await run(settings_for_tests(**overrides), store, fake_api(store))
    assert store.puts == []


async def test_an_api_that_is_not_ready_fails_with_its_own_reason() -> None:
    """F-2026-09-23-001 from the host: the API lacks PUBLIC_BASE_URL, this shell has it.

    The download itself would succeed — the endpoint never reads PUBLIC_BASE_URL — so
    only the API's own readiness can catch it, and nothing is written first.
    """
    store = MemoryObjectStore()
    api = fake_api(store, not_ready="not configured: PUBLIC_BASE_URL")
    with pytest.raises(RuntimeError, match="HTTP 503: not configured: PUBLIC_BASE_URL"):
        await run(settings_for_tests(), store, api)
    assert store.puts == []


async def test_an_api_with_a_different_secret_is_named() -> None:
    store = MemoryObjectStore()
    with pytest.raises(RuntimeError, match="HTTP 403: the API refused the signature"):
        await run(settings_for_tests(), store, fake_api(store, secret="rotated"))


async def test_an_unreachable_origin_is_named() -> None:
    def refused(url: str) -> Any:
        raise urllib.error.URLError("connection refused")

    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL is unreachable from here"):
        await run(settings_for_tests(), MemoryObjectStore(), refused)


async def test_wrong_bytes_fail() -> None:
    store = MemoryObjectStore()

    def tampered(url: str) -> Any:
        return FakeResponse(b"not the probe", "https://store.invalid/x")

    with pytest.raises(RuntimeError, match="different bytes"):
        await run(settings_for_tests(), store, tampered)


def test_main_reports_failure_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unconfigured environment is `SELFTEST FAILED` and exit 1, never a traceback."""

    def no_store(settings: object) -> None:
        raise RuntimeError("no object store configured")

    monkeypatch.setattr("fleetforge.deploycheck.Settings", lambda: settings_for_tests())
    monkeypatch.setattr("fleetforge.deploycheck.create_object_store", no_store)
    assert main([]) == 1
    assert "SELFTEST FAILED: RuntimeError: no object store configured" in capsys.readouterr().err
