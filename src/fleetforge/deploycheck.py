"""`python -m fleetforge.deploycheck` — prove a board could download firmware. S0-infra-9.

```
just deploy-check                  # this, then the broker matrix: the release gate
just deploy-check-prod             # the same two, inside the prod API container
```

`/v1/readyz` proves the deploy-mandatory settings are *present*. This proves they
*work*, by walking the exact path a board walks after a `stage` command, against a
running stack:

0. GET `{PUBLIC_BASE_URL}/v1/readyz` and require 200. That is the **API's** view of its
   own configuration — it catches an API container missing `PUBLIC_BASE_URL` even when
   this environment has one, which steps 1–4 cannot (the download endpoint never reads
   it; only the deploy that mints the link does);
1. write a fixed probe blob through `put_blob` (content-addressed, so re-running is a
   no-op and nothing accumulates — one 40-byte object, ever, per store);
2. mint a download link with `mint_artifact_url`, on `PUBLIC_BASE_URL`, signed with
   `ARTIFACT_URL_SECRET` — the same function `api/routers/deploys.py` mints with;
3. GET it with no credentials. That hits the API's `GET /v1/artifact/{sha256}/bin`,
   which verifies the signature with **its own** secret and 307s to the store; urllib
   follows the redirect exactly as `esp_https_ota` does;
4. compare the bytes to the probe's digest.

So a missing or wrong `PUBLIC_BASE_URL` (ops-log F-2026-09-23-001), an API whose secret
disagrees with this environment's, an unconfigured or unreachable store and a store URL a
board could not reach all fail here, each with a different message. The broker half
(F-2026-09-23-002) is `python -m fleetforge.broker selftest`, chained after this by the
recipe rather than imported, so each keeps its own transcript.

**Where to run it.** Wherever `PUBLIC_BASE_URL` resolves the way a board resolves it. In
dev that is the HOST (`http://localhost:${FF_HTTP_PORT}`; inside the container
`localhost` is the container). In prod it is inside `fleetforge-api` (`https://bingo.tvaroska.sk`, with the
GCS settings only the container has).

**Prints no secret and no signature.** The link is a bearer credential, even for a probe
blob, so only its path is printed. Same argparse / stdout-is-the-transcript shape as
`storage/__main__.py`; `urllib.request` because httpx is a dev dependency.
"""

import argparse
import asyncio
import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from fleetforge.artifact_urls import artifact_path, mint_artifact_url
from fleetforge.config import Settings
from fleetforge.storage.blobs import digest_bytes, put_blob
from fleetforge.storage.factory import create_object_store
from fleetforge.storage.objectstore import ObjectStore

# Fixed bytes, so the digest — and therefore the object — is the same on every run.
PROBE = b"fleetforge deploy-check probe, format 1\n"
# Long enough for one fetch, short enough that a leaked link is worthless.
PROBE_TTL_S = 120
FETCH_TIMEOUT_S = 30

# What each refusal from our own endpoint means, because "HTTP 403" alone sends an
# operator to the wrong place.
_HINTS = {
    403: "the API refused the signature: its ARTIFACT_URL_SECRET differs from this environment's",
    404: "the link reached the store and the probe is not there: wrong bucket or prefix",
    503: (
        "the API cannot serve downloads: no ARTIFACT_URL_SECRET or no object store in the "
        "API's own environment, or the store is down — see `GET /v1/readyz` and its log"
    ),
}

Opener = Callable[[str], Any]


def _step(message: str) -> None:
    """One line per step, to stdout, so the whole run reads as a transcript."""
    print(message, flush=True)


def _open(url: str) -> Any:
    return urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S)  # noqa: S310 - http(s) from PUBLIC_BASE_URL


def _check_ready(base_url: str, opener: Opener) -> None:
    """Step 0: the API's own readiness, through the origin a board uses."""
    url = f"{base_url.rstrip('/')}/v1/readyz"
    try:
        with opener(url) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail", "")
        except (ValueError, AttributeError):
            detail = ""
        raise RuntimeError(f"{url} answered HTTP {exc.code}: {detail or 'no detail'}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"PUBLIC_BASE_URL is unreachable from here: {exc.reason}") from exc
    _step(f"ready    {url} answered 200")


async def run(settings: Settings, store: ObjectStore, opener: Opener = _open) -> None:
    """Raise with a diagnosis unless a signed download link serves the probe's bytes."""
    if not settings.artifact_url_secret:
        raise RuntimeError("ARTIFACT_URL_SECRET is not set: no download link can be signed")
    if not settings.public_base_url:
        raise RuntimeError(
            "PUBLIC_BASE_URL is not set: there is no origin a board could download from"
        )

    _check_ready(settings.public_base_url, opener)

    digest = digest_bytes(PROBE)
    key = await put_blob(store, PROBE)
    _step(f"put      {key} ({len(PROBE)} bytes, content-addressed probe)")

    url = mint_artifact_url(
        settings.public_base_url, digest, secret=settings.artifact_url_secret, ttl_s=PROBE_TTL_S
    )
    _step(f"link     {settings.public_base_url.rstrip('/')}{artifact_path(digest)}?exp=…&sig=…")

    try:
        with opener(url) as response:
            body = bytes(response.read())
            final = response.geturl()
    except urllib.error.HTTPError as exc:
        hint = _HINTS.get(exc.code, "unexpected status")
        raise RuntimeError(f"download refused with HTTP {exc.code}: {hint}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"PUBLIC_BASE_URL or the store's redirect target is unreachable from here: {exc.reason}"
        ) from exc

    # Never the full redirect target: it is the store's own signed URL.
    _step(f"fetch    {len(body)} bytes, served from {urllib.parse.urlsplit(final).netloc}")
    if hashlib.sha256(body).hexdigest() != digest:
        raise RuntimeError("the download link served different bytes than the probe")
    _step("verify   bytes match the probe's sha256 — a board could download firmware")


def main(argv: list[str] | None = None) -> int:
    """Run the check. Returns a process exit code; `SELFTEST OK` means success."""
    parser = argparse.ArgumentParser(prog="python -m fleetforge.deploycheck")
    parser.parse_args(argv)
    try:
        settings = Settings()  # type: ignore[call-arg]  # values come from the environment
        asyncio.run(run(settings, create_object_store(settings)))
    except (ValueError, RuntimeError, OSError) as exc:
        # ObjectStoreConfigError and every ObjectStore*Error are RuntimeErrors; a
        # pydantic ValidationError is a ValueError — all land here, never as a traceback.
        print(f"SELFTEST FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("SELFTEST OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
