"""Google Cloud Storage — the production `ObjectStore`. **CRITICAL** (signing, key file).

`DECISIONS.md` 2026-09-08 → *Artifacts in GCS, MinIO for self-hosting*: artifact bytes
live in `gs://btvaroska/fleetforge/`, served to devices by a V4 signed URL without ever
touching the API process.

Three properties that are easy to get wrong:

1. **The credential is a key file or an impersonated service account, never plain ADC.**
   `factory.py` requires exactly one of `GCS_CREDENTIALS_FILE` /
   `GCS_IMPERSONATE_SERVICE_ACCOUNT` and refuses to construct this adapter without one —
   plain ADC in prod is the estate's shared VM account, which can read
   `gs://btvaroska/secrets/` and would make the prefix IAM condition decorative, and it
   carries no private key so V4 signing would silently become an IAM `signBlob` call by
   the wrong identity. This adapter holds only an opaque `bucket_factory` and **cannot
   tell which credential is behind it**, which is why property 4 has one code path.
2. **`gs://btvaroska` is shared** with this estate's `secrets/` backups and the `boris`
   podcast audio. `validate_prefix` runs in the constructor and `resolve_key` runs on
   every verb; `fleetforge-artifacts@btvaroska` — whether reached by key file or by
   impersonation — is additionally bound by an IAM condition to
   `…/objects/fleetforge/…` (`docs/runbooks/artifact-storage.md`). Two confinements, and
   the in-process one is not a substitute for the IAM one.
3. **`delete` of a missing blob raises `NotFound` here and does not on S3.** The
   idempotency lives in the Protocol contract, so this adapter swallows it rather than
   every caller learning which backend it is talking to.
4. **Signing is not local CPU any more, and neither is getting a `Bucket`.** Under
   impersonation `generate_signed_url(version="v4")` POSTs to the IAM `signBlob`
   endpoint, and the first `bucket_factory()` call resolves ADC and mints a token. Both
   are synchronous HTTPS round trips with no timeout of their own, so **every** verb
   runs both through `asyncio.to_thread` inside `_guard` — one path, key file or not.

`google-cloud-storage` is synchronous: every network call goes through
`asyncio.to_thread` under an `asyncio.timeout`.
"""

import asyncio
import contextlib
import datetime as dt
import functools
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
        credential_mode: str = "key-file",
    ) -> None:
        self._bucket_factory = bucket_factory
        self._bucket = bucket
        self._prefix = validate_prefix(prefix)
        self._default_ttl_s = default_ttl_s
        self._max_get_bytes = max_get_bytes
        self._timeout_s = timeout_s
        self._credential_mode = credential_mode

    @property
    def describe(self) -> str:
        """A one-line, **credential-free** description for logs and the selftest.

        `credential_mode` is `key-file` or `impersonated(<email>)`. A service-account
        *email* is not a credential and is exactly what proves no key file was used; a
        path, a token or key content would be, and none of them appear here.
        """
        return (
            f"gcs bucket={self._bucket} prefix={self._prefix or '(none)'} "
            f"creds={self._credential_mode}"
        )

    def _blob(self, resolved: str) -> Any:
        """Bucket + blob. Cheap after the first call; the FIRST call may do network I/O
        (impersonation resolves ADC and mints a token), which is why every caller runs it
        in a thread inside `_guard`."""
        return self._bucket_factory().blob(resolved)

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
    ) -> None:
        """Store `data` at `key`, overwriting. See the Protocol for the contract."""
        resolved = resolve_key(self._prefix, key)
        async with self._guard("put", resolved):
            blob = await asyncio.to_thread(self._blob, resolved)
            if cache_control is not None:
                # Set on the blob *before* the upload: `upload_from_string` writes the
                # object's metadata in the same request, so assigning it afterwards would
                # need a second `patch` call — and would leave a window where a device's
                # GET sees no caching headers at all.
                blob.cache_control = cache_control
            await asyncio.to_thread(blob.upload_from_string, data, content_type=content_type)

    async def get(self, key: str) -> bytes:
        """Return the bytes at `key`, refusing an oversized object before downloading."""
        resolved = resolve_key(self._prefix, key)
        async with self._guard("get", resolved):
            blob = await asyncio.to_thread(self._blob, resolved)
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
        """A V4 signed GET URL. **Not local CPU — it may be a network call.**

        With a key file the signature is computed locally. Under impersonation
        `generate_signed_url` calls the IAM `signBlob` API over HTTPS, with its own retry
        loop and no timeout, and this adapter cannot tell the two apart: it holds an
        opaque `bucket_factory`. So there is one path, and it is the safe one — into a
        thread, under `_guard`'s timeout. `validate_ttl` stays outside the guard: a bad
        TTL is a programming error and must still raise synchronously.
        """
        resolved = resolve_key(self._prefix, key)
        ttl = validate_ttl(self._default_ttl_s if ttl_s is None else ttl_s)
        async with self._guard("signed_url", resolved):
            blob = await asyncio.to_thread(self._blob, resolved)
            url = await asyncio.to_thread(
                functools.partial(
                    blob.generate_signed_url,
                    version="v4",
                    expiration=dt.timedelta(seconds=ttl),
                    method="GET",
                )
            )
        return str(url)

    async def delete(self, key: str) -> None:
        """Remove `key`. Idempotent — GCS raises `NotFound`, which is swallowed here."""
        resolved = resolve_key(self._prefix, key)
        try:
            async with self._guard("delete", resolved):
                blob = await asyncio.to_thread(self._blob, resolved)
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
