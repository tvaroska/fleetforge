"""Google Cloud Storage — the production `ObjectStore`. **CRITICAL** (signing, key file).

`DECISIONS.md` 2026-09-08 → *Artifacts in GCS, MinIO for self-hosting*: artifact bytes
live in `gs://btvaroska/fleetforge/`, served to devices by a V4 signed URL without ever
touching the API process.

Three properties that are easy to get wrong:

1. **V4 signing needs a real private key in the credentials.** Application Default
   Credentials on a GCE VM come from the metadata server and carry no private key, so
   signing would need an IAM `signBlob` round trip and extra permissions. `factory.py`
   therefore **requires `GCS_CREDENTIALS_FILE` and never falls back to ADC** — an ADC
   fallback would also silently pick up the project-wide compute default service
   account, which is exactly the credential the prefix IAM condition exists to avoid.
2. **`gs://btvaroska` is shared** with this estate's `secrets/` backups and the `boris`
   podcast audio. `validate_prefix` runs in the constructor and `resolve_key` runs on
   every verb; the service-account key is additionally bound by an IAM condition to
   `…/objects/fleetforge/…` (`docs/runbooks/artifact-storage.md`).
3. **`delete` of a missing blob raises `NotFound` here and does not on S3.** The
   idempotency lives in the Protocol contract, so this adapter swallows it rather than
   every caller learning which backend it is talking to.

`google-cloud-storage` is synchronous: every network call goes through
`asyncio.to_thread` under an `asyncio.timeout`. Signing does not — it is local CPU.
"""

import asyncio
import contextlib
import datetime as dt
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from google.api_core import exceptions as gcs_exceptions
from google.auth import exceptions as auth_exceptions

from fleetforge.storage.objectstore import (
    ObjectNotFound,
    ObjectStoreError,
    ObjectTooLarge,
    resolve_key,
    validate_prefix,
    validate_ttl,
)

logger = logging.getLogger(__name__)

BucketFactory = Callable[[], Any]


class GcsObjectStore:
    """`ObjectStore` backed by Google Cloud Storage.

    `bucket_factory` returns a `google.cloud.storage.Bucket`; it is injected for the
    same reason `S3ObjectStore` takes client factories (tests use a duck-typed fake —
    there is no `Stubber` equivalent for this SDK) and so the real client, which is
    expensive to build and reads a key file from disk, is constructed exactly once.
    """

    def __init__(
        self,
        bucket_factory: BucketFactory,
        *,
        bucket: str,
        prefix: str,
        default_ttl_s: int,
        max_get_bytes: int,
        timeout_s: float,
    ) -> None:
        self._bucket_factory = bucket_factory
        self._bucket = bucket
        self._prefix = validate_prefix(prefix)
        self._default_ttl_s = default_ttl_s
        self._max_get_bytes = max_get_bytes
        self._timeout_s = timeout_s

    @property
    def describe(self) -> str:
        """A one-line, **credential-free** description for logs and the selftest."""
        return f"gcs bucket={self._bucket} prefix={self._prefix or '(none)'}"

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        """Store `data` at `key`, overwriting. See the Protocol for the contract."""
        resolved = resolve_key(self._prefix, key)
        blob = self._bucket_factory().blob(resolved)
        async with self._guard("put", resolved):
            await asyncio.to_thread(blob.upload_from_string, data, content_type=content_type)

    async def get(self, key: str) -> bytes:
        """Return the bytes at `key`, refusing an oversized object before downloading."""
        resolved = resolve_key(self._prefix, key)
        blob = self._bucket_factory().blob(resolved)
        async with self._guard("get", resolved):
            # `reload()` populates `size` from object metadata; it is also what turns a
            # missing object into `NotFound` before any bytes move.
            await asyncio.to_thread(blob.reload)
            size = int(blob.size or 0)
            if size > self._max_get_bytes:
                raise ObjectTooLarge(
                    f"object {resolved} is {size} bytes, over the {self._max_get_bytes} cap"
                )
            data = await asyncio.to_thread(blob.download_as_bytes)
        return bytes(data)

    async def signed_url(self, key: str, *, ttl_s: int | None = None) -> str:
        """A V4 signed GET URL. Local CPU only — no `to_thread`, no I/O.

        Requires the credentials to carry a private key; `factory.py` guarantees that
        by refusing to construct this adapter without `GCS_CREDENTIALS_FILE`.
        """
        resolved = resolve_key(self._prefix, key)
        ttl = validate_ttl(self._default_ttl_s if ttl_s is None else ttl_s)
        blob = self._bucket_factory().blob(resolved)
        try:
            url = blob.generate_signed_url(
                version="v4",
                expiration=dt.timedelta(seconds=ttl),
                method="GET",
            )
        except (gcs_exceptions.GoogleAPIError, auth_exceptions.GoogleAuthError, OSError) as exc:
            raise ObjectStoreError(
                f"could not sign a URL for {resolved}: {type(exc).__name__}"
            ) from exc
        return str(url)

    async def delete(self, key: str) -> None:
        """Remove `key`. Idempotent — GCS raises `NotFound`, which is swallowed here."""
        resolved = resolve_key(self._prefix, key)
        blob = self._bucket_factory().blob(resolved)
        try:
            async with self._guard("delete", resolved):
                await asyncio.to_thread(blob.delete)
        except ObjectNotFound:
            logger.debug("gcs delete of %s: already absent", resolved)

    @contextlib.asynccontextmanager
    async def _guard(self, verb: str, resolved: str) -> AsyncIterator[None]:
        """Timeout plus exception translation for one backend call.

        Everything the SDK can raise becomes an `ObjectStoreError` (or
        `ObjectNotFound`): a `GoogleAPIError` escaping into a request handler is a 500
        where a retriable 503 belongs. **The message names the verb and the key, never
        the credentials or the key-file path.**
        """
        try:
            async with asyncio.timeout(self._timeout_s):
                yield
        except TimeoutError as exc:
            raise ObjectStoreError(
                f"gcs {verb} of {resolved} timed out after {self._timeout_s}s"
            ) from exc
        except gcs_exceptions.NotFound as exc:
            raise ObjectNotFound(f"no object at {resolved}") from exc
        except (gcs_exceptions.GoogleAPIError, auth_exceptions.GoogleAuthError, OSError) as exc:
            raise ObjectStoreError(
                f"gcs {verb} of {resolved} failed: {type(exc).__name__}"
            ) from exc
