"""Reusable resilience primitives for external services.

The agent talks to services that can be temporarily unavailable for perfectly
normal reasons: rate limits, proxy restarts, network hiccups, and Jira rolling
deployments.  Keeping retry and circuit-breaker behaviour here prevents each
integration from inventing subtly different failure semantics.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Callable, Mapping, Optional, Set


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff with jitter."""

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter_ratio: float = 0.25
    retryable_statuses: Set[int] = frozenset(
        {408, 409, 425, 429, 500, 502, 503, 504}
    )

    def attempts(self) -> int:
        return max(1, int(self.max_attempts))

    def delay_for(
        self,
        attempt_index: int,
        *,
        retry_after: Optional[float] = None,
        random_fn: Optional[Callable[[float, float], float]] = None,
    ) -> float:
        if retry_after is not None:
            return max(0.0, min(float(retry_after), self.max_delay))
        base = min(
            max(0.0, self.base_delay) * (2 ** max(0, attempt_index)),
            max(0.0, self.max_delay),
        )
        jitter_source = random_fn or random.uniform
        jitter = jitter_source(0.0, base * max(0.0, self.jitter_ratio))
        return min(base + jitter, max(0.0, self.max_delay))


def parse_retry_after(
    headers: Optional[Mapping[str, str]],
    *,
    now: Callable[[], float] = time.time,
) -> Optional[float]:
    """Parse both numeric and HTTP-date ``Retry-After`` values."""

    raw = str((headers or {}).get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
        return max(0.0, retry_at.timestamp() - now())
    except (TypeError, ValueError, OverflowError):
        return None


class CircuitBreaker:
    """Thread-safe closed/open/half-open circuit breaker.

    Only one probe is admitted after cooldown.  A successful probe closes the
    circuit; a failed probe opens it for another cooldown period.
    """

    def __init__(
        self,
        *,
        failure_threshold: int,
        cooldown_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0
        self._probe_in_flight = False

    def allow_request(self) -> bool:
        with self._lock:
            now = self._clock()
            if self._open_until <= 0:
                return True
            if now < self._open_until:
                return False
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0.0
            self._probe_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._probe_in_flight = False
            if self._failures >= self.failure_threshold:
                self._open_until = self._clock() + self.cooldown_seconds

    def snapshot(self) -> dict:
        with self._lock:
            now = self._clock()
            return {
                "state": "open" if self._open_until > now else "closed",
                "failures": self._failures,
                "retry_in_seconds": max(0.0, self._open_until - now),
            }


__all__ = ["CircuitBreaker", "RetryPolicy", "parse_retry_after"]
