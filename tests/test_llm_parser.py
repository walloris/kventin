from agent.llm.llm_parser import parse_llm_action


def test_parse_llm_action_accepts_plain_json() -> None:
    assert parse_llm_action('{"action": "click", "selector": "ref:1"}') == {
        "action": "click",
        "selector": "ref:1",
    }


def test_parse_llm_action_accepts_fenced_json() -> None:
    raw = """```json
{"action": "scroll", "direction": "down"}
```"""

    assert parse_llm_action(raw) == {"action": "scroll", "direction": "down"}


def test_parse_llm_action_extracts_embedded_object() -> None:
    raw = 'Сначала сделай так: {"action": "hover", "selector": "button"} потом проверь.'

    assert parse_llm_action(raw) == {"action": "hover", "selector": "button"}


def test_parse_llm_action_rejects_non_action_json() -> None:
    assert parse_llm_action('{"message": "ok"}') is None
