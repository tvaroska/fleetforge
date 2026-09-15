"""Which object store, built from `Settings`. **CRITICAL** (credentials, ambiguity).

The SDKs are imported **inside** the branch that needs them, so a process that never
touches artifacts pays for neither, and `create_app()` stays cheap in a 256 M container
(the `api/deps.get_broker_provisioner` precedent).

Two rules that are the whole point of this module:

* **Ambiguous configuration is refused, not resolved.** Both `S3_*` and `GCS_*` set
  raises `ObjectStoreConfigError`, and so does an ambiguous **credential** — both
  `GCS_CREDENTIALS_FILE` and `GCS_IMPERSONATE_SERVICE_ACCOUNT` set. "Which bucket did my
  firmware go to?" and "which identity wrote it?" must never be answered by reading a
  precedence rule in a factory.
* **Empty strings count as unset.** Compose interpolation of an unset variable yields
  `""`, not absence — the same trap `api/deps.dynsec_configured` documents. Truthiness
  is the check; `is not None` is the bug.

Clients are built **once** per store and captured in the factory closure: a boto3 client
costs ~100 ms to construct and a GCS client reads a key file off disk (or, under
impersonation, resolves ADC and mints a token — network I/O, which is why that path is
built lazily inside the closure and never at construction). Both are thread-safe for this
usage, and `asyncio.to_thread` is what actually calls them.
"""

import logging
import re
from typing import Any

from fleetforge.config import Settings
from fleetforge.storage.objectstore import ObjectStore, ObjectStoreConfigError, ObjectStoreError

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
        "the dev stack) or GCS_BUCKET + one of GCS_CREDENTIALS_FILE / "
        "GCS_IMPERSONATE_SERVICE_ACCOUNT (production)"
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


# A service-account email and nothing else: `<id>@<project>.iam.gserviceaccount.com`,
# lowercase. A user principal (`boris@gmail.com`) cannot be impersonated for signing and
# is a different kind of identity entirely; a bare name is a typo. Both are REFUSED, never
# repaired — the same rule `resolve_key` and `blobs.parse_blob_key` follow.
_SA_EMAIL_RE = re.compile(r"\A[a-z0-9-]{1,63}@[a-z0-9-]{1,63}\.iam\.gserviceaccount\.com\Z")

# Storage read/write only — least privilege, and enough for put/get/delete. Signing needs
# no scope at all; it is the SOURCE credential that needs cloud-platform.
_IMPERSONATION_SCOPES = ["https://www.googleapis.com/auth/devstorage.read_write"]


def _impersonated_bucket(target: str, bucket_name: str) -> Any:
    """Build one client over impersonated credentials. Does network I/O — call lazily.

    Both the ADC lookup and the first token mint happen here, so every caller must run
    this in a thread under `GcsObjectStore._guard` (see `storage/gcs.py::_blob`).
    """
    import google.auth
    from google.auth import exceptions as auth_exceptions
    from google.auth import impersonated_credentials
    from google.auth.transport.requests import Request as AuthRequest
    from google.cloud import storage as gcs

    try:
        source, _project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    except auth_exceptions.DefaultCredentialsError as exc:
        # `ObjectStoreError`, NOT `ObjectStoreConfigError`: this runs mid-verb, and
        # `api/deps.get_object_store` only translates a config error around
        # `create_object_store`. A config error here escapes an already-started request
        # as a 500 where `objectstore.py` promises a retriable 503.
        raise ObjectStoreError(
            "GCS_IMPERSONATE_SERVICE_ACCOUNT needs Application Default Credentials to "
            "impersonate FROM (the VM's attached identity in prod, `gcloud auth "
            "application-default login` on a workstation); none were found"
        ) from exc

    creds = impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=target,
        target_scopes=_IMPERSONATION_SCOPES,
        lifetime=3600,
    )
    # Mint the first token HERE rather than letting the SDK do it on the next call. The
    # common failure is a runtime identity with no tokenCreator binding, which surfaces
    # lazily as a bare `RefreshError` — `gcs get of … failed: RefreshError` names neither
    # the principal nor the missing role. Cost is zero: the very next call would mint it.
    try:
        creds.refresh(AuthRequest())
    except auth_exceptions.RefreshError as exc:
        # The message names the target principal and the role, and nothing else — no
        # token, no ADC path, and not the 403 body (it carries an opaque troubleshooter
        # URL, fine in a log and not in an exception that may reach a handler).
        raise ObjectStoreError(
            f"could not impersonate {target}: the runtime identity was refused a token. "
            "Grant it roles/iam.serviceAccountTokenCreator on that service account "
            "(docs/runbooks/artifact-storage.md)."
        ) from exc

    # project=None on purpose: the bucket is cross-project, nothing here lists buckets,
    # and the default sentinel would send google-cloud-storage looking for a project via
    # ADC and raise when it cannot find one. `Client.__init__` maps None -> no project.
    return gcs.Client(project=None, credentials=creds).bucket(bucket_name)


def _gcs_store(settings: Settings) -> ObjectStore:
    """`GcsObjectStore` from a key file **or** an impersonated service account.

    **Never falls back to ADC**, and the reason is containment first, signing second.
    Prod's attached identity is `mainsite@sites-470716`, the shared VM account for the
    whole estate, which can read `gs://btvaroska/secrets/` — plain ADC would make the IAM
    prefix condition on `fleetforge-artifacts` decorative. Secondarily, ADC on a GCE VM
    carries no private key, so V4 signing would silently need an IAM `signBlob` round trip
    with the wrong service account. Both failures show up for the first time on a real
    device, so this raises at construction instead.

    Eager checks here are SHAPE checks only and raise `ObjectStoreConfigError`. Anything
    discovered when the credential is first resolved (no ADC, a refused token, a signBlob
    403) happens inside a verb and raises `ObjectStoreError` — see `_impersonated_bucket`.
    """
    from pathlib import Path

    from fleetforge.storage.gcs import GcsObjectStore

    key_file = settings.gcs_credentials_file
    target = (settings.gcs_impersonate_service_account or "").strip()

    if key_file and target:
        raise ObjectStoreConfigError(
            "GCS_CREDENTIALS_FILE and GCS_IMPERSONATE_SERVICE_ACCOUNT are mutually "
            "exclusive; set exactly one. Ambiguous credentials are refused, not resolved."
        )
    if not key_file and not target:
        raise ObjectStoreConfigError(
            "GCS_CREDENTIALS_FILE is required for the GCS backend, or "
            "GCS_IMPERSONATE_SERVICE_ACCOUNT instead of it: Application Default "
            "Credentials are never used directly, because prod's attached identity can "
            "read gs://btvaroska/secrets/ and cannot sign a V4 URL. "
            "See docs/runbooks/artifact-storage.md."
        )

    bucket_name = str(settings.gcs_bucket)
    bucket: Any = None

    if key_file:
        if not Path(key_file).is_file():
            raise ObjectStoreConfigError(f"GCS_CREDENTIALS_FILE does not exist: {key_file}")
        credential_mode = "key-file"

        def bucket_factory() -> Any:
            """One client and one `Bucket`, built on first use and kept."""
            nonlocal bucket
            if bucket is None:
                from google.cloud import storage as gcs

                bucket = gcs.Client.from_service_account_json(key_file).bucket(bucket_name)
            return bucket
    else:
        if not _SA_EMAIL_RE.match(target):
            raise ObjectStoreConfigError(
                f"GCS_IMPERSONATE_SERVICE_ACCOUNT is not a service-account email: {target!r}. "
                "Expected <name>@<project>.iam.gserviceaccount.com, lowercase."
            )
        credential_mode = f"impersonated({target})"

        def bucket_factory() -> Any:
            """One impersonated client and one `Bucket`, built on first use and kept.

            The first call resolves ADC and mints a token; the client refreshes it by
            itself afterwards, so this stays one credential per store.
            """
            nonlocal bucket
            if bucket is None:
                bucket = _impersonated_bucket(target, bucket_name)
            return bucket

    return GcsObjectStore(
        bucket_factory,
        bucket=bucket_name,
        prefix=settings.gcs_prefix,
        default_ttl_s=settings.signed_url_ttl_s,
        max_get_bytes=settings.object_get_max_bytes,
        timeout_s=settings.object_store_timeout_s,
        credential_mode=credential_mode,
    )


def create_object_store(settings: Settings) -> ObjectStore:
    """The configured `ObjectStore`, or `ObjectStoreConfigError` saying what is missing."""
    backend = select_backend(settings)
    if backend == "s3":
        return _s3_store(settings)
    return _gcs_store(settings)
