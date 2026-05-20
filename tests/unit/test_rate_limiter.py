from __future__ import annotations

import pytest

from yoink.core.rate_limiter import IDLE_GC_SECONDS, TokenBucketLimiter


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_first_burst_passes_then_blocks() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(rate_per_min=10, clock=clock)
    chat = 42
    for _ in range(10):
        assert limiter.try_acquire(chat) is True
    assert limiter.try_acquire(chat) is False


def test_separate_keys_have_independent_buckets() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(rate_per_min=2, clock=clock)
    assert limiter.try_acquire(1) is True
    assert limiter.try_acquire(1) is True
    assert limiter.try_acquire(1) is False
    assert limiter.try_acquire(2) is True
    assert limiter.try_acquire(2) is True
    assert limiter.try_acquire(2) is False


def test_refills_over_time() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(rate_per_min=60, clock=clock)
    for _ in range(60):
        assert limiter.try_acquire(7) is True
    assert limiter.try_acquire(7) is False
    clock.advance(1.0)  # 60/min = 1 token/sec
    assert limiter.try_acquire(7) is True
    assert limiter.try_acquire(7) is False


def test_burst_caps_token_accrual() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(rate_per_min=10, burst=5, clock=clock)
    for _ in range(5):
        assert limiter.try_acquire(1) is True
    assert limiter.try_acquire(1) is False
    clock.advance(3600)  # huge gap, but tokens still cap at burst=5
    for _ in range(5):
        assert limiter.try_acquire(1) is True
    assert limiter.try_acquire(1) is False


def test_idle_buckets_garbage_collected() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(rate_per_min=10, clock=clock)
    assert limiter.try_acquire(1) is True
    assert limiter.try_acquire(2) is True
    assert len(limiter) == 2
    clock.advance(IDLE_GC_SECONDS + 1.0)
    assert limiter.try_acquire(3) is True
    assert len(limiter) == 1
    assert 1 not in limiter._buckets
    assert 2 not in limiter._buckets


def test_active_bucket_not_gc_when_recently_used() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(rate_per_min=10, clock=clock)
    assert limiter.try_acquire(1) is True
    clock.advance(IDLE_GC_SECONDS - 1.0)
    assert limiter.try_acquire(1) is True
    clock.advance(IDLE_GC_SECONDS - 1.0)
    assert limiter.try_acquire(1) is True
    assert 1 in limiter._buckets


def test_invalid_rate_rejected() -> None:
    with pytest.raises(ValueError):
        TokenBucketLimiter(rate_per_min=0)
    with pytest.raises(ValueError):
        TokenBucketLimiter(rate_per_min=10, burst=0)


def test_burst_defaults_to_rate_per_min() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(rate_per_min=3, clock=clock)
    for _ in range(3):
        assert limiter.try_acquire(99) is True
    assert limiter.try_acquire(99) is False


def test_rate_per_hour_basic() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(rate_per_hour=5, clock=clock)
    for _ in range(5):
        assert limiter.try_acquire(1) is True
    assert limiter.try_acquire(1) is False


def test_rate_per_hour_refills_over_time() -> None:
    clock = FakeClock()
    # idle_gc bumped above the refill window so bucket survives long enough
    # to observe the refill instead of being evicted as idle.
    limiter = TokenBucketLimiter(rate_per_hour=5, clock=clock, idle_gc_seconds=3600.0)
    for _ in range(5):
        assert limiter.try_acquire(1) is True
    assert limiter.try_acquire(1) is False
    clock.advance(720.0)  # 5/hour -> one token per 720s
    assert limiter.try_acquire(1) is True
    assert limiter.try_acquire(1) is False


def test_rate_per_hour_burst_defaults_to_rate() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(rate_per_hour=5, clock=clock)
    clock.advance(3600.0 * 10)
    for _ in range(5):
        assert limiter.try_acquire(1) is True
    assert limiter.try_acquire(1) is False


def test_requires_exactly_one_rate_kind() -> None:
    with pytest.raises(ValueError):
        TokenBucketLimiter()
    with pytest.raises(ValueError):
        TokenBucketLimiter(rate_per_min=10, rate_per_hour=5)
    with pytest.raises(ValueError):
        TokenBucketLimiter(rate_per_hour=0)
