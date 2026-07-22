from datetime import datetime, timezone
from email.utils import format_datetime

from agent.core.resilience import CircuitBreaker, RetryPolicy, parse_retry_after


def test_retry_policy_is_bounded_and_honors_retry_after() -> None:
    policy = RetryPolicy(max_attempts=4, base_delay=2, max_delay=5, jitter_ratio=0.5)

    assert policy.delay_for(0, random_fn=lambda _low, high: high) == 3
    assert policy.delay_for(5, random_fn=lambda _low, high: high) == 5
    assert policy.delay_for(0, retry_after=30) == 5


def test_parse_retry_after_supports_seconds_and_http_date() -> None:
    assert parse_retry_after({"Retry-After": "2.5"}) == 2.5

    retry_at = datetime.fromtimestamp(105, tz=timezone.utc)
    assert parse_retry_after(
        {"Retry-After": format_datetime(retry_at, usegmt=True)},
        now=lambda: 100,
    ) == 5


def test_circuit_breaker_allows_only_one_recovery_probe() -> None:
    clock = [10.0]
    circuit = CircuitBreaker(
        failure_threshold=2,
        cooldown_seconds=5,
        clock=lambda: clock[0],
    )

    assert circuit.allow_request() is True
    circuit.record_failure()
    assert circuit.allow_request() is True
    circuit.record_failure()
    assert circuit.allow_request() is False

    clock[0] = 15.0
    assert circuit.allow_request() is True
    assert circuit.allow_request() is False
    circuit.record_success()
    assert circuit.allow_request() is True
