"""Exponential backoff helper for reconnect logic."""
from __future__ import annotations

from app.config import settings


class Backoff:
    """Tracks current delay; bumps it on each failure, resets on success."""

    def __init__(self,
                 initial: int | None = None,
                 maximum: int | None = None,
                 factor: float = 2.0):
        self.initial = initial or settings.wan_reconnect_initial_delay_seconds
        self.maximum = maximum or settings.wan_reconnect_max_delay_seconds
        self.factor  = factor
        self.current = self.initial
        self.failures = 0

    def fail(self) -> int:
        """Record a failure, return delay to wait before retry."""
        self.failures += 1
        delay = self.current
        self.current = min(self.maximum, max(self.initial, int(self.current * self.factor)))
        return delay

    def succeed(self) -> None:
        self.failures = 0
        self.current  = self.initial
