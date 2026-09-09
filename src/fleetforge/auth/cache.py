"""A short-lived memo of argon2 verifications.

**This caches the hash comparison, not the authorization.** The only thing stored is
the fact that *this secret* matches *this token id's* stored hash. The database row
is still fetched on every authenticated request, and `revoked_at` / `expires_at` are
still checked on every authenticated request. That is what keeps revocation instant,
which is the entire reason `design/architecture.md` rejected JWT:

> "JWT ... costs instant revocation, which we do need on a public-facing API. A DB
> lookup per request is free at this scale."

A future "optimisation" that caches an `AuthContext` — or anything else derived from
the row — silently breaks that property. Do not.

Why it exists at all: without it every authenticated request pays ~40 ms of CPU and
19 MiB of memory (`auth/hashing.py`), which an SSE-heavy dashboard would feel.
"""

import hashlib
import hmac
import time
import uuid

DEFAULT_TTL_S = 60.0

# One admin means one live entry in practice, but the key is attacker-influenced
# (any unknown-but-well-formed token id would allocate) — so it is bounded.
DEFAULT_MAX_ENTRIES = 256


def _digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()


class VerifiedSecretCache:
    """`{token_id: (sha256(secret), expiry_monotonic)}`, bounded and TTL'd.

    Not thread-safe by design: it is touched only from the event loop.
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self._entries: dict[uuid.UUID, tuple[bytes, float]] = {}
        self._max_entries = max_entries

    def check(self, token_id: uuid.UUID, secret: str) -> bool:
        """Has this exact secret already been verified against this token's hash?"""
        entry = self._entries.get(token_id)
        if entry is None:
            return False
        stored, expires_at = entry
        if expires_at <= time.monotonic():
            del self._entries[token_id]
            return False
        return hmac.compare_digest(stored, _digest(secret))

    def remember(self, token_id: uuid.UUID, secret: str, ttl: float = DEFAULT_TTL_S) -> None:
        """Memoize a successful argon2 verification for `ttl` seconds."""
        if token_id not in self._entries and len(self._entries) >= self._max_entries:
            oldest = next(iter(self._entries))  # dicts preserve insertion order
            del self._entries[oldest]
        self._entries[token_id] = (_digest(secret), time.monotonic() + ttl)

    def forget(self, token_id: uuid.UUID) -> None:
        """Drop any memo for `token_id`. Logout calls this; revocation does not need it."""
        self._entries.pop(token_id, None)
