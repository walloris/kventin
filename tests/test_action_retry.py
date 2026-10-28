from agent.actions import action_retry
from agent.actions.action_preflight import ActionPreflightResult


def test_retry_is_blocked_when_a_new_overlay_hides_targetless_action(monkeypatch) -> None:
    monkeypatch.setattr(action_retry, "inspect_overlays", lambda _page: {"has_overlay": True})

    decision = action_retry.prepare_action_retry(
        object(),
        object(),
        {"action": "scroll", "selector": ""},
    )

    assert decision.allowed is False
    assert decision.reason == "overlay_changed"


def test_retry_uses_live_preflight_result(monkeypatch) -> None:
    repaired = {"action": "click", "selector": "ref:2"}
    monkeypatch.setattr(action_retry, "inspect_overlays", lambda _page: {"has_overlay": True})
    monkeypatch.setattr(
        action_retry,
        "preflight_action",
        lambda *_args, **_kwargs: ActionPreflightResult(
            repaired,
            True,
            "ref_repaired",
            repaired=True,
        ),
    )

    decision = action_retry.prepare_action_retry(
        object(),
        object(),
        {"action": "click", "selector": "ref:1"},
    )

    assert decision.allowed is True
    assert decision.action == repaired
