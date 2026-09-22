"""Upstream signed URLs, cached for their own lifetime. **CRITICAL.** R1-be-3.

**This is the only module in the application that calls `ObjectStore.signed_url`.**
`CRITICAL.md` → *Artifact signing keys & `signed_url` generation*; the tripwire in
`tests/test_api_deploy.py` holds it there.

Why a cache exists at all: since S0-infra-5 `signed_url` on GCS is an IAM `signBlob`
**network call**, not local CPU. The download endpoint hands the URL to a device that may
resume a 1.9 MB transfer over a marginal link, so a 10-range download that signed 10
times would be 10 Google API calls for one artifact — a rate-limit waiting for the day a
fleet resumes together. One signing call per artifact per cache lifetime, shared by the
whole fleet, is the budget.

Modelled on `firmware/catalog.py::CatalogCache`, deliberately: per app, **construction
does no I/O** (a container must start while the bucket is down), one `asyncio.Lock`, and
`time.monotonic()` for expiry so a wall-clock jump cannot extend a URL's life.

Three rules copied from that cache for the same reasons:

* **One lock for the whole cache, not one per key.** With ≤ 25 boards
  (`spec/prd.md` → *Capacity*) and one artifact per deploy there is nothing to convoy:
  the lock is held for one `signed_url` call bounded by `object_store_timeout_s`. Do not
  "fix" this into a per-key lock map; that is a bigger object to reason about for a
  contention that cannot occur at this scale.
* **Failures are not cached**, and no stale entry is served over a failed refresh. A URL
  that has passed its refresh margin is closer to expiry than a device's transfer is
  long, which is the one thing this cache exists to avoid handing out.
* **The refresh margin is not optional.** A URL signed with 3 s of life left becomes a
  403 in the middle of the device's download. An entry is reusable only while
  `monotonic() < signed_at + ttl_s - refresh_margin_s`, so every URL handed out has at
  least the margin left. A device unlucky enough to straddle the edge gets an upstream
  403, fails the download and retries — honest, and exactly the R6 resume path.

Never log the URL: it is a credential. The one INFO line here is the *observable* for the
"signing call count for a 10-range download is 1" acceptance criterion, so it is emitted
on an actual signing call and nowhere else.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from fleetforge.storage.objectstore import ObjectStore

logger = logging.getLogger(__name__)

# Bounded so a hostile or merely enthusiastic caller cannot grow this without limit.
# A fleet has one or two artifacts in flight; 128 is "never reached in practice".
DEFAULT_MAX_ENTRIES = 128


@dataclass(frozen=True, slots=True)
class _Entry:
    """One signed URL and the monotonic instant it was signed at."""

    url: str
    signed_at: float


class SignedUrlCache:
    """An upstream `signed_url` per key, reused until its refresh margin is reached.

    Constructed in `create_app()` and therefore I/O-free to build, like `CatalogCache`.

    `ttl_s` is the lifetime asked of the store; `refresh_margin_s` is how much of that
    lifetime is reserved for the device's transfer. A margin greater than or equal to the
    TTL means every request signs — degraded, but never a mid-transfer 403.
    """

    def __init__(
        self,
        *,
        ttl_s: int,
        refresh_margin_s: int,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._ttl_s = ttl_s
        # The reusable window. Clamped at 0 rather than raising: a misconfigured margin
        # must degrade into "sign every time", not into a container that cannot start.
        self._reuse_window_s = max(ttl_s - refresh_margin_s, 0)
        self._max_entries = max(max_entries, 1)
        self._lock = asyncio.Lock()
        self._entries: dict[str, _Entry] = {}

    def _fresh(self, key: str) -> str | None:
        """The cached URL if it still has the whole refresh margin left, else `None`."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        if (time.monotonic() - entry.signed_at) >= self._reuse_window_s:
            return None
        return entry.url

    async def get(self, store: ObjectStore, key: str) -> str:
        """A signed URL for `key`, signing only when there is no reusable one.

        Propagates `ObjectStoreError` — a caller must be able to say "the store is
        unreachable" rather than serve a redirect to nothing.
        """
        cached = self._fresh(key)
        if cached is not None:
            return cached

        async with self._lock:
            # Double-checked: ten ranged requests arriving together must cost one call.
            cached = self._fresh(key)
            if cached is not None:
                return cached
            # Dropped BEFORE the call, not after: if signing raises there must be no
            # entry left to serve, and a failure must not be cached.
            self._entries.pop(key, None)
            url = await store.signed_url(key, ttl_s=self._ttl_s)
            # The key, the TTL — never the URL, which is the credential itself.
            logger.info("signed a fresh artifact URL for %s (ttl %ds)", key, self._ttl_s)
            self._evict_if_full()
            self._entries[key] = _Entry(url=url, signed_at=time.monotonic())
            return url

    def _evict_if_full(self) -> None:
        """Drop the oldest insertion when the bound is reached. Called under the lock."""
        while len(self._entries) >= self._max_entries:
            self._entries.pop(next(iter(self._entries)))

    def clear(self) -> None:
        """Forget every URL. For tests, and for an operator rotating store credentials."""
        self._entries.clear()
