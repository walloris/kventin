from agent.actions.action_candidates import ActionCandidate, candidate_by_id, render_candidates_for_prompt
from agent.actions.action_policy import action_from_llm_candidate_choice, choose_best_candidate, rank_candidates
from agent.core.agent_memory import AgentMemory


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


def test_policy_returns_no_candidate_when_all_are_already_covered() -> None:
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
        )
    ]

    assert choose_best_candidate(candidates, memory) is None


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


def test_policy_prefers_overlay_close_over_background_cta() -> None:
    candidates = [
        ActionCandidate(id="c0", action="close_modal", selector="ref:9", label="Закрыть активный оверлей", kind="overlay", priority=5),
        ActionCandidate(
            id="c1",
            action="click",
            selector="ref:1",
            label="Сохранить",
            kind="button",
            priority=10,
            risk_flags=["outside_overlay"],
        ),
    ]

    assert choose_best_candidate(candidates).id == "c0"


def test_policy_tests_modal_content_before_closing_it() -> None:
    candidates = [
        ActionCandidate(id="c0", action="close_modal", selector="ref:9", label="Закрыть активный оверлей", kind="overlay", priority=5),
        ActionCandidate(id="c1", action="click", selector="ref:10", label="Продолжить", kind="button", priority=10),
    ]

    assert choose_best_candidate(candidates).id == "c1"


def test_policy_fills_form_before_submit() -> None:
    candidates = [
        ActionCandidate(
            id="c1",
            action="click",
            selector="ref:1",
            label="Зарегистрироваться",
            kind="button",
            priority=10,
            risk_flags=["form_incomplete"],
        ),
        ActionCandidate(
            id="c2",
            action="type",
            selector="ref:2",
            value="test@example.com",
            label="Email",
            kind="input",
            priority=20,
        ),
    ]

    assert choose_best_candidate(candidates).id == "c2"
