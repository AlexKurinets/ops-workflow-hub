from __future__ import annotations

from datetime import UTC, datetime, timedelta


class Clock:
    """Wall clock, replaceable in tests so retries and schedules are deterministic."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock(Clock):
    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)
