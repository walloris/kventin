from agent.checks import scheduler
from agent.checks.scheduler import CheckSchedule


def test_schedule_is_due_deterministically_and_pauses_under_overlay() -> None:
    schedule = CheckSchedule(
        a11y_every=2,
        performance_every=3,
        iframe_every=4,
        responsive_every=5,
        enable_iframe=True,
        enable_responsive=True,
    )

    assert schedule.due(60) == ["a11y", "performance", "iframe", "responsive"]
    assert schedule.due(60, has_overlay=True) == []


def test_check_failures_are_isolated(monkeypatch) -> None:
    calls = []

    def failing(*_args):
        calls.append("a11y")
        raise RuntimeError("broken checker")

    def successful(*_args):
        calls.append("performance")

    monkeypatch.setattr(scheduler, "run_a11y_check", failing)
    monkeypatch.setattr(scheduler, "run_perf_check", successful)
    completed = scheduler.run_periodic_checks(
        object(),
        object(),
        step=1,
        current_url="https://example.test",
        console_log=[],
        network_failures=[],
        has_overlay=False,
        schedule=CheckSchedule(
            a11y_every=1,
            performance_every=1,
            iframe_every=0,
            responsive_every=0,
            enable_iframe=False,
            enable_responsive=False,
        ),
    )

    assert calls == ["a11y", "performance"]
    assert completed == ["performance"]
