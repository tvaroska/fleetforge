"""Unit tests for the credential primitives — no HTTP, no database.

These are pure and fast; the HTTP-level behaviour they underpin lives in
`tests/test_api_auth.py`.
"""

import time
import uuid

from fleetforge.auth.cache import VerifiedSecretCache
from fleetforge.auth.hashing import hash_secret, verify_secret
from fleetforge.auth.ratelimit import GLOBAL_KEY, FixedWindowLimiter
from fleetforge.auth.tokens import (
    ADMIN_TOKEN_PREFIX,
    ENROLLMENT_TOKEN_PREFIX,
    MAX_TOKEN_LENGTH,
    issue_token,
    parse_token,
)


def test_token_roundtrip() -> None:
    """A minted token parses back to its own id and a secret its hash verifies."""
    issued = issue_token(ADMIN_TOKEN_PREFIX)

    parts = parse_token(issued.token, ADMIN_TOKEN_PREFIX)

    assert parts is not None
    assert parts.token_id == issued.token_id
    assert parts.prefix == ADMIN_TOKEN_PREFIX
    assert verify_secret(parts.secret, issued.secret_hash)
    assert issued.token.startswith("ffa_")
    # The plaintext secret is not recoverable from what gets stored.
    assert parts.secret not in issued.secret_hash


def test_parse_rejects_wrong_prefix() -> None:
    """An enrollment token must never authenticate an admin request."""
    issued = issue_token(ENROLLMENT_TOKEN_PREFIX)

    assert parse_token(issued.token, ADMIN_TOKEN_PREFIX) is None
    assert parse_token(issued.token, ENROLLMENT_TOKEN_PREFIX) is not None


def test_parse_rejects_garbage() -> None:
    """`parse_token` is total: garbage in, `None` out, never an exception."""
    token_id = uuid.uuid4().hex
    garbage = [
        None,
        "",
        "ffa_",
        "ffa_zz.zz",
        "ffa_" + token_id,  # no dot
        f"ffa_{token_id}.",  # empty secret
        f"ffa{token_id}.secret",  # no underscore
        f"ffa_{token_id[:31]}.secret",  # short uuid
        f"ffa_{token_id}.secret".replace("ffa", "ffe"),  # wrong prefix
        "x" * (MAX_TOKEN_LENGTH + 1),
        f"ffa_{token_id}." + "x" * 10_000,  # oversized secret
    ]
    for raw in garbage:
        assert parse_token(raw, ADMIN_TOKEN_PREFIX) is None, raw


def test_hash_is_argon2id_phc() -> None:
    """New hashes carry the pinned OWASP profile and a fresh salt each time."""
    first = hash_secret("hunter2")
    second = hash_secret("hunter2")

    assert first.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert first != second
    assert verify_secret("hunter2", first)
    assert not verify_secret("hunter3", first)


def test_verify_rejects_corrupt_hash() -> None:
    """A corrupt `secret_hash` is a 401's worth of `False`, not a 500's worth of raise."""
    assert verify_secret("x", "not-a-phc-string") is False
    assert verify_secret("x", "") is False
    assert verify_secret("x", "$argon2id$v=19$m=19456,t=2,p=1$truncated") is False


def test_cache_hit_and_miss() -> None:
    """The cache answers only for the exact secret, only before its TTL."""
    cache = VerifiedSecretCache()
    token_id = uuid.uuid4()

    assert cache.check(token_id, "s3cret") is False

    cache.remember(token_id, "s3cret", ttl=60)
    assert cache.check(token_id, "s3cret") is True
    assert cache.check(token_id, "s3crev") is False
    assert cache.check(uuid.uuid4(), "s3cret") is False

    cache.forget(token_id)
    assert cache.check(token_id, "s3cret") is False

    cache.remember(token_id, "s3cret", ttl=-1)
    assert cache.check(token_id, "s3cret") is False


def test_cache_is_bounded() -> None:
    """The key is attacker-influenced, so the cache evicts rather than grows."""
    cache = VerifiedSecretCache(max_entries=4)
    ids = [uuid.uuid4() for _ in range(8)]
    for token_id in ids:
        cache.remember(token_id, "s")

    assert sum(cache.check(token_id, "s") for token_id in ids) == 4
    assert cache.check(ids[-1], "s") is True
    assert cache.check(ids[0], "s") is False


def test_limiter_window() -> None:
    """N failures block the key; the window elapsing unblocks it."""
    limiter = FixedWindowLimiter(per_key=3, per_global=100, window_s=0.2)

    assert limiter.allow("10.0.0.1") is True
    for _ in range(3):
        limiter.record_failure("10.0.0.1")

    assert limiter.allow("10.0.0.1") is False
    assert limiter.retry_after("10.0.0.1") >= 1
    # Another client is unaffected by this one's failures.
    assert limiter.allow("10.0.0.2") is True

    time.sleep(0.25)
    assert limiter.allow("10.0.0.1") is True


def test_limiter_global_backstop() -> None:
    """The per-client key is spoofable, so a global bucket backs it up."""
    limiter = FixedWindowLimiter(per_key=100, per_global=3, window_s=60)

    for index in range(3):
        limiter.record_failure(f"10.0.0.{index}")

    assert limiter.allow("10.0.0.99") is False
    assert limiter.allow(GLOBAL_KEY) is False


def test_limiter_keys_are_bounded() -> None:
    """A spoofed X-Forwarded-For must not be an unbounded memory leak."""
    limiter = FixedWindowLimiter(per_key=1, per_global=10_000, window_s=60, max_keys=8)
    for index in range(64):
        limiter.record_failure(f"10.0.0.{index}")

    assert len(limiter._failures) <= 9  # 8 client keys + the global bucket
