"""MinIO / S3 via boto3 — the self-hostable `ObjectStore`. **CRITICAL** (signing).

The dev stack's backend, and the V2 turnkey self-hosting backend, behind the same
Protocol as GCS. Clients are injected as **factories** rather than instances, the same
move as `DynsecProvisioner(session_factory)`: it is what lets `tests/test_object_store.py`
drive `botocore.stub.Stubber` with no MinIO running.

Three properties that are easy to get wrong, all encoded below:

1. **A presigned URL signs the `Host` header.** A URL generated against
   `http://minio:9000` (the container-internal endpoint used for `put`/`get`) cannot be
   handed to a device outside the compose network, and rewriting the host afterwards
   invalidates the signature. Hence *two* client factories: one for I/O, one for
   presigning against `s3_public_endpoint_url`. The failure mode is invisible to every
   test you would think to write — it works from inside the network — so
   `tests/test_object_store.py` asserts the URL's host explicitly.
2. **MinIO needs path-style addressing.** botocore's default virtual-host style
   (`http://<bucket>.minio:9000/…`) does not resolve; `build_s3_config` pins
   `addressing_style="path"`. `region_name` is mandatory for SigV4 even against MinIO.
3. **boto3 is synchronous.** Every network call goes through `asyncio.to_thread` under
   an `asyncio.timeout`, so one slow backend cannot pin the event loop. `signed_url` is
   the exception — signing is local CPU, sub-millisecond, no I/O.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from fleetforge.storage.objectstore import (
    ObjectNotFound,
    ObjectStoreError,
    ObjectTooLarge,
    resolve_key,
    validate_prefix,
    validate_ttl,
)

logger = logging.getLogger(__name__)

# The `Error.Code` values S3 and MinIO use for "no such object". `404` is what a
# `head_object` returns (it has no body to carry `NoSuchKey`).
#
# `NoSuchBucket` is deliberately NOT here. A missing bucket is a misconfiguration, not
# a missing artifact: mapping it to `ObjectNotFound` would answer 404 for every key in
# a store that does not exist, and the operator would go looking for the firmware
# instead of for `S3_BUCKET`. It stays an `ObjectStoreError` (503), which is the signal
# that says "the backend is wrong", and there is a test for exactly that.
NOT_FOUND_CODES = frozenset({"NoSuchKey", "404", "NotFound"})

ClientFactory = Callable[[], Any]


def _error_code(exc: ClientError) -> str:
    """The `Error.Code` of a botocore `ClientError`, or `""` when it carries none."""
    response = getattr(exc, "response", None) or {}
    error = response.get("Error") or {}
    return str(error.get("Code", ""))


class S3ObjectStore:
    """`ObjectStore` backed by any S3-compatible endpoint (MinIO in the dev stack).

    `presign_client_factory` may be the same callable as `client_factory` when no
    separate public endpoint is configured — see the module docstring, property 1.
    """

    def __init__(
        self,
        client_factory: ClientFactory,
        presign_client_factory: ClientFactory,
        *,
        bucket: str,
        prefix: str,
        default_ttl_s: int,
        max_get_bytes: int,
        timeout_s: float,
    ) -> None:
        self._client_factory = client_factory
        self._presign_client_factory = presign_client_factory
        self._bucket = bucket
        self._prefix = validate_prefix(prefix)
        self._default_ttl_s = default_ttl_s
        self._max_get_bytes = max_get_bytes
        self._timeout_s = timeout_s

    @property
    def describe(self) -> str:
        """A one-line, **credential-free** description for logs and the selftest."""
        return f"s3 bucket={self._bucket} prefix={self._prefix or '(none)'}"

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        """Store `data` at `key`, overwriting. See the Protocol for the contract."""
        resolved = resolve_key(self._prefix, key)
        client = self._client_factory()
        async with self._guard("put", resolved):
            await asyncio.to_thread(
                client.put_object,
                Bucket=self._bucket,
                Key=resolved,
                Body=data,
                ContentType=content_type,
            )

    async def get(self, key: str) -> bytes:
        """Return the bytes at `key`, refusing an oversized object before downloading."""
        resolved = resolve_key(self._prefix, key)
        client = self._client_factory()
        async with self._guard("get", resolved):
            head = await asyncio.to_thread(client.head_object, Bucket=self._bucket, Key=resolved)
            size = int(head.get("ContentLength", 0))
            if size > self._max_get_bytes:
                raise ObjectTooLarge(
                    f"object {resolved} is {size} bytes, over the {self._max_get_bytes} cap"
                )
            response = await asyncio.to_thread(client.get_object, Bucket=self._bucket, Key=resolved)
            body = await asyncio.to_thread(response["Body"].read)
        return bytes(body)

    async def signed_url(self, key: str, *, ttl_s: int | None = None) -> str:
        """A presigned GET URL, signed against the **public** endpoint.

        No `to_thread`: SigV4 signing is local CPU with no I/O. The client used here is
        the presigning one, whose `endpoint_url` is what the device must be able to
        reach — see the module docstring, property 1.
        """
        resolved = resolve_key(self._prefix, key)
        ttl = validate_ttl(self._default_ttl_s if ttl_s is None else ttl_s)
        client = self._presign_client_factory()
        try:
            url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": resolved},
                ExpiresIn=ttl,
            )
        except (BotoCoreError, ClientError, OSError) as exc:
            raise ObjectStoreError(
                f"could not sign a URL for {resolved}: {type(exc).__name__}"
            ) from exc
        return str(url)

    async def delete(self, key: str) -> None:
        """Remove `key`. Idempotent — S3 answers 204 for a key that is not there."""
        resolved = resolve_key(self._prefix, key)
        client = self._client_factory()
        async with self._guard("delete", resolved):
            await asyncio.to_thread(client.delete_object, Bucket=self._bucket, Key=resolved)

    @contextlib.asynccontextmanager
    async def _guard(self, verb: str, resolved: str) -> AsyncIterator[None]:
        """Timeout plus exception translation for one backend call.

        Every transport exception becomes an `ObjectStoreError` (or `ObjectNotFound`),
        exactly as `DynsecProvisioner.ensure_client` converts `aiomqtt.MqttError` — a
        botocore exception escaping into a request handler is a 500 where a retriable
        503 belongs. **The message names the verb and the key, never the credentials.**
        """
        try:
            async with asyncio.timeout(self._timeout_s):
                yield
        except TimeoutError as exc:
            raise ObjectStoreError(
                f"s3 {verb} of {resolved} timed out after {self._timeout_s}s"
            ) from exc
        except ClientError as exc:
            code = _error_code(exc)
            if code in NOT_FOUND_CODES:
                raise ObjectNotFound(f"no object at {resolved}") from exc
            raise ObjectStoreError(
                f"s3 {verb} of {resolved} refused: {code or 'ClientError'}"
            ) from exc
        except (BotoCoreError, OSError) as exc:
            raise ObjectStoreError(f"s3 {verb} of {resolved} failed: {type(exc).__name__}") from exc
