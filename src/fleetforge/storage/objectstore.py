"""The artifact object-store seam: one Protocol, two adapters. **CRITICAL.**

`CRITICAL.md` → *Artifact signing keys & `signed_url` generation*: "Signature *is* the
authorization for artifact download." `design/architecture.md` → *API surface &
authentication* lists three credentials, and the third one is generated here — a device
downloading firmware presents no token at all, only a URL whose signature is the whole
of its authority.

**The prefix is a security boundary, not tidiness.** `gs://btvaroska` is a *shared*
bucket: it already holds this estate's `secrets/` `.env` backups, the `boris` podcast
audio under `podcasts/`/`audio/`, `backup/` and `production/`. Fleetforge owns exactly
one prefix, `fleetforge/`, and a key reaches `resolve_key` from an HTTP request body
(R1-BE-1's upload endpoint). So there are two independent confinements and both must
exist: `resolve_key` below, and an IAM condition on the service-account key limiting it
to `…/objects/fleetforge/…` (`docs/runbooks/artifact-storage.md`). Neither alone is
enough — the first is defeated by a future caller that bypasses it, the second is what
still holds when that happens.

**Never normalise, always reject** — `fleetforge/identity.py`'s rule, for the same
reason. A key that is repaired (`.lstrip("/")`, `posixpath.normpath`) is a string two
readers can reason about differently: the adapter stores one thing and the operator
reading a bucket listing sees another.

Two adapters, the same local-vs-real shape as `broker/provisioner.py`:

* `S3ObjectStore` (`storage/s3.py`) — MinIO in the dev stack, any S3 in V2 self-hosting.
* `GcsObjectStore` (`storage/gcs.py`) — `gs://btvaroska/fleetforge/` in production.

Selection lives in `storage/factory.py`; **no SDK is imported in this module**, so the
pure key rules can be tested (and reused) without boto3 or google-cloud-storage.
"""

from typing import Protocol

# A key longer than this is a caller bug, and it is well under both backends' limits
# (S3 1024 bytes, GCS 1024 bytes) even after the prefix is joined on.
MAX_KEY_LEN = 512

# Both backends refuse a longer presigned lifetime at redemption time (S3 SigV4 and
# GCS V4 both cap at 7 days). Raise rather than emit a URL that 400s on the device.
SIGNED_URL_MAX_TTL_S = 7 * 24 * 3600


class ObjectStoreError(RuntimeError):
    """The backend could not be reached or refused the operation. Always a 503.

    Retriable by contract: a caller may try again. Everything the transport can throw
    — botocore's `ClientError`/`BotoCoreError`, google's `GoogleAPIError`, `OSError`,
    `TimeoutError` — is converted to this by the adapters, exactly as
    `DynsecProvisioner.ensure_client` converts `aiomqtt.MqttError`.
    """


class ObjectNotFound(ObjectStoreError):
    """`get` on a key the store does not hold. A 404, never a retry."""


class ObjectTooLarge(ObjectStoreError):
    """`get` on an object bigger than `object_get_max_bytes`.

    Checked from object metadata *before* any bytes are downloaded: the API container
    is capped at 256 M and `get` exists for checksum/signature verification of a
    ≤ 1.9 MB artifact (`spec/prd.md` → *Capacity*), not for streaming.
    """


class ObjectKeyError(ValueError):
    """The key cannot be placed inside the configured prefix.

    **Deliberately a `ValueError` and NOT an `ObjectStoreError`.** A caller that retries
    transport failures must not retry a key that will never become valid, and a caller
    that maps `ObjectStoreError` to 503 must not answer 503 for what is either its own
    bug or a 4xx.
    """


class ObjectStoreConfigError(RuntimeError):
    """No backend configured, both configured, or a required credential is missing.

    Raised at construction/selection time, never mid-request-body. `api/deps.py` turns
    it into a 503 that says what is missing (server-side log only).
    """


class ObjectStore(Protocol):
    """Artifact bytes, four verbs. `design/architecture.md` → *Artifact storage*."""

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        """Store `data` at `key`, **overwriting** any existing object.

        Overwrite rather than error-on-exists because R1-BE-1 content-addresses
        artifacts by sha256: the same key always carries the same bytes, so a retried
        upload is a no-op rather than a conflict to reason about.

        Raises `ObjectKeyError` for a key that cannot be confined to the prefix, and
        `ObjectStoreError` for anything the backend does wrong.
        """
        ...

    async def get(self, key: str) -> bytes:
        """Return the bytes at `key`.

        **Raises `ObjectNotFound`; never returns `None`.** An `Optional[bytes]` here
        becomes an `if data:` at the call site that treats a missing artifact as an
        empty one — i.e. a deploy that ships zero bytes of firmware.

        Raises `ObjectTooLarge` when the stored object exceeds the configured cap,
        before downloading it.
        """
        ...

    async def signed_url(self, key: str, *, ttl_s: int | None = None) -> str:
        """A short-lived URL that authorizes **GET of this one object**.

        The signature *is* the authorization (`spec/prd.md` → *Security & data
        posture*), which is why there is no presigned-upload verb: uploads go through
        `POST /v1/artifact` (R1-BE-1) so size and content validation cannot be
        bypassed.

        `ttl_s=None` means `settings.signed_url_ttl_s`. A TTL over
        `SIGNED_URL_MAX_TTL_S` raises `ValueError` rather than producing a URL that
        both backends reject at redemption.

        `async` even though signing is local CPU with no I/O: a future backend that
        signs through IAM `signBlob` must not be a Protocol change.
        """
        ...

    async def delete(self, key: str) -> None:
        """Remove `key`. **Idempotent** — deleting what is not there succeeds.

        Same reasoning as `BrokerProvisioner.ensure_client`'s idempotency: the retry
        path must not fail. The backends disagree (S3 returns 204, GCS raises
        `NotFound`), so the contract lives here and the adapters absorb the difference
        rather than every caller doing it.
        """
        ...


def _reject_characters(value: str, what: str) -> None:
    """Shared character rules for a key and for a prefix. Rejects, never repairs."""
    for char in value:
        if ord(char) < 0x20 or ord(char) == 0x7F:
            raise ObjectKeyError(f"{what} contains a control character")
        if ord(char) > 0x7F:
            # Non-ASCII is legal in S3/GCS but arrives percent-encoded in the signed
            # URL and normalises differently (NFC vs NFD) between two readers.
            raise ObjectKeyError(f"{what} contains a non-ASCII character")
    if "\\" in value:
        # Not a separator to S3/GCS, but it is to a Windows reader and to some
        # tooling — two spellings of one object is the whole problem.
        raise ObjectKeyError(f"{what} contains a backslash")
    if "?" in value or "#" in value:
        # Both terminate the path component of the signed URL that carries this key.
        raise ObjectKeyError(f"{what} contains '?' or '#'")


def validate_prefix(prefix: str) -> str:
    """Return `prefix` unchanged, or raise `ObjectKeyError`.

    Either empty (a dedicated bucket, as in the dev MinIO) or a relative,
    slash-terminated path. Called from every adapter's constructor, so a misconfigured
    `GCS_PREFIX` fails at startup rather than writing next to `secrets/`.
    """
    if prefix == "":
        return prefix
    if prefix.startswith("/"):
        raise ObjectKeyError("prefix must not start with '/'")
    if not prefix.endswith("/"):
        raise ObjectKeyError("prefix must end with '/'")
    if "//" in prefix:
        raise ObjectKeyError("prefix contains an empty segment")
    _reject_characters(prefix, "prefix")
    if any(segment in (".", "..") for segment in prefix.split("/")):
        raise ObjectKeyError("prefix contains a '.' or '..' segment")
    return prefix


def resolve_key(prefix: str, key: str) -> str:
    """Join `key` onto `prefix`, or raise `ObjectKeyError`. **Never normalises.**

    This is the in-process half of the two confinements described in the module
    docstring. Escaping the prefix must be *structurally impossible* — every rule below
    rejects, none repairs, and the result is asserted to start with `prefix` afterwards
    as belt and braces.
    """
    if not key:
        raise ObjectKeyError("key is empty")
    if len(key) > MAX_KEY_LEN:
        raise ObjectKeyError(f"key is longer than {MAX_KEY_LEN} characters")
    if key.startswith("/"):
        raise ObjectKeyError("key must be relative (no leading '/')")
    if "//" in key:
        # GCS accepts an empty segment and it makes two spellings of one object.
        raise ObjectKeyError("key contains an empty segment ('//')")
    _reject_characters(key, "key")
    if any(segment in (".", "..") for segment in key.split("/")):
        raise ObjectKeyError("key contains a '.' or '..' segment")

    resolved = f"{prefix}{key}"
    if not resolved.startswith(prefix):  # pragma: no cover - unreachable by construction
        raise ObjectKeyError("resolved key escapes the configured prefix")
    return resolved


def validate_ttl(ttl_s: int) -> int:
    """Return `ttl_s`, or raise `ValueError` if it is not a usable signed-URL lifetime."""
    if ttl_s <= 0:
        raise ValueError("signed-URL TTL must be positive")
    if ttl_s > SIGNED_URL_MAX_TTL_S:
        raise ValueError(
            f"signed-URL TTL {ttl_s}s exceeds the {SIGNED_URL_MAX_TTL_S}s both backends allow"
        )
    return ttl_s
