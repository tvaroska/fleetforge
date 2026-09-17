"""Artifact object storage — the seam between the API and the bytes.

`R0-be-6` created this package. Nothing in R0 calls it: `R1-BE-1` (`POST /v1/artifact`)
puts, `R1-BE-3`'s public download endpoint redirects a device to a signed URL, and R2's
version pruning deletes. The cheapest place to get artifact-URL authorization wrong is
here, so it ships whole rather than as a stub.

Since R1-be-3 **`storage/urlcache.py::SignedUrlCache` is the only caller of
`signed_url`** in the application: signing is an IAM round trip on GCS (S0-infra-5), and
one place to sign is also one place to cache. `R1-BE-2`'s `stage` command now carries a
URL on *our* origin, signed by `fleetforge/artifact_urls.py`, not a store URL.

Import the Protocol, the errors and the key rules from here — including the frozen
content-addressed scheme in `storage/blobs.py` (`blob_key`, `parse_blob_key`,
`put_blob`, `BLOB_CACHE_CONTROL`), which is what makes `put`'s overwrite-is-safe
contract true. The adapters live in
`storage/s3.py` (MinIO/S3) and `storage/gcs.py` (Google Cloud Storage) and are imported
**lazily** by `storage/factory.py`, so a process that never touches artifacts pays for
neither SDK. Same layout as `fleetforge.broker`.

Round-trip the configured backend with `just storage-check`
(`python -m fleetforge.storage selftest`); the operational half is
`docs/runbooks/artifact-storage.md`.
"""

from fleetforge.storage.blobs import (
    BLOB_CACHE_CONTROL,
    BLOB_PREFIX,
    blob_key,
    digest_bytes,
    parse_blob_key,
    put_blob,
)
from fleetforge.storage.objectstore import (
    MAX_KEY_LEN,
    SIGNED_URL_MAX_TTL_S,
    ObjectKeyError,
    ObjectNotFound,
    ObjectStore,
    ObjectStoreConfigError,
    ObjectStoreError,
    ObjectTooLarge,
    resolve_key,
    validate_prefix,
    validate_ttl,
)
from fleetforge.storage.urlcache import SignedUrlCache

__all__ = [
    "BLOB_CACHE_CONTROL",
    "BLOB_PREFIX",
    "MAX_KEY_LEN",
    "SIGNED_URL_MAX_TTL_S",
    "ObjectKeyError",
    "ObjectNotFound",
    "ObjectStore",
    "ObjectStoreConfigError",
    "ObjectStoreError",
    "ObjectTooLarge",
    "SignedUrlCache",
    "blob_key",
    "digest_bytes",
    "parse_blob_key",
    "put_blob",
    "resolve_key",
    "validate_prefix",
    "validate_ttl",
]
