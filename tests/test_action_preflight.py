from src.action_preflight import expected_stable_key, preflight_action, ref_num
from src.agent_memory import AgentMemory


class FakePage:
    def __init__(self, *, ref_meta=None, states=None):
        self.ref_meta = {str(k): v for k, v in (ref_meta or {}).items()}
        self.states = {str(k): v for k, v in (states or {}).items()}
        self.url = "https://example.test/orders"

    def is_closed(self):
        return False

    def evaluate(self, script, arg=None):
        if "window.__agentLocator" in script:
            return ""
        if "window.__agentRefMeta && window.__agentRefMeta[ref]" in script:
            return self.ref_meta.get(str(arg), "")
        if "for (const ref of Object.keys(meta))" in script:
            for ref, key in self.ref_meta.items():
                state = self.states.get(ref, {})
                if key == arg and state.get("exists", True) and state.get("visible", True):
                    return f"ref:{ref}"
            return ""
        if "const byAttr = document.querySelector" in script:
            state = {
                "exists": True,
                "visible": True,
                "disabled": False,
                "hidden_dom": False,
                "outside_overlay": False,
                "stable_key": self.ref_meta.get(str(arg), ""),
            }
            state.update(self.states.get(str(arg), {}))
            return state
        return ""


def test_ref_num_and_expected_stable_key() -> None:
    assert ref_num("ref:42") == 42
    assert ref_num("42") == 42
    assert ref_num("button") is None
    assert expected_stable_key("ref:42", {"42": "tid:save"}) == "tid:save"


def test_preflight_repairs_stale_llm_ref() -> None:
    page = FakePage(
        ref_meta={"2": "tid:settings"},
        states={"2": {"exists": True, "visible": True}},
    )

    result = preflight_action(
        page,
        AgentMemory(),
        {"action": "click", "selector": "ref:1"},
        expected_ref_meta={"1": "tid:settings"},
    )

    assert result.ok is True
    assert result.repaired is True
    assert result.action["selector"] == "ref:2"


def test_preflight_rejects_hidden_ref() -> None:
    page = FakePage(
        ref_meta={"1": "tid:save"},
        states={"1": {"exists": True, "visible": False, "hidden_dom": True}},
    )

    result = preflight_action(
        page,
        AgentMemory(),
        {"action": "click", "selector": "ref:1"},
    )

    assert result.ok is False
    assert result.reason == "hidden_dom"


def test_preflight_rejects_repeated_action() -> None:
    memory = AgentMemory()
    memory.current_url_pattern = "/orders"
    memory.done_by_url = {"/orders": {"click": {"tid:save"}}}

    result = preflight_action(
        FakePage(),
        memory,
        {
            "action": "click",
            "selector": "ref:1",
            "_stable_key": "tid:save",
            "_url_pattern": "/orders",
        },
    )

    assert result.ok is False
    assert result.reason == "repeat"
