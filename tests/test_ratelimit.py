from __future__ import annotations

import pytest

from hub.engine.errors import TransientError
from hub.engine.ratelimit import TokenBucket


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def test_bucket_allows_burst_then_blocks():
    ft = FakeTime()
    b = TokenBucket(rate_per_second=2, capacity=3, monotonic=ft.monotonic, sleep=ft.sleep)
    assert [b.try_acquire() for _ in range(4)] == [True, True, True, False]
    ft.t += 0.5  # one token refilled
    assert b.try_acquire() is True
    assert b.try_acquire() is False


def test_acquire_waits_for_refill():
    ft = FakeTime()
    b = TokenBucket(rate_per_second=4, capacity=1, monotonic=ft.monotonic, sleep=ft.sleep)
    assert b.acquire() == 0.0
    waited = b.acquire()
    assert waited == pytest.approx(0.25)
    assert ft.slept == [pytest.approx(0.25)]


def test_acquire_raises_transient_when_wait_exceeds_timeout():
    ft = FakeTime()
    b = TokenBucket(rate_per_second=0.1, capacity=1, monotonic=ft.monotonic, sleep=ft.sleep)
    b.acquire()
    with pytest.raises(TransientError, match="rate limit wait"):
        b.acquire(timeout=5)
    assert ft.slept == []


def test_invalid_configuration_rejected():
    with pytest.raises(ValueError):
        TokenBucket(0, 1)
