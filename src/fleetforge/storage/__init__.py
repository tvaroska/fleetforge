"""Artifact object storage — the seam between the API and the bytes.

`R0-be-6` created this package. Nothing in R0 calls it: `R1-BE-1` (`POST /v1/artifact`)
puts, `R1-BE-2` signs a URL into the `stage` command, `R1-BE-3` gets, and R2's version
pruning deletes. The cheapest place to get artifact-URL authorization wrong is here, so
it ships whole rather than as a stub.

Import the Protocol, the errors and the key rules from here; the adapters live in
`storage/s3.py` (MinIO/S3) and `storage/gcs.py` (Google Cloud Storage) and are imported
**lazily** by `storage/factory.py`, so a process that never touches artifacts pays for
neither SDK. Same layout as `fleetforge.broker`.

Round-trip the configured backend with `just storage-check`
(`python -m fleetforge.storage selftest`); the operational half is
`docs/runbooks/artifact-storage.md`.
"""

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

__all__ = [
    "MAX_KEY_LEN",
    "SIGNED_URL_MAX_TTL_S",
    "ObjectKeyError",
    "ObjectNotFound",
    "ObjectStore",
    "ObjectStoreConfigError",
    "ObjectStoreError",
    "ObjectTooLarge",
    "resolve_key",
    "validate_prefix",
    "validate_ttl",
]
