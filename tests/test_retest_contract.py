from src.agent_memory import AgentMemory
from src.defect_builder import (
    RETEST_JSON_MARKER,
    build_retest_oracle,
    format_retest_spec_wiki,
    memory_actions_to_retest_plan,
    parse_retest_plan_from_description,
)
from src.defect_retest import _assert_retest_oracle


def test_retest_plan_parses_code_block_before_following_sections() -> None:
    plan = {
        "kventin_retest_version": 1,
        "start_url": "https://example.test",
        "primary_locator": "#save",
        "oracle": {"fail_on_action_result_contains": ["click_error"]},
        "steps": [{"op": "click", "selector": "#save"}],
    }
    description = format_retest_spec_wiki(plan) + "\n\nh3. Ожидаемый результат\nOK"

    parsed = parse_retest_plan_from_description(description)

    assert parsed is not None
    assert parsed["start_url"] == "https://example.test"
    assert parsed["oracle"]["fail_on_action_result_contains"] == ["click_error"]


def test_memory_actions_to_retest_plan_includes_oracle_signals() -> None:
    memory = AgentMemory()
    memory.add_action(
        {
            "action": "click",
            "selector": "#save",
            "_canonical_locator": "#save",
            "_step_context": {"url_before": "https://example.test/form"},
        },
        result="click_error: Timeout 10000ms exceeded",
    )

    plan = memory_actions_to_retest_plan(
        memory,
        "https://example.test/form",
        bug_description="Кнопка сохранения не нажимается",
        console_log=[{"type": "error", "text": "TypeError: Cannot read properties of undefined"}],
        network_failures=[{"status": 500, "method": "POST", "url": "https://example.test/api/save?id=1"}],
    )

    assert plan is not None
    assert plan["oracle"]["original_action_failure"]["selector"] == "#save"
    assert "TypeError" in plan["oracle"]["fail_on_console_contains"][0]
    assert plan["oracle"]["fail_on_network"][0]["url_path"] == "/api/save"


def test_retest_oracle_fails_on_repeated_signals() -> None:
    memory = AgentMemory()
    memory.add_action({"action": "click", "selector": "#save"}, result="clicked")
    oracle = build_retest_oracle(
        "API 500",
        network_failures=[{"status": 500, "method": "POST", "url": "https://example.test/api/save"}],
    )
    plan = {
        "kventin_retest_version": 1,
        "start_url": "https://example.test",
        "steps": [],
        "oracle": oracle,
    }

    ok, msg = _assert_retest_oracle(
        plan,
        memory,
        console_log=[],
        network_failures=[{"status": 500, "method": "POST", "url": "https://example.test/api/save"}],
    )

    assert ok is False
    assert "network-сигнал" in msg
