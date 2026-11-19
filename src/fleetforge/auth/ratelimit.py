"""A fixed-window failure counter for the login endpoint.

`design/architecture.md` → *Bootstrap*: "the login endpoint rate-limited — it is a
password endpoint on the public internet."

Two design notes:

* **Only failures stay counted.** A successful login releases its reserved slot so
  legitimate logins never lock out the operator, while concurrent bursts are gated
  synchronously via `acquire(key)` *before* any Argon2 or upstream Firebase work runs.
* **Two buckets.** A per-client bucket keyed on the client IP, plus a global
  backstop, because the client key comes from `X-Forwarded-For` and Traefik
  *appends* to that header rather than replacing it — so the key is spoofable and
  the per-client bucket alone is bypassable.
"""

import time
from collections import OrderedDict, deque
from collections.abc import Callable

GLOBAL_KEY = "*"

DEFAULT_MAX_KEYS = 1024


class FixedWindowLimiter:
    """Failure counters over a sliding `window_s`, per key and globally."""

    def __init__(
        self,
        per_key: int = 5,
        per_global: int = 30,
        window_s: float = 60.0,
        max_keys: int = DEFAULT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_key = per_key
        self._per_global = per_global
        self._window_s = window_s
        self._max_keys = max_keys
        self._clock = clock
        self._global_failures: deque[float] = deque()
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    def _bucket(self, key: str) -> deque[float]:
        if key == GLOBAL_KEY:
            return self._global_failures
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
        now = self._clock()
        for bucket_key in (key, GLOBAL_KEY):
            bucket = self._bucket(bucket_key)
            self._prune(bucket, now)
            if len(bucket) >= self._limit_for(bucket_key):
                return False
        return True

    def acquire(self, key: str) -> float | None:
        """Atomically check and reserve an attempt slot before awaiting password work.

        Returns the reservation timestamp if allowed, or `None` when rate-limited.
        Call `release(key, stamp)` on successful authentication (or pure upstream
        unavailability when no local password check failed).
        """
        now = self._clock()
        for bucket_key in (key, GLOBAL_KEY):
            bucket = self._bucket(bucket_key)
            self._prune(bucket, now)
            if len(bucket) >= self._limit_for(bucket_key):
                return None
        for bucket_key in (key, GLOBAL_KEY):
            self._bucket(bucket_key).append(now)
        return now

    def release(self, key: str, stamp: float) -> None:
        """Release a previously acquired slot after successful login."""
        for bucket_key in (key, GLOBAL_KEY):
            bucket = self._bucket(bucket_key)
            try:
                bucket.remove(stamp)
            except ValueError:
                pass

    def record_failure(self, key: str) -> None:
        """Count one failed attempt against both the client bucket and the global one."""
        now = self._clock()
        for bucket_key in (key, GLOBAL_KEY):
            bucket = self._bucket(bucket_key)
            self._prune(bucket, now)
            bucket.append(now)

    def retry_after(self, key: str) -> int:
        """Whole seconds until `key` is allowed again; at least 1."""
        now = self._clock()
        wait = 0.0
        for bucket_key in (key, GLOBAL_KEY):
            bucket = self._bucket(bucket_key)
            self._prune(bucket, now)
            if len(bucket) >= self._limit_for(bucket_key):
                wait = max(wait, bucket[0] + self._window_s - now)
        return max(1, int(wait) + 1)
