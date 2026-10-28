from agent.core.supervisor import AgentSupervisor, SupervisorConfig


def test_supervisor_does_not_restart_bounded_run() -> None:
    sleeps = []
    supervisor = AgentSupervisor(
        SupervisorConfig(continuous=True, bounded_run=True),
        sleep_fn=sleeps.append,
    )

    result = supervisor.run(
        lambda **_kwargs: {
            "defects": 1,
            "steps": 4,
            "error": "browser closed",
            "termination": "page_closed",
        }
    )

    assert result["restarts"] == 0
    assert result["steps"] == 4
    assert sleeps == []


def test_supervisor_restarts_recoverable_sessions_and_aggregates_results() -> None:
    sessions = iter(
        [
            {"defects": 0, "steps": 0, "termination": "startup_error"},
            {"defects": 1, "steps": 3, "termination": "page_closed"},
            {"defects": 0, "steps": 2, "termination": "completed"},
        ]
    )
    sleeps = []
    supervisor = AgentSupervisor(
        SupervisorConfig(continuous=True, base_delay=1, max_delay=10),
        sleep_fn=sleeps.append,
    )

    result = supervisor.run(lambda **_kwargs: next(sessions))

    assert result["termination"] == "completed"
    assert result["restarts"] == 2
    assert result["steps"] == 5
    assert result["defects"] == 1
    assert sleeps == [1, 1]


def test_supervisor_contains_an_unhandled_session_exception() -> None:
    calls = [0]

    def run_session(**_kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("boom")
        return {"defects": 0, "steps": 1, "termination": "completed"}

    supervisor = AgentSupervisor(
        SupervisorConfig(continuous=True, base_delay=0, max_delay=0),
        sleep_fn=lambda _delay: None,
    )

    result = supervisor.run(run_session)

    assert calls[0] == 2
    assert result["termination"] == "completed"
    assert result["restarts"] == 1
