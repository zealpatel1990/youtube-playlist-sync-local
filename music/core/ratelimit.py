"""
Token-bucket rate limiting and a daily budget.

The previous version had neither, which mattered most for Gemini: a free-tier
key with a very low quota was called once per unidentified track with no pacing
and no ceiling, so a library sweep would exhaust the day's quota in minutes and
then fail every remaining track.

`DailyBudget` is the harder guarantee — a limiter only paces requests, it does
not stop you making 5,000 of them over a day.
"""

from __future__ import annotations

import threading
import time
from datetime import date


class RateLimiter:
    """Classic token bucket. Thread-safe, monotonic, no background thread."""

    def __init__(self, rate_per_sec: float, *, burst: float | None = None) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self.rate = rate_per_sec
        self.capacity = burst if burst is not None else max(1.0, rate_per_sec)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated = now

    def acquire(self, *, timeout: float | None = None) -> bool:
        """Block until a token is available. False if `timeout` elapsed first."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                shortfall = (1.0 - self._tokens) / self.rate
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                shortfall = min(shortfall, remaining)
            # Sleeping outside the lock lets other threads refill and proceed.
            time.sleep(max(shortfall, 0.01))


class DailyBudget:
    """A hard call ceiling that resets at local midnight.

    Unlike a rate limiter this cannot be waited out — once spent, `consume`
    returns False for the rest of the day and the caller must degrade.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._day = date.today()
        self._used = 0
        self._lock = threading.Lock()

    def _roll(self) -> None:
        today = date.today()
        if today != self._day:
            self._day = today
            self._used = 0

    def consume(self, count: int = 1) -> bool:
        if self.limit <= 0:
            return False
        with self._lock:
            self._roll()
            if self._used + count > self.limit:
                return False
            self._used += count
            return True

    @property
    def remaining(self) -> int:
        with self._lock:
            self._roll()
            return max(0, self.limit - self._used)

    def stats(self) -> dict:
        with self._lock:
            self._roll()
            return {
                "limit": self.limit,
                "used": self._used,
                "remaining": max(0, self.limit - self._used),
            }
