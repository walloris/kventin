from agent.actions.action_executor import ActionHandlers, execute_browser_action


class Page:
    url = "https://example.test/page"


class Memory:
    def __init__(self):
        self.coverage = []

    def record_page_element(self, url, key):
        self.coverage.append((url, key))


def _handlers(calls):
    return ActionHandlers(
        click=lambda selector, reason: calls.append(("click", selector, reason)) or "clicked: " + selector,
        fill_form=lambda strategy: calls.append(("fill_form", strategy)) or "form_filled: 2 fields",
        type_text=lambda selector, value, strategy: calls.append(("type", selector, value, strategy)) or "typed: " + value,
        scroll=lambda direction: calls.append(("scroll", direction)) or "scrolled_" + direction,
        hover=lambda selector: calls.append(("hover", selector)) or "hovered: " + selector,
        close_modal=lambda selector: calls.append(("close_modal", selector)) or "modal_closed",
        select_option=lambda selector, value: calls.append(("select", selector, value)) or "selected",
        press_key=lambda key: calls.append(("key", key)) or "key_pressed",
        upload_file=lambda selector, path: calls.append(("upload", selector, path)) or "uploaded: file",
    )


def test_execute_browser_action_dispatches_and_records_click_coverage() -> None:
    calls = []
    memory = Memory()

    result = execute_browser_action(
        Page(),
        {"action": "click", "selector": "ref:1", "reason": "test"},
        memory,
        _handlers(calls),
    )

    assert result == "clicked: ref:1"
    assert calls == [("click", "ref:1", "test")]
    assert memory.coverage == [("https://example.test/page", "click:ref:1")]


def test_execute_browser_action_uses_escape_for_empty_press_key() -> None:
    calls = []

    result = execute_browser_action(
        Page(),
        {"action": "press_key", "selector": "", "value": ""},
        None,
        _handlers(calls),
    )

    assert result == "key_pressed"
    assert calls == [("key", "Escape")]
