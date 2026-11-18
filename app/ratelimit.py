"""Simple in-process sliding-window rate limiter and login lockout (no external services)."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        now = time.monotonic()
        with self._lock:
            queue = self._hits[key]
            while queue and now - queue[0] >= window_seconds:
                queue.popleft()
            if len(queue) >= limit:
                return False
            queue.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class LoginGuard:
    """Track consecutive failures per key and lock after a threshold."""

    def __init__(self) -> None:
        self._failures: dict[str, int] = defaultdict(int)
        self._locked_until: dict[str, float] = {}
        self._lock = Lock()

    def locked_remaining(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            until = self._locked_until.get(key)
            if until is None:
                return 0
            if until <= now:
                self._locked_until.pop(key, None)
                return 0
            return int(until - now) + 1

    def register_failure(self, key: str, max_failures: int, lock_seconds: float) -> int:
        """Return remaining lock seconds (0 if not yet locked)."""
        now = time.monotonic()
        with self._lock:
            until = self._locked_until.get(key)
            if until and until > now:
                return int(until - now) + 1
            self._failures[key] += 1
            if self._failures[key] >= max_failures:
                self._locked_until[key] = now + lock_seconds
                self._failures[key] = 0
                return int(lock_seconds)
            return 0

    def register_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._locked_until.clear()



class ConsecutiveCounter:
    """In-memory consecutive failure counters, reset on success or explicit clear."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = Lock()

    def fail(self, key: str) -> int:
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1
            return self._counts[key]

    def reset(self, key: str) -> None:
        with self._lock:
            self._counts.pop(key, None)

    def count(self, key: str) -> int:
        with self._lock:
            return self._counts.get(key, 0)

    def clear(self) -> None:
        with self._lock:
            self._counts.clear()
