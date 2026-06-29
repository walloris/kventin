from src.action_selection import apply_preflight_or_fallback


class Page:
    url = "https://example.test/page"

    def is_closed(self):
        return False

    def evaluate(self, script, arg=None):
        if "window.__agentRefMeta && window.__agentRefMeta[ref]" in script:
            return "tid:save"
        if "window.__agentLocator" in script:
            return ""
        if "const byAttr = document.querySelector" in script:
            return {
                "exists": False,
                "visible": False,
                "disabled": False,
                "hidden_dom": False,
                "outside_overlay": False,
            }
        return ""


class Memory:
    ignore_overlay = False
    current_url_pattern = "/page"

    def is_already_done_action(self, action):
        return False

    def record_repeat(self):
        raise AssertionError("repeat should not be recorded")


def test_selection_falls_back_when_preflight_rejects_stale_ref() -> None:
    fallback = {"action": "scroll", "selector": "down", "reason": "fallback"}

    action, source, trace = apply_preflight_or_fallback(
        page=Page(),
        memory=Memory(),
        action={"action": "click", "selector": "ref:1"},
        source="LLM",
        has_overlay=False,
        decision_candidates=[],
        fallback_factory=lambda: fallback,
        expected_ref_meta={"1": "tid:save"},
        step=1,
    )

    assert action["action"] == "scroll"
    assert source == "LLM/Preflight"
    assert trace["preflight"] == "stale_ref"
