"""`python -m fleetforge.storage selftest` — round-trip the configured object store.

```
just storage-check                       # whichever backend the environment selects
just storage-check --backend gcs         # force one
just storage-check --ttl 5 --keep        # leave the object, short-lived URL
just storage-check --key '../escape.bin' # must fail with ObjectKeyError
just storage-check --blob                # the content-addressed path (S0-infra-4)
```

`--blob` is the T2 harness for the frozen key scheme: it writes through
`storage.blobs.put_blob`, so the key is `blobs/sha256/<the payload's own digest>`, and
it then checks the **`Cache-Control` the device's GET actually receives** rather than
trusting that the adapter sent one. A header asserted only in a unit test against a fake
bucket is a header nobody has ever seen on an object.

Both the T2 harness for `R0-be-6` and the ops answer to "is the artifact store actually
reachable from this container?" — run it inside the api container with
`docker compose exec -T api python -m fleetforge.storage selftest`.

**Nothing here prints a credential.** The backend, bucket and prefix are printed; the
access key, secret and key-file *contents* never are. The signed URL is printed on
purpose — it is short-lived by construction and it is the only way to check the host it
was signed against (`storage/s3.py`, property 1).

`urllib.request` rather than `httpx`: httpx is a dev dependency and this module ships in
the production image. Precedent for the argparse/stdout-is-the-result shape:
`auth/__main__.py`.
"""

import argparse
import asyncio
import hashlib
import secrets
import sys
import urllib.error
import urllib.request
import uuid

from fleetforge.config import Settings
from fleetforge.storage.blobs import BLOB_CACHE_CONTROL, blob_key, digest_bytes, put_blob
from fleetforge.storage.factory import create_object_store, select_backend
from fleetforge.storage.objectstore import ObjectNotFound, ObjectStore

# Big enough that a truncated read is visible, small enough to be free.
PAYLOAD_BYTES = 4096
FETCH_TIMEOUT_S = 30


def _step(message: str) -> None:
    """One line per step, to stdout, so the whole run reads as a transcript."""
    print(message, flush=True)


def _fetch(url: str) -> tuple[bytes, str | None]:
    """GET `url` with no credentials at all — the signature is the authorization.

    Returns the body and the object's `Cache-Control`, because the header the *device*
    receives is the only proof that the metadata reached the object.
    """
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as response:  # noqa: S310 - http(s) URL from the adapter
        return bytes(response.read()), response.headers.get("Cache-Control")


async def _check_signed_url(
    store: ObjectStore,
    local_store: ObjectStore | None,
    key: str,
    ttl_s: int,
    digest: str,
    expect_cache_control: str | None = None,
) -> None:
    """Sign a URL, print it, and prove it serves the right bytes.

    **The device-facing URL is deliberately not always fetchable from here.** Inside the
    compose network `S3_PUBLIC_ENDPOINT_URL` names the *host's* published MinIO port —
    that is the whole point (`storage/s3.py`, property 1), and `localhost:9000` inside
    the api container is the container's own loopback. So the two failure kinds are kept
    apart, because they mean opposite things:

    * an **HTTP** status (403 `Request has expired`, `SignatureDoesNotMatch`, 404) means
      the signature or the authorization is wrong — a hard failure, always;
    * a **connection** failure means this process cannot reach that endpoint, which is
      the expected and correct state in the container. It is reported, and then signing
      is still proven end-to-end by re-signing against the internally reachable endpoint
      (`local_store`) and fetching that. Fetch the printed URL from the host to check
      the device's half.
    """
    url = await store.signed_url(key, ttl_s=ttl_s)
    _step(f"url      {url}")

    try:
        downloaded, cache_control = _fetch(url)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"the signed URL was refused with HTTP {exc.code}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        if local_store is None:
            raise
        _step(
            f"warn     the signed URL's endpoint is not reachable from here ({exc.reason}). "
            "Expected inside the compose network: S3_PUBLIC_ENDPOINT_URL names a "
            "host-side endpoint on purpose. Fetch the URL above from the host."
        )
        downloaded, cache_control = _fetch(await local_store.signed_url(key, ttl_s=ttl_s))
        _step(f"fetch    {len(downloaded)} bytes over HTTP via the internal endpoint")
    else:
        _step(f"fetch    {len(downloaded)} bytes over HTTP")

    if hashlib.sha256(downloaded).hexdigest() != digest:
        raise RuntimeError("the signed URL served different bytes")
    _step("verify   the signed URL's bytes match the sha256 above")

    if expect_cache_control is not None:
        # What the device sees, read off the response — not what we believe we sent.
        _step(f"cache    cache-control: {cache_control}")
        if cache_control != expect_cache_control:
            raise RuntimeError(
                f"the object serves Cache-Control {cache_control!r}, "
                f"expected {expect_cache_control!r}"
            )


async def _round_trip(
    store: ObjectStore,
    local_store: ObjectStore | None,
    key: str,
    payload: bytes,
    ttl_s: int,
    keep: bool,
    *,
    blob: bool = False,
) -> None:
    """put → get → signed_url (fetched) → delete → ObjectNotFound → delete again.

    `blob=True` writes through `put_blob`, so the key is a function of the bytes and the
    object carries `BLOB_CACHE_CONTROL`; the signed-URL check then asserts that header.
    """
    digest = digest_bytes(payload)

    if blob:
        written = await put_blob(store, payload)
        if written != key:
            raise RuntimeError(f"put_blob wrote {written}, not the announced {key}")
        _step(f"put      {key} ({len(payload)} bytes, sha256={digest}, content-addressed)")
    else:
        await store.put(key, payload, content_type="application/octet-stream")
        _step(f"put      {key} ({len(payload)} bytes, sha256={digest})")

    fetched = await store.get(key)
    if hashlib.sha256(fetched).hexdigest() != digest:
        raise RuntimeError("get returned different bytes than put")
    _step(f"get      {len(fetched)} bytes, sha256 matches")

    await _check_signed_url(
        store,
        local_store,
        key,
        ttl_s,
        digest,
        BLOB_CACHE_CONTROL if blob else None,
    )

    if keep:
        _step(f"keep     {key} left in place (--keep)")
        return

    await store.delete(key)
    _step(f"delete   {key}")

    try:
        await store.get(key)
    except ObjectNotFound:
        _step("missing  get after delete raised ObjectNotFound, as the contract says")
    else:
        raise RuntimeError("get after delete returned bytes")

    await store.delete(key)
    _step("delete   second delete succeeded (idempotent)")


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()  # type: ignore[call-arg]  # values come from the environment
    if args.backend:
        settings = settings.model_copy(update={"object_store_backend": args.backend})

    backend = select_backend(settings)
    store = create_object_store(settings)
    # `describe` is the adapters' credential-free one-liner.
    _step(f"backend  {getattr(store, 'describe', backend)}")

    # A second store that signs against the endpoint this process uses for I/O, used
    # only when the device-facing one turns out to be unreachable from here — see
    # `_check_signed_url`. Only S3 has two endpoints; GCS URLs are always public.
    local_store: ObjectStore | None = None
    if backend == "s3" and settings.s3_public_endpoint_url not in (None, settings.s3_endpoint_url):
        local_store = create_object_store(
            settings.model_copy(update={"s3_public_endpoint_url": settings.s3_endpoint_url})
        )

    payload = secrets.token_bytes(PAYLOAD_BYTES)
    if args.blob:
        if args.key:
            # There is nothing to choose: a blob's key IS its digest. Accepting both
            # would invite a key that does not describe its own contents.
            raise ValueError("--key and --blob are mutually exclusive")
        key = blob_key(digest_bytes(payload))
    else:
        key = args.key or f"selftest/{uuid.uuid4().hex}.bin"
    ttl_s = args.ttl if args.ttl is not None else settings.signed_url_ttl_s
    _step(f"key      {key} (signed-URL ttl {ttl_s}s)")

    await _round_trip(store, local_store, key, payload, ttl_s, args.keep, blob=args.blob)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the round trip. Returns a process exit code; `SELFTEST OK` means success."""
    parser = argparse.ArgumentParser(prog="python -m fleetforge.storage")
    subparsers = parser.add_subparsers(dest="command", required=True)
    selftest = subparsers.add_parser(
        "selftest", help="put/get/signed_url/delete against the configured backend"
    )
    selftest.add_argument("--backend", choices=("s3", "gcs"), help="force a backend")
    selftest.add_argument("--key", help="object key to use instead of selftest/<uuid>.bin")
    selftest.add_argument(
        "--blob",
        action="store_true",
        help="write through put_blob at blobs/sha256/<digest> and check Cache-Control",
    )
    selftest.add_argument("--ttl", type=int, help="signed-URL lifetime in seconds")
    selftest.add_argument(
        "--keep", action="store_true", help="do not delete the object (leaves it for inspection)"
    )
    args = parser.parse_args(argv)

    try:
        code = asyncio.run(_run(args))
    except (ValueError, RuntimeError, OSError) as exc:
        # ObjectKeyError is a ValueError, every ObjectStore*Error is a RuntimeError, and
        # urllib's URLError/HTTPError are OSError subclasses — so an expired or refused
        # signed URL lands here too rather than as a traceback.
        # Printed with its type because "which failure was it" is the whole question.
        print(f"SELFTEST FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("SELFTEST OK")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
