"""A fixed-window failure counter for the login endpoint.

`design/architecture.md` → *Bootstrap*: "the login endpoint rate-limited — it is a
password endpoint on the public internet."

Two design notes:

* **Only failures are counted.** A successful login already required the password,
  so it is not the attack being limited; counting successes would only lock out the
  one legitimate operator. Both buckets are consulted *before* any argon2 work, so a
  flood costs no CPU.
* **Two buckets.** A per-client bucket keyed on the client IP, plus a global
  backstop, because the client key comes from `X-Forwarded-For` and Traefik
  *appends* to that header rather than replacing it — so the key is spoofable and
  the per-client bucket alone is bypassable.

**Per-process, therefore per-uvicorn-worker:** with N workers the effective limit is
N x `per_key`. v1 runs one worker per container (`Dockerfile` `CMD`, no `--workers`).
A shared limiter means Redis or a Postgres table, which nobody has asked for.
"""

import time
from collections import OrderedDict, deque

GLOBAL_KEY = "*"

# Bounded so a spoofed X-Forwarded-For cannot grow this dictionary without limit.
DEFAULT_MAX_KEYS = 1024


class FixedWindowLimiter:
    """Failure counters over a sliding `window_s`, per key and globally."""

    def __init__(
        self,
        per_key: int = 5,
        per_global: int = 30,
        window_s: float = 60.0,
        max_keys: int = DEFAULT_MAX_KEYS,
    ) -> None:
        self._per_key = per_key
        self._per_global = per_global
        self._window_s = window_s
        self._max_keys = max_keys
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    def _bucket(self, key: str) -> deque[float]:
        bucket = self._failures.get(key)
        if bucket is None:
            if len(self._failures) >= self._max_keys:
                self._failures.popitem(last=False)
            bucket = deque()
            self._failures[key] = bucket
        return bucket

    def _prune(self, bucket: deque[float], now: float) -> None:
        cutoff = now - self._window_s
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    def _limit_for(self, key: str) -> int:
        return self._per_global if key == GLOBAL_KEY else self._per_key

    def allow(self, key: str) -> bool:
        """Is another login attempt from `key` permitted right now?"""
        now = time.monotonic()
        for bucket_key in (key, GLOBAL_KEY):
            bucket = self._bucket(bucket_key)
            self._prune(bucket, now)
            if len(bucket) >= self._limit_for(bucket_key):
                return False
        return True

    def record_failure(self, key: str) -> None:
        """Count one failed attempt against both the client bucket and the global one."""
        now = time.monotonic()
        for bucket_key in (key, GLOBAL_KEY):
            bucket = self._bucket(bucket_key)
            self._prune(bucket, now)
            bucket.append(now)

    def retry_after(self, key: str) -> int:
        """Whole seconds until `key` is allowed again; at least 1."""
        now = time.monotonic()
        wait = 0.0
        for bucket_key in (key, GLOBAL_KEY):
            bucket = self._bucket(bucket_key)
            self._prune(bucket, now)
            if len(bucket) >= self._limit_for(bucket_key):
                # The oldest failure in the bucket is the one that has to age out.
                wait = max(wait, bucket[0] + self._window_s - now)
        return max(1, int(wait) + 1)
