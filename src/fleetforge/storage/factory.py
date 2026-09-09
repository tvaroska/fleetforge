"""Which object store, built from `Settings`. **CRITICAL** (credentials, ambiguity).

The SDKs are imported **inside** the branch that needs them, so a process that never
touches artifacts pays for neither, and `create_app()` stays cheap in a 256 M container
(the `api/deps.get_broker_provisioner` precedent).

Two rules that are the whole point of this module:

* **Ambiguous configuration is refused, not resolved.** Both `S3_*` and `GCS_*` set
  raises `ObjectStoreConfigError`. "Which bucket did my firmware go to?" must never be
  answered by reading a precedence rule in a factory.
* **Empty strings count as unset.** Compose interpolation of an unset variable yields
  `""`, not absence — the same trap `api/deps.dynsec_configured` documents. Truthiness
  is the check; `is not None` is the bug.

Clients are built **once** per store and captured in the factory closure: a boto3 client
costs ~100 ms to construct and a GCS client reads a key file off disk. Both are
thread-safe for this usage, and `asyncio.to_thread` is what actually calls them.
"""

import logging
from typing import Any

from fleetforge.config import Settings
from fleetforge.storage.objectstore import ObjectStore, ObjectStoreConfigError

logger = logging.getLogger(__name__)


def s3_configured(settings: Settings) -> bool:
    """Is an S3/MinIO endpoint configured? Empty strings count as unset."""
    return bool(settings.s3_endpoint_url) and bool(settings.s3_bucket)


def gcs_configured(settings: Settings) -> bool:
    """Is a GCS bucket configured? The credentials file is checked at construction."""
    return bool(settings.gcs_bucket)


def object_store_configured(settings: Settings) -> bool:
    """Would `create_object_store` find exactly one backend?

    **Does no I/O and constructs no client** — `api/main.create_app()` calls this before
    anything is reachable, and a factory that dials a bucket at import time is a
    container that cannot start when the bucket is down.
    """
    if settings.object_store_backend == "s3":
        return s3_configured(settings)
    if settings.object_store_backend == "gcs":
        return gcs_configured(settings)
    return s3_configured(settings) != gcs_configured(settings)


def select_backend(settings: Settings) -> str:
    """`"s3"` or `"gcs"`, or raise `ObjectStoreConfigError` saying what is wrong."""
    explicit = settings.object_store_backend
    if explicit == "s3":
        if not s3_configured(settings):
            raise ObjectStoreConfigError(
                "OBJECT_STORE_BACKEND=s3 but S3_ENDPOINT_URL/S3_BUCKET are not both set"
            )
        return "s3"
    if explicit == "gcs":
        if not gcs_configured(settings):
            raise ObjectStoreConfigError("OBJECT_STORE_BACKEND=gcs but GCS_BUCKET is not set")
        return "gcs"

    if s3_configured(settings) and gcs_configured(settings):
        raise ObjectStoreConfigError(
            "both S3_* and GCS_* are configured; refusing to guess which bucket "
            "artifacts belong in. Unset one, or set OBJECT_STORE_BACKEND explicitly."
        )
    if s3_configured(settings):
        return "s3"
    if gcs_configured(settings):
        return "gcs"
    raise ObjectStoreConfigError(
        "no object-store backend configured; set S3_ENDPOINT_URL + S3_BUCKET (MinIO, "
        "the dev stack) or GCS_BUCKET + GCS_CREDENTIALS_FILE (production)"
    )


def build_s3_config() -> Any:
    """The botocore `Config` every S3 client in this project must be built with.

    `addressing_style="path"` because botocore's virtual-host default
    (`http://<bucket>.minio:9000/…`) does not resolve against MinIO, and SigV4 because
    that is what both MinIO and the presigned-URL format expect. `max_attempts=3` is
    botocore's own retry — there is deliberately no second retry loop on top.
    """
    from botocore.config import Config

    return Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        retries={"max_attempts": 3, "mode": "standard"},
    )


def _s3_store(settings: Settings) -> ObjectStore:
    """`S3ObjectStore` with two lazily-built, cached clients (I/O and presigning)."""
    import boto3

    from fleetforge.storage.s3 import S3ObjectStore

    if not settings.s3_access_key or not settings.s3_secret_key:
        raise ObjectStoreConfigError("S3_ACCESS_KEY / S3_SECRET_KEY are not both set")

    session = boto3.session.Session()
    config = build_s3_config()
    clients: dict[str, Any] = {}

    def client_for(endpoint_url: str) -> Any:
        """One client per endpoint, built on first use and kept."""
        existing = clients.get(endpoint_url)
        if existing is None:
            existing = session.client(
                "s3",
                endpoint_url=endpoint_url,
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
                region_name=settings.s3_region,
                config=config,
            )
            clients[endpoint_url] = existing
        return existing

    io_endpoint = str(settings.s3_endpoint_url)
    # The device-reachable endpoint. Same value by default, which is correct on the
    # host and WRONG inside the compose network — see storage/s3.py, property 1.
    public_endpoint = settings.s3_public_endpoint_url or io_endpoint

    return S3ObjectStore(
        lambda: client_for(io_endpoint),
        lambda: client_for(public_endpoint),
        bucket=str(settings.s3_bucket),
        prefix=settings.s3_prefix,
        default_ttl_s=settings.signed_url_ttl_s,
        max_get_bytes=settings.object_get_max_bytes,
        timeout_s=settings.object_store_timeout_s,
    )


def _gcs_store(settings: Settings) -> ObjectStore:
    """`GcsObjectStore` from a service-account key file. **Never falls back to ADC.**

    ADC on a GCE VM carries no private key, so V4 signing would silently need an IAM
    `signBlob` round trip; worse, it would pick up the project-wide compute default
    service account instead of the prefix-scoped one. Both failures show up for the
    first time on a real device, so this raises at construction instead.
    """
    from pathlib import Path

    from google.cloud import storage as gcs

    from fleetforge.storage.gcs import GcsObjectStore

    key_file = settings.gcs_credentials_file
    if not key_file:
        raise ObjectStoreConfigError(
            "GCS_CREDENTIALS_FILE is required for the GCS backend: Application Default "
            "Credentials cannot sign a V4 URL and would use the wrong service account. "
            "See docs/runbooks/artifact-storage.md."
        )
    if not Path(key_file).is_file():
        raise ObjectStoreConfigError(f"GCS_CREDENTIALS_FILE does not exist: {key_file}")

    bucket_name = str(settings.gcs_bucket)
    bucket: Any = None

    def bucket_factory() -> Any:
        """One client and one `Bucket`, built on first use and kept."""
        nonlocal bucket
        if bucket is None:
            client = gcs.Client.from_service_account_json(key_file)
            bucket = client.bucket(bucket_name)
        return bucket

    return GcsObjectStore(
        bucket_factory,
        bucket=bucket_name,
        prefix=settings.gcs_prefix,
        default_ttl_s=settings.signed_url_ttl_s,
        max_get_bytes=settings.object_get_max_bytes,
        timeout_s=settings.object_store_timeout_s,
    )


def create_object_store(settings: Settings) -> ObjectStore:
    """The configured `ObjectStore`, or `ObjectStoreConfigError` saying what is missing."""
    backend = select_backend(settings)
    if backend == "s3":
        return _s3_store(settings)
    return _gcs_store(settings)
