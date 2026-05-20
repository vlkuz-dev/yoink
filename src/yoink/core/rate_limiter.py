from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

IDLE_GC_SECONDS: float = 600.0


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    """Per-key token bucket with lazy garbage collection.

    `rate_per_min` tokens accrue per minute up to `burst`. Buckets idle longer
    than `IDLE_GC_SECONDS` are evicted opportunistically on each `try_acquire`.
    """

    def __init__(
        self,
        rate_per_min: int | None = None,
        burst: int | None = None,
        *,
        rate_per_hour: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        idle_gc_seconds: float = IDLE_GC_SECONDS,
    ) -> None:
        if (rate_per_min is None) == (rate_per_hour is None):
            raise ValueError("specify exactly one of rate_per_min or rate_per_hour")
        if rate_per_min is not None:
            if rate_per_min <= 0:
                raise ValueError("rate_per_min must be positive")
            rate_per_sec = rate_per_min / 60.0
            default_burst = rate_per_min
        else:
            assert rate_per_hour is not None
            if rate_per_hour <= 0:
                raise ValueError("rate_per_hour must be positive")
            rate_per_sec = rate_per_hour / 3600.0
            default_burst = rate_per_hour
        if burst is None:
            burst = default_burst
        if burst <= 0:
            raise ValueError("burst must be positive")
        self._rate_per_sec: float = rate_per_sec
        self._burst: float = float(burst)
        self._clock = clock
        self._idle_gc_seconds = idle_gc_seconds
        self._buckets: dict[int, _Bucket] = {}

    def try_acquire(self, key: int) -> bool:
        now = self._clock()
        self._gc(now)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self._burst, updated_at=now)
            self._buckets[key] = bucket
        else:
            elapsed = now - bucket.updated_at
            if elapsed > 0:
                bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._rate_per_sec)
            bucket.updated_at = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True
        return False

    def _gc(self, now: float) -> None:
        cutoff = now - self._idle_gc_seconds
        stale = [k for k, b in self._buckets.items() if b.updated_at < cutoff]
        for k in stale:
            del self._buckets[k]

    def __len__(self) -> int:
        return len(self._buckets)
