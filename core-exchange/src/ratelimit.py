"""
Per-key sliding-window rate limiter for VectraFi protocol endpoints.

In-memory and per-worker — not suitable for distributed enforcement across
multiple workers. Sufficient for single-worker deployments and as a first-line
defence against trivial burst abuse. For multi-worker production, back this
with a shared Redis store or a reverse-proxy rate limit.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import HTTPException, status


class SlidingWindowLimiter:
    """Thread-safe sliding-window rate limiter keyed by an arbitrary string."""

    def __init__(self, max_calls: int, window_seconds: float) -> None:
        self.max_calls = max_calls
        self.window = window_seconds
        self._buckets: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str) -> None:
        """Raise HTTP 429 if `key` has exceeded the rate limit; otherwise record the call."""
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            bucket = self._buckets[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.max_calls:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=(
                        f"Rate limit exceeded — max {self.max_calls} requests "
                        f"per {int(self.window)}s per key"
                    ),
                    headers={"Retry-After": str(int(self.window))},
                )
            bucket.append(now)


# 20 negotiate-intent submissions per agent_id per 60 s
negotiate_limiter = SlidingWindowLimiter(max_calls=20, window_seconds=60.0)

# 10 evaluate calls per caller IP per 60 s (simulation is expensive)
evaluate_limiter = SlidingWindowLimiter(max_calls=10, window_seconds=60.0)
