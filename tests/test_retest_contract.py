from agent.core.agent_memory import AgentMemory
from agent.defects.defect_builder import (
    RETEST_JSON_MARKER,
    build_retest_oracle,
    format_retest_spec_wiki,
    memory_actions_to_retest_plan,
    parse_retest_plan_from_description,
)
from agent.defects import defect_retest
from agent.defects.defect_retest import (
    _apply_plan_step,
    _assert_retest_oracle,
    _run_legacy_text_steps,
    _run_loaded_plan_steps,
)


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


def test_retest_oracle_excludes_resource_noise_and_unrelated_dead_clicks() -> None:
    oracle = build_retest_oracle(
        "HTTP 500 POST /api/register",
        console_log=[
            {
                "type": "error",
                "text": "Failed to load resource: the server responded with a status of 404",
                "source_url": "https://example.test/favicon.ico",
            },
            {"type": "pageerror", "text": "TypeError: profile is undefined"},
        ],
        network_failures=[
            {"status": 404, "method": "GET", "url": "https://example.test/favicon.ico"},
            {"status": 500, "method": "POST", "url": "https://example.test/api/register"},
        ],
    )

    assert oracle["fail_on_console_contains"] == ["TypeError: profile is undefined"]
    assert oracle["fail_on_network"] == [
        {"status": 500, "method": "POST", "url_path": "/api/register"}
    ]
    assert "possible_dead_click" not in oracle["fail_on_action_result_contains"]


def test_retest_fails_closed_for_empty_or_unknown_steps() -> None:
    memory = AgentMemory()

    assert _run_loaded_plan_steps(None, memory, {"steps": []})[0] is False
    assert _apply_plan_step(None, memory, {"op": "magic"}, "")[0] is False
    assert _run_legacy_text_steps(None, memory, [], "")[0] is False
    assert _run_legacy_text_steps(None, memory, ["Сделать что-нибудь"], "")[0] is False


def test_retest_in_qa_is_retriable_after_infrastructure_failure(monkeypatch) -> None:
    transition_calls = []
    monkeypatch.setattr(
        defect_retest,
        "get_issue_with_changelog",
        lambda _key: (
            200,
            {
                "fields": {"status": {"name": defect_retest.JIRA_RETEST_STATUS_QA}, "description": "x"},
                "changelog": {},
            },
            "",
        ),
    )
    monkeypatch.setattr(defect_retest, "start_qa_transition", lambda key: transition_calls.append(key))
    monkeypatch.setattr(defect_retest, "extract_description_text", lambda _fields: "description")
    monkeypatch.setattr(
        defect_retest,
        "run_retest_playwright",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("browser unavailable")),
    )

    assert defect_retest.process_retest_issue("QA-1") is False
    assert transition_calls == []


def test_inconclusive_retest_stays_in_qa(monkeypatch) -> None:
    comments = []
    reopened = []
    monkeypatch.setattr(
        defect_retest,
        "get_issue_with_changelog",
        lambda _key: (
            200,
            {
                "fields": {
                    "status": {"name": defect_retest.JIRA_RETEST_STATUS_QA},
                    "description": "legacy steps",
                    "comment": {"comments": []},
                },
                "changelog": {},
            },
            "",
        ),
    )
    monkeypatch.setattr(defect_retest, "extract_description_text", lambda _fields: "legacy steps")
    monkeypatch.setattr(
        defect_retest,
        "run_retest_playwright",
        lambda *_args: (False, "Шаг 1 не распознан: сделать что-нибудь"),
    )
    monkeypatch.setattr(defect_retest, "add_issue_comment", lambda _key, body: comments.append(body) or True)
    monkeypatch.setattr(
        defect_retest,
        "reopen_or_move_to_in_progress",
        lambda *_args, **_kwargs: reopened.append(True) or True,
    )

    assert defect_retest.process_retest_issue("QA-2") is True
    assert len(comments) == 1
    assert defect_retest._INCONCLUSIVE_MARKER_PREFIX in comments[0]
    assert reopened == []


def test_same_inconclusive_retest_is_not_repeated(monkeypatch) -> None:
    description = "legacy steps"
    marker = defect_retest._inconclusive_marker(description)
    runs = []
    monkeypatch.setattr(
        defect_retest,
        "get_issue_with_changelog",
        lambda _key: (
            200,
            {
                "fields": {
                    "status": {"name": defect_retest.JIRA_RETEST_STATUS_QA},
                    "description": description,
                    "comment": {"comments": [{"body": marker}]},
                },
                "changelog": {},
            },
            "",
        ),
    )
    monkeypatch.setattr(defect_retest, "extract_description_text", lambda _fields: description)
    monkeypatch.setattr(
        defect_retest,
        "run_retest_playwright",
        lambda *_args: runs.append(True) or (True, "ok"),
    )

    assert defect_retest.process_retest_issue("QA-3") is True
    assert runs == []
