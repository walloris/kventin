from src.action_candidates import ActionCandidate, candidate_by_id, render_candidates_for_prompt
from src.action_policy import action_from_llm_candidate_choice, choose_best_candidate, rank_candidates
from src.agent_memory import AgentMemory


def test_policy_prefers_new_cta_over_footer_link() -> None:
    candidates = [
        ActionCandidate(id="c1", action="click", selector="ref:1", label="Privacy footer link", kind="link", priority=40),
        ActionCandidate(id="c2", action="click", selector="ref:2", label="Сохранить", kind="button", priority=10),
    ]

    best = choose_best_candidate(candidates, AgentMemory())

    assert best is not None
    assert best.id == "c2"
    assert rank_candidates(candidates)[0].score > rank_candidates(candidates)[1].score


def test_policy_penalizes_repeated_candidate() -> None:
    memory = AgentMemory()
    memory.current_url_pattern = "/orders"
    memory.done_by_url = {"/orders": {"click": {"tid:save"}}}

    candidates = [
        ActionCandidate(
            id="c1",
            action="click",
            selector="ref:1",
            label="Сохранить",
            kind="button",
            stable_key="tid:save",
        ),
        ActionCandidate(
            id="c2",
            action="type",
            selector="ref:2",
            label="Email",
            kind="input",
            stable_key="tid:email",
        ),
    ]

    best = choose_best_candidate(candidates, memory)

    assert best is not None
    assert best.id == "c2"


def test_llm_candidate_choice_returns_candidate_action() -> None:
    candidates = [
        ActionCandidate(id="c1", action="click", selector="ref:1", label="Open"),
        ActionCandidate(id="c2", action="type", selector="ref:2", value="test@test.com", label="Email"),
    ]

    action = action_from_llm_candidate_choice('{"candidate_id":"c2","reason":"форма"}', candidates)

    assert action is not None
    assert action["action"] == "type"
    assert action["selector"] == "ref:2"
    assert action["_candidate_id"] == "c2"


def test_candidate_prompt_and_lookup() -> None:
    candidates = [ActionCandidate(id="c1", action="click", selector="ref:1", label="Continue")]

    assert "c1:" in render_candidates_for_prompt(candidates)
    assert candidate_by_id(candidates, "c1").selector == "ref:1"
