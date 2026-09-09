"""Opaque token issuance and parsing.

**Wire format (`DECISIONS.md` 2026-09-08 → *Schema…*):**

```
{prefix}_{uuid-hex-32}.{secret-b64url}      e.g.  ffa_9f1c...c3.xQ7...
```

The UUID half is the row's primary key. It rides in the token because argon2 hashes
are salted and therefore not searchable — `WHERE secret_hash = argon2(input)` cannot
work, and scanning every row would cost one argon2 verification per row per request.
Only the secret half is verified against `secret_hash`; the plaintext is never
stored.

The prefix is a namespace, and it is **checked**: an enrollment token must never
authenticate an admin request, even though both are rows with the same shape. This
module is prefix-generic so R0-be-2 can reuse it for `ffe_` unchanged.
"""

import secrets
import uuid
from dataclasses import dataclass

import anyio.to_thread

from fleetforge.auth.hashing import ARGON2_LIMITER, hash_secret

ADMIN_TOKEN_PREFIX = "ffa"  # noqa: S105 - a namespace tag in the wire format, not a secret
ENROLLMENT_TOKEN_PREFIX = "ffe"  # noqa: S105 - same; reserved for R0-be-2

# 32 random bytes, base64url-encoded.
SECRET_BYTES = 32

# Nothing legitimate comes close; an unbounded Authorization header is otherwise
# free CPU for an attacker.
MAX_TOKEN_LENGTH = 512

_UUID_HEX_LENGTH = 32


@dataclass(frozen=True, slots=True)
class TokenParts:
    """The two halves of a parsed token: the row to look up, and what to verify."""

    prefix: str
    token_id: uuid.UUID
    secret: str


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A freshly minted token. `token` is the only time the plaintext exists."""

    token: str
    token_id: uuid.UUID
    secret_hash: str


def _format_token(prefix: str, token_id: uuid.UUID, secret: str) -> str:
    return f"{prefix}_{token_id.hex}.{secret}"


def issue_token(prefix: str) -> IssuedToken:
    """Mint a token for `prefix`: new UUID, 256-bit secret, argon2id hash.

    Blocking (it hashes, ~40 ms) — call `aissue_token` from a request handler.
    """
    token_id = uuid.uuid4()
    secret = secrets.token_urlsafe(SECRET_BYTES)
    return IssuedToken(
        token=_format_token(prefix, token_id, secret),
        token_id=token_id,
        secret_hash=hash_secret(secret),
    )


async def aissue_token(prefix: str) -> IssuedToken:
    """`issue_token` off the event loop. One code path: this calls `issue_token`."""
    return await anyio.to_thread.run_sync(issue_token, prefix, limiter=ARGON2_LIMITER)


def parse_token(raw: str | None, expected_prefix: str) -> TokenParts | None:
    """Split `raw` into its parts, or return `None`.

    A total function: it never raises and never distinguishes *why* a token is
    unusable — the caller's response is the same 401 either way. `None` covers an
    absent/wrong prefix, a missing separator, a malformed UUID, an empty secret and
    anything over `MAX_TOKEN_LENGTH`.
    """
    if not raw or len(raw) > MAX_TOKEN_LENGTH:
        return None

    prefix, separator, remainder = raw.partition("_")
    if not separator or prefix != expected_prefix:
        return None

    id_hex, separator, secret = remainder.partition(".")
    if not separator or not secret or len(id_hex) != _UUID_HEX_LENGTH:
        return None

    try:
        token_id = uuid.UUID(hex=id_hex)
    except ValueError:
        return None

    return TokenParts(prefix=prefix, token_id=token_id, secret=secret)
