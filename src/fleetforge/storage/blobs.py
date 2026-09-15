"""The content-addressed key scheme — frozen by S0-infra-4, before R1 writes an object.

`DECISIONS.md` 2026-09-14 → *one storage model for every image*: artifact bytes live at
one key derived from their own sha256, write-once, served `Cache-Control: immutable`.
`objectstore.py::put` justifies its overwrite semantics with "the same key always carries
the same bytes"; **this module is what makes that sentence true.**

**Keys are store-relative.** `design/artifacts.md` writes the layout as the absolute
object path `fleetforge/blobs/sha256/<hex>`; the `fleetforge/` half is the *store's*
prefix, applied by `resolve_key`, and must never appear in a key a caller hands to an
`ObjectStore`:

| | `Settings` | prefix | key handed to the store | resulting object |
|---|---|---|---|---|
| prod (GCS) | `gcs_prefix` | `fleetforge/` | `blobs/sha256/<hex>` | `gs://btvaroska/fleetforge/blobs/sha256/<hex>` |
| dev (MinIO) | `s3_prefix` | `""` (dedicated bucket) | `blobs/sha256/<hex>` | `fleetforge/blobs/sha256/<hex>` (bucket `fleetforge`) |

Hardcoding `fleetforge/` into `blob_key` would write `fleetforge/fleetforge/blobs/…` in
production and put dev and prod on two different layouts — so a `fleetforge/`-prefixed
key is *refused* here, and `tests/test_blob_keys.py::test_blob_key_is_store_relative` is
the regression guard.

**Write-once.** The key *is* the bytes, so a re-`put` of the same digest is a no-op by
construction and there is deliberately **no existence pre-check**: it costs a round trip
and it is a race (two uploaders of identical bytes), and losing that race is harmless.

**Never normalise, always reject** — `objectstore.py`'s rule, and `identity.py`'s before
it. Uppercase hex is *not* lowercased: `AB…` and `ab…` would be two objects holding one
artifact, the exact "two spellings of one object" failure the key rules exist to stop.

No SDK is imported here (same rule as `objectstore.py`), so the pure key rules can be
tested and reused without boto3 or google-cloud-storage.
"""

import hashlib
import re

from fleetforge.storage.objectstore import ObjectKeyError, ObjectStore

# Store-relative, never absolute — see the module docstring's table.
BLOB_PREFIX = "blobs/sha256/"

# Lowercase only, exactly 64. `\Z` and NOT `$`: in Python `$` also matches just before
# a trailing newline, so `^[0-9a-f]{64}$` accepts "<64 hex>\n" — a 65-character key with
# a control character in it. (The SQL CHECK in `db/models.py` writes `$`, which in
# POSIX regex is a true end-of-string and has no such behaviour.)
SHA256_HEX = re.compile(r"^[0-9a-f]{64}\Z")

# One year plus `immutable`: the bytes at this key can never change, because the key IS
# the bytes. Both adapters pass this through to the object metadata, so it is what a
# device's GET through a signed URL actually sees.
BLOB_CACHE_CONTROL = "public, max-age=31536000, immutable"


def digest_bytes(data: bytes) -> str:
    """The lowercase hex sha256 of `data` — the identity of a blob."""
    return hashlib.sha256(data).hexdigest()


def validate_digest(digest: str) -> str:
    """Return `digest` unchanged, or raise `ObjectKeyError`.

    `ObjectKeyError` rather than a new exception type: it is deliberately a `ValueError`
    and not an `ObjectStoreError`, so a caller that retries transport failures does not
    retry a digest that will never become valid.
    """
    if not SHA256_HEX.match(digest):
        # Deliberately NOT `digest.lower()`. A repaired digest is a second spelling of
        # one artifact, and the caller that produced it never learns it was wrong.
        raise ObjectKeyError("digest is not 64 lowercase hex characters")
    return digest


def blob_key(digest: str) -> str:
    """The store-relative key holding the bytes whose sha256 is `digest`.

    Returns `blobs/sha256/<hex>`; **never** contains `fleetforge/` — that is the store's
    prefix, joined on by `resolve_key`. See the module docstring.
    """
    return f"{BLOB_PREFIX}{validate_digest(digest)}"


def parse_blob_key(key: str) -> str:
    """The digest a blob key names, or `ObjectKeyError`. The inverse of `blob_key`.

    Accepts **only** `BLOB_PREFIX` followed by 64 lowercase hex characters: nothing
    before it (a `fleetforge/`-prefixed key is the store-relative bug of the module
    docstring), nothing after it (`…/app.bin` is a directory layout, not a blob), and no
    other digest algorithm. A `key.startswith(BLOB_PREFIX)` test alone accepts all three.
    """
    if not key.startswith(BLOB_PREFIX):
        raise ObjectKeyError(f"key is not under {BLOB_PREFIX!r}")
    return validate_digest(key[len(BLOB_PREFIX) :])


async def put_blob(
    store: ObjectStore,
    data: bytes,
    *,
    content_type: str = "application/octet-stream",
    expected_digest: str | None = None,
) -> str:
    """Store `data` at its own digest and return the key. Write-once by construction.

    The key is computed **from the bytes**, never from a caller-supplied digest: an
    upload path that trusts the client's digest is how a blob ends up at a key that lies
    about its own contents. `expected_digest` is therefore a *check* — when it is given
    and disagrees, this raises `ObjectKeyError` before any backend call is made.

    No existence pre-check, on purpose: the same key always carries the same bytes, so
    re-putting is a no-op, and checking first is a round trip plus a race.
    """
    digest = digest_bytes(data)
    if expected_digest is not None and validate_digest(expected_digest) != digest:
        raise ObjectKeyError(
            f"data hashes to {digest}, not the expected {expected_digest} — refusing to "
            "store bytes under a key that does not describe them"
        )
    key = blob_key(digest)
    await store.put(key, data, content_type=content_type, cache_control=BLOB_CACHE_CONTROL)
    return key
