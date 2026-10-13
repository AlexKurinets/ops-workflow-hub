from __future__ import annotations

import threading
import time
from collections.abc import Callable

from hub.engine.errors import TransientError


class TokenBucket:
    """Per-adapter rate limiter. Each call takes one token; tokens refill at a fixed rate.

    One bucket per adapter per worker process. The production build shared the budget across
    workers through a Postgres advisory counter, which is left out here; see the README.
    """

    def __init__(
        self,
        rate_per_second: float,
        capacity: int,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_second <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self.rate = rate_per_second
        self.capacity = capacity
        self._tokens = float(capacity)
        self._monotonic = monotonic
        self._sleep = sleep
        self._last = monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._monotonic()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(float(self.capacity), self._tokens + elapsed * self.rate)

    def try_acquire(self) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def acquire(self, timeout: float | None = None) -> float:
        """Block until a token is available. Returns the seconds waited.

        Raises TransientError if the wait would exceed the timeout, so the step is retried
        later instead of holding a worker for minutes.
        """
        waited = 0.0
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return waited
                needed = (1.0 - self._tokens) / self.rate
            if timeout is not None and waited + needed > timeout:
                raise TransientError(f"rate limit wait of {needed:.1f}s exceeds timeout {timeout}s")
            self._sleep(needed)
            waited += needed
