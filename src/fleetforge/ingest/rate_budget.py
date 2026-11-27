"""Bounded-memory per-device MQTT ingest token-bucket rate limiter (`A-15`).

Critical OTA and lifecycle channels (`announce`, `status`, `ack`, `presence`) never consume
tokens and are never dropped so rollbacks, confirmations, and commands never stall behind a
chatty board. High-volume channels (`log`, `telemetry`, `hb`) draw from an LRU-bounded
monotonic token bucket per `device_id` before touching PostgreSQL.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from fleetforge.config import Settings
from fleetforge.ingest.protocol import UpChannel

logger = logging.getLogger(__name__)

CRITICAL_UP_CHANNELS: frozenset[str] = frozenset(
    {
        UpChannel.ANNOUNCE,
        UpChannel.STATUS,
        UpChannel.ACK,
        UpChannel.PRESENCE,
    }
)


@dataclass
class _DeviceBucket:
    tokens: float
    updated_at: float
    last_warn_at: float | None = None
    suppressed_since_warn: int = 0


class DeviceIngestRateLimiter:
    """LRU-bounded per-device monotonic token bucket for non-critical MQTT `up/` messages."""

    def __init__(
        self,
        *,
        rate_per_min: int = 120,
        burst: int = 30,
        max_devices: int = 4096,
        warn_interval_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate_per_min = max(0, int(rate_per_min))
        self._burst = max(0, int(burst))
        self._max_devices = max(1, int(max_devices))
        self._warn_interval_s = max(0.0, float(warn_interval_s))
        self._clock = clock
        self._buckets: OrderedDict[str, _DeviceBucket] = OrderedDict()
        self._dropped_total = 0

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> DeviceIngestRateLimiter:
        if settings is None:
            return cls(rate_per_min=0, burst=0, clock=clock)
        return cls(
            rate_per_min=settings.ingest_device_rate_per_min,
            burst=settings.ingest_device_burst,
            clock=clock,
        )

    @property
    def enabled(self) -> bool:
        return self._rate_per_min > 0 and self._burst > 0

    @property
    def dropped_total(self) -> int:
        return self._dropped_total

    @property
    def tracked_devices(self) -> int:
        return len(self._buckets)

    def allow(self, device_id: str, channel: str) -> bool:
        """Return `True` if `(device_id, channel)` may proceed to PostgreSQL, else `False`."""
        if channel in CRITICAL_UP_CHANNELS or not self.enabled:
            return True

        now = self._clock()
        bucket = self._buckets.get(device_id)
        if bucket is None:
            bucket = _DeviceBucket(tokens=float(self._burst), updated_at=now)
            self._buckets[device_id] = bucket
            if len(self._buckets) > self._max_devices:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(device_id)
            elapsed = max(0.0, now - bucket.updated_at)
            refill = elapsed * (self._rate_per_min / 60.0)
            bucket.tokens = min(float(self._burst), bucket.tokens + refill)
            bucket.updated_at = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True

        self._dropped_total += 1
        bucket.suppressed_since_warn += 1
        if bucket.last_warn_at is None or (now - bucket.last_warn_at) >= self._warn_interval_s:
            suppressed = bucket.suppressed_since_warn
            bucket.suppressed_since_warn = 0
            bucket.last_warn_at = now
            logger.warning(
                "device %s exhausted ingest rate budget on up/%s "
                "(rate=%d/min burst=%d, dropped=%d)",
                device_id,
                channel,
                self._rate_per_min,
                self._burst,
                suppressed,
            )
        return False
