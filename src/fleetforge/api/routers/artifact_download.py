"""`GET /v1/artifact/{sha256}/bin` — the public firmware download. **CRITICAL.** R1-be-3.

**This is the second unauthenticated endpoint in the app, after `POST /v1/enroll`, and
its credential is the signature in its query string** (`artifact_urls.py`;
`CRITICAL.md` → *Artifact signing keys*). There is deliberately no
`dependencies=[Depends(bearer_scheme), …]` on this router — a device downloading
firmware has no admin token and never will. `routers/artifacts.py` keeps `POST ""` in a
*different* module for exactly this reason: one router with a per-route auth opt-out is
how a public endpoint gets created by accident.

**It verifies our signature and then 307s to the object store.** The API process never
carries artifact bytes:

* `design/production.md` → *Artifact storage* is explicit that signed URLs are
  "range-capable, and served without touching the API process", so "device downloads do
  not consume the VM's bandwidth";
* a proxy would need an HTTP client in the production image (`httpx` is a **dev**
  dependency — `simulator/device.py::_fetch` uses `urllib.request` for that reason), and
  a `urllib` proxy would hold one of uvicorn's threadpool slots per board for the
  duration of a 1.9 MB transfer on a 256 M container;
* `Range`, `If-Range`, `Content-Range`, suffix ranges and 416 are then the **store's**
  RFC-correct implementation, not a hand-rolled parser on the OTA critical path.

Both clients follow redirects unchanged: `urllib.request` does (the simulator), and
`esp_https_ota` does via `esp_http_client_set_redirection` (R1-fw-1). curl, urllib and
`esp_http_client` all re-send a `Range:` header across a redirect.

What the indirection buys over putting the raw store URL in the `stage` command: the URL
shape is ours (and is what `spec/device-protocol.md` already fixed), the authorization is
ours and is backend-independent, the store URL's short life is decoupled from the
command's life (a resume 25 minutes later gets a freshly signed upstream URL with no new
deploy), and the only IAM `signBlob` traffic is one call per artifact per cache lifetime
(`storage/urlcache.py`).

**The order of work below is security-relevant** — digest shape, then configuration, then
the signature, and only then the store. Nothing above the signature check touches the
object store, so an unauthenticated caller can neither drive an IAM `signBlob` call nor
learn which digests exist. The store dependency is declared *after* `_authorized_digest`
so that FastAPI resolves it in that order too;
`tests/test_api_artifact_download.py::test_a_bad_signature_is_403_even_with_no_store`
pins it.

**A bad digest is 404, never 422.** A validation-error body is an oracle, and the only
useful answer to "is there firmware at this path?" from an unsigned caller is one
uniform "no".

**No database access at all.** A download must keep working while the database is
degraded, and the signature already carries the authorization: membership in `artifacts`
tells a signature-holder nothing it does not already know. A validly signed digest with
no object behind it ends as the store's own 404 after the redirect. Do not "just check
the table" here.

**`GET` only.** FastAPI adds no implicit `HEAD`, `esp_https_ota` sends none, and an
unused branch on a public endpoint is surface for nothing.

**No rate limiter.** Unlike `/v1/enroll` (one argon2 verification per request) the
pre-auth work here is one HMAC, and a fleet behind one NAT resuming together must not be
throttled off its own firmware; post-signature work is a cache hit.

**Logging: the digest and the outcome, never the query string, the signature or the
upstream URL.** Residual, recorded rather than fixed here: uvicorn's and nginx's *access*
logs record the full request line, so a valid signature is visible to whoever can read
container logs for its lifetime (`docs/features/ota-deploy.md`).
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from fleetforge.api.deps import ObjectStoreDep, SettingsDep
from fleetforge.artifact_urls import (
    ArtifactUrlExpired,
    ArtifactUrlInvalid,
    is_valid_digest,
    verify_artifact_url,
)
from fleetforge.storage.blobs import blob_key
from fleetforge.storage.objectstore import ObjectStoreError
from fleetforge.storage.urlcache import SignedUrlCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/artifact", tags=["artifacts"])

# One refusal for every way a signature can be wrong. A device cannot act differently on
# "expired" and "forged" — it re-enters the retry path either way — and an attacker must
# not be handed the difference. The distinction goes to the log.
REFUSED = "this download link is not valid"

# Named, retriable, and about the server — never the bucket, the key or an exception.
STORE_UNAVAILABLE = "the artifact store cannot be reached; try again shortly"
NOT_CONFIGURED = "artifact downloads are not configured on this server"


async def _authorized_digest(
    sha256: str,
    settings: SettingsDep,
    exp: Annotated[str | None, Query(description="Unix seconds this link dies at")] = None,
    sig: Annotated[str | None, Query(description="base64url HMAC over the link")] = None,
) -> str:
    """The digest, once the URL has proven it may have it. Raises 404 / 503 / 403.

    A dependency rather than the first lines of the handler so that it is resolved
    **before** `ObjectStoreDep`: an unsigned caller must not be able to reach the store
    factory at all, not even for the 503 an unconfigured one would raise.
    """
    if not is_valid_digest(sha256):
        # 404, not 422: one uniform answer, and no schema-error body to read.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such artifact")

    if not settings.artifact_url_secret:
        logger.error(
            "ARTIFACT_URL_SECRET is not set: no download link can be verified, so every "
            "GET /v1/artifact/{sha256}/bin answers 503. Mint one with `just artifact-secret`."
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=NOT_CONFIGURED)

    try:
        verify_artifact_url(sha256, exp=exp, sig=sig, secret=settings.artifact_url_secret)
    except (ArtifactUrlExpired, ArtifactUrlInvalid) as exc:
        # The reason is logged (with the digest, never the signature), the body is not.
        logger.info("artifact %s refused: %s", sha256, exc)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=REFUSED) from None

    return sha256


@router.get(
    "/{sha256}/bin",
    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    summary="Download a firmware artifact (public; the signature is the authorization)",
    response_class=RedirectResponse,
)
async def download_artifact(
    digest: Annotated[str, Depends(_authorized_digest)],
    request: Request,
    store: ObjectStoreDep,
) -> RedirectResponse:
    """Redirect a device to the bytes, on a store URL cached for its own lifetime.

    307, not 302: temporary, method-preserving, and explicitly not cacheable —
    `Cache-Control: no-store`, because the target is a credential with minutes of life
    and a shared cache must not keep a copy of it.
    """
    cache: SignedUrlCache = request.app.state.artifact_url_cache
    try:
        url = await cache.get(store, blob_key(digest))
    except ObjectStoreError:
        # Never the bucket, the key or the exception text.
        logger.exception("artifact %s could not be served: the object store is unavailable", digest)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=STORE_UNAVAILABLE
        ) from None

    logger.info("artifact %s authorized; redirecting to the store", digest)
    return RedirectResponse(
        url,
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={"Cache-Control": "no-store"},
    )
