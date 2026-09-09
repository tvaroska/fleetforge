"""Argon2id hashing for admin passwords and token secrets.

Two non-obvious constraints shape this module, both about the `api` container's
256 M limit (`docker-compose.yml` → `api.deploy.resources.limits.memory`):

1. **The library defaults do not fit.** `PasswordHasher()` defaults to
   `time_cost=3, memory_cost=65536 (64 MiB), parallelism=4`. `ARGON2` below is
   pinned to OWASP's second recommended argon2id profile — `t=2, m=19 MiB, p=1` —
   which is what a new hash is written with. Verification reads the parameters out
   of the stored PHC string, so hashes written under a different profile keep
   verifying if this constant ever changes.
2. **Concurrency multiplies the memory.** An argon2 call handed to Starlette's
   threadpool (default limiter: 40 threads) would peak at 40 x 19 MiB ~ 760 MiB and
   be OOM-killed. Every call therefore goes through `ARGON2_LIMITER`, an
   `anyio.CapacityLimiter(2)` — peak ~38 MiB.

**Never call the sync forms from a coroutine.** `hash`/`verify` are blocking C calls
of roughly 40 ms; on the event loop they stall every concurrent request, including
the SSE stream R0-be-5 adds and `spec/prd.md`'s "<= 2 s to reflect a state change".
Request handlers use `ahash_secret` / `averify_secret`.

**Never log a secret, a password or a PHC string.** A token *id* is the only part of
a credential that may appear in a log line.
"""

import anyio
import anyio.to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# OWASP argon2id profile #2 (t=2, m=19 MiB, p=1). Pinned in exactly one place.
ARGON2 = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, hash_len=32, salt_len=16)

# Two concurrent argon2 calls at 19 MiB each fit inside the api container's 256 M.
ARGON2_LIMITER = anyio.CapacityLimiter(2)

# Burned when a lookup finds no row, so "unknown id" and "wrong secret" cost the
# same. Computed once at import — it is a hash of a constant, not a credential.
DUMMY_HASH = ARGON2.hash("dummy")  # noqa: S106 - not a credential; a fixed timing-equaliser input
_DUMMY_SECRET = "dummy"  # noqa: S105 - see above


def hash_secret(secret: str) -> str:
    """Return an argon2id PHC string for `secret`. Blocking (~40 ms)."""
    return ARGON2.hash(secret)


def verify_secret(secret: str, phc: str) -> bool:
    """Return whether `secret` matches the PHC string `phc`. Blocking (~40 ms).

    A corrupt or non-argon2 `secret_hash` in the database must be a 401, never a
    500, so `InvalidHashError` is a mismatch like any other. Nothing is logged here:
    the inputs are a secret and a hash.
    """
    try:
        return ARGON2.verify(phc, secret)
    except (VerificationError, InvalidHashError):
        return False


async def ahash_secret(secret: str) -> str:
    """`hash_secret` off the event loop, under `ARGON2_LIMITER`."""
    return await anyio.to_thread.run_sync(hash_secret, secret, limiter=ARGON2_LIMITER)


async def averify_secret(secret: str, phc: str) -> bool:
    """`verify_secret` off the event loop, under `ARGON2_LIMITER`."""
    return await anyio.to_thread.run_sync(verify_secret, secret, phc, limiter=ARGON2_LIMITER)


async def dummy_verify() -> None:
    """Burn one verification so a missing row costs about what a wrong secret costs."""
    await averify_secret(_DUMMY_SECRET, DUMMY_HASH)
