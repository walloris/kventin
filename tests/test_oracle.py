from agent.core.oracle import build_oracle_context, should_run_oracle


def test_should_run_oracle_only_on_useful_signal_when_lazy() -> None:
    assert should_run_oracle(
        enabled=True,
        action_type="click",
        has_screenshot=True,
        visual_diff={"changed": False},
        new_errors=[],
        new_network=[],
        lazy_on_visual_or_error=True,
    ) is False

    assert should_run_oracle(
        enabled=True,
        action_type="click",
        has_screenshot=True,
        visual_diff={"changed": True},
        new_errors=[],
        new_network=[],
        lazy_on_visual_or_error=True,
    ) is True


def test_oracle_context_includes_observed_deltas() -> None:
    context = build_oracle_context(
        action={"action": "click", "selector": "ref:1"},
        result="clicked",
        expected_outcome="Открывается форма",
        visual_diff={"changed": True, "change_percent": 12.5, "detail": "center changed"},
        new_errors=[{"type": "pageerror", "text": "TypeError: boom"}],
        new_network=[{"status": 404, "method": "GET", "url": "https://example.test/api/item"}],
    )

    assert "Открывается форма" in context
    assert "TypeError: boom" in context
    assert "404 GET" in context
    assert "12.5%" in context
