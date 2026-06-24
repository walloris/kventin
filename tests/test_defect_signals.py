from src.defect_signals import collect_rule_signals, pick_best_signal


def test_defect_signals_prioritize_5xx() -> None:
    signals = collect_rule_signals(
        action={"action": "click", "selector": "ref:1"},
        result="clicked",
        current_url="https://example.test/orders",
        new_console=[{"type": "pageerror", "text": "TypeError: boom"}],
        new_network=[{"status": 500, "method": "GET", "url": "https://example.test/api/orders"}],
    )

    best = pick_best_signal(signals)

    assert best is not None
    assert best.kind == "network_5xx"
    assert best.confidence > 0.9


def test_defect_signals_detect_action_failure() -> None:
    signals = collect_rule_signals(
        action={"action": "click", "selector": "ref:7"},
        result="click_error: Timeout 10000ms exceeded while waiting for element to be visible, enabled and stable",
        current_url="https://example.test/orders",
        new_console=[],
        new_network=[],
    )

    best = pick_best_signal(signals)

    assert best is not None
    assert best.kind == "action_failure"
    assert "кликабельным" in best.title
