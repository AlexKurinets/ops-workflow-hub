from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with a cap and a small jitter so retries from many runs spread out."""

    max_attempts: int = 5
    base_seconds: float = 2.0
    max_seconds: float = 300.0
    multiplier: float = 2.0
    jitter_ratio: float = 0.1

    def delay_for(self, failures: int, rng: random.Random | None = None) -> float:
        """Seconds to wait after the given number of consecutive failures (1-based)."""
        if failures < 1:
            raise ValueError("failures must be at least 1")
        raw = self.base_seconds * (self.multiplier ** (failures - 1))
        capped = min(self.max_seconds, raw)
        if self.jitter_ratio <= 0:
            return capped
        rng = rng or random.Random()
        spread = capped * self.jitter_ratio
        return max(0.0, capped + rng.uniform(-spread, spread))

    def exhausted(self, failures: int) -> bool:
        return failures >= self.max_attempts
