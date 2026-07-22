from agent.defects import defect_pipeline
from agent.defects.jira_client import (
    is_local_duplicate,
    reserve_local_defect,
    reset_session_defects,
)


class Memory:
    def __init__(self):
        self.created = []

    def record_defect_created(self, key, summary, severity):
        self.created.append((key, summary, severity))


class MemoryWithDefects(Memory):
    def __init__(self):
        super().__init__()
        self.defects_created = [{"key": "QA-1", "summary": "HTTP 500 на checkout"}]


def setup_function() -> None:
    reset_session_defects()


def teardown_function() -> None:
    reset_session_defects()


def test_reserved_defect_is_delivered_without_self_dedup(monkeypatch) -> None:
    summary = "Checkout returns HTTP 500"
    signature = "network|5xx|/checkout||500 post /orders"
    captured = {}
    memory = Memory()
    assert reserve_local_defect(summary, signature=signature) is True

    def fake_create_jira_issue(**kwargs):
        captured.update(kwargs)
        return "QA-42"

    monkeypatch.setattr(defect_pipeline, "is_semantic_duplicate", lambda *_args: False)
    monkeypatch.setattr(defect_pipeline, "create_jira_issue", fake_create_jira_issue)

    defect_pipeline._create_defect_bg(
        summary,
        "description",
        "HTTP 500",
        None,
        memory,
        "critical",
        signature,
    )

    assert captured["skip_local_duplicate_check"] is True
    assert memory.created == [("QA-42", summary, "critical")]
    assert is_local_duplicate(summary, signature=signature) is True


def test_failed_delivery_releases_reservation(monkeypatch) -> None:
    summary = "Save button is blocked"
    signature = "action_failure|intercept|/form|tid:save|"
    assert reserve_local_defect(summary, signature=signature) is True

    monkeypatch.setattr(defect_pipeline, "is_semantic_duplicate", lambda *_args: False)
    monkeypatch.setattr(defect_pipeline, "create_jira_issue", lambda **_kwargs: None)

    defect_pipeline._create_defect_bg(
        summary,
        "description",
        "intercepts pointer events",
        None,
        None,
        "major",
        signature,
    )

    assert is_local_duplicate(summary, signature=signature) is False


def test_semantic_duplicate_uses_context_and_question(monkeypatch) -> None:
    captured = []

    def consult(context, question):
        captured.append((context, question))
        return "ДА"

    monkeypatch.setattr(defect_pipeline, "consult_agent", consult)

    assert defect_pipeline.is_semantic_duplicate(
        "Ошибка сервера при checkout",
        MemoryWithDefects(),
    ) is True
    assert "QA-1" in captured[0][0]
    assert "ДУБЛЬ" in captured[0][1]
