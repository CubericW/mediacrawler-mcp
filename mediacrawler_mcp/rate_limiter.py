"""Simple in-process sliding-window rate limiter.

Protects the target platforms (and the user's account) from bursty access by
capping how many crawl invocations may happen inside a rolling time window.
This is a best-effort, single-process guard; it is NOT a substitute for
account-level caution.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self, max_calls: int, window_seconds: int) -> None:
        self.max_calls = max(1, max_calls)
        self.window_seconds = max(1, window_seconds)
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._calls and self._calls[0] < cutoff:
            self._calls.popleft()

    def check(self) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        now = time.time()
        with self._lock:
            self._prune(now)
            if len(self._calls) < self.max_calls:
                return True, 0.0
            retry_after = self.window_seconds - (now - self._calls[0])
            return False, max(0.0, retry_after)

    def record(self) -> None:
        with self._lock:
            self._calls.append(time.time())

    def acquire(self) -> tuple[bool, float]:
        """Check and, if allowed, record the call atomically."""
        now = time.time()
        with self._lock:
            self._prune(now)
            if len(self._calls) < self.max_calls:
                self._calls.append(now)
                return True, 0.0
            retry_after = self.window_seconds - (now - self._calls[0])
            return False, max(0.0, retry_after)
