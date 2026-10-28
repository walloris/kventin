from agent.defects import jira_client
from agent.defects.jira_client import (
    _attach_files,
    build_defect_signature,
    create_jira_issue,
    is_local_duplicate,
    register_local_defect,
    release_local_defect,
    reserve_local_defect,
    reset_session_defects,
)


def setup_function() -> None:
    reset_session_defects()


def teardown_function() -> None:
    reset_session_defects()


def test_build_defect_signature_requires_two_meaningful_parts() -> None:
    assert build_defect_signature(kind="network") == ""
    assert build_defect_signature(kind="network", rule="5xx").startswith("network|5xx|")


def test_local_duplicate_uses_signature_first() -> None:
    signature = build_defect_signature(
        kind="network",
        rule="5xx",
        url_pattern="/api/orders/:id",
        error_signature="GET /api/orders/1 500",
    )

    register_local_defect("HTTP 500 при загрузке заказа", signature=signature)

    assert is_local_duplicate("Серверная ошибка при открытии заказа", signature=signature) is True


def test_local_duplicate_uses_normalized_summary_similarity() -> None:
    register_local_defect("[Kventin] Кнопка Сохранить не нажимается")

    assert is_local_duplicate("Кнопка сохранить не нажимается") is True


def test_pending_reservation_is_atomic_and_releasable() -> None:
    assert reserve_local_defect("HTTP 500 на форме") is True
    assert reserve_local_defect("HTTP 500 на форме") is False

    release_local_defect("HTTP 500 на форме")

    assert reserve_local_defect("HTTP 500 на форме") is True


class Response:
    def __init__(self, status_code, data=None, text=""):
        self.status_code = status_code
        self._data = data or {}
        self.text = text
        self.headers = {}

    def json(self):
        return self._data

    def raise_for_status(self):
        return None


def test_jira_create_recovers_key_after_lost_response(monkeypatch) -> None:
    searches = iter([None, "QA-42"])
    posts = []
    monkeypatch.setattr(jira_client, "is_ignorable_issue", lambda *_args: False)
    monkeypatch.setattr(jira_client, "search_duplicates", lambda *_args, **_kwargs: next(searches))
    monkeypatch.setattr(
        jira_client,
        "_jira_http_request",
        lambda *_args, **_kwargs: posts.append(True) or Response(500, text="proxy error"),
    )

    key = create_jira_issue(
        "Checkout HTTP 500",
        "description",
        jira_url="https://jira.example.test",
        api_token="x" * 30,
        project_key="QA",
    )

    assert key == "QA-42"
    assert posts == [True]


def test_jira_success_without_key_is_not_registered(monkeypatch) -> None:
    summary = "Malformed Jira response"
    monkeypatch.setattr(jira_client, "is_ignorable_issue", lambda *_args: False)
    monkeypatch.setattr(jira_client, "search_duplicates", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        jira_client,
        "_jira_http_request",
        lambda *_args, **_kwargs: Response(201, data={}, text="{}"),
    )

    key = create_jira_issue(
        summary,
        "description",
        jira_url="https://jira.example.test",
        api_token="x" * 30,
        project_key="QA",
    )

    assert key is None
    assert is_local_duplicate(summary) is False


def test_jira_create_keeps_severity_as_label_without_priority(monkeypatch) -> None:
    payloads = []
    monkeypatch.setattr(jira_client, "is_ignorable_issue", lambda *_args: False)
    monkeypatch.setattr(jira_client, "search_duplicates", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jira_client, "JIRA_PRIORITY_CRITICAL", "")
    monkeypatch.setattr(jira_client, "JIRA_ASSIGNEE", "")

    def fake_request(method, _url, **kwargs):
        if method == "POST":
            payloads.append(kwargs["json"])
            return Response(201, data={"key": "QA-43"})
        raise AssertionError("Unexpected Jira request: %s" % method)

    monkeypatch.setattr(jira_client, "_jira_http_request", fake_request)

    key = create_jira_issue(
        "Checkout HTTP 500",
        "Detailed description with reproduction steps",
        jira_url="https://jira.example.test",
        api_token="x" * 30,
        project_key="QA",
        severity="critical",
    )

    assert key == "QA-43"
    fields = payloads[0]["fields"]
    assert fields["description"] == "Detailed description with reproduction steps"
    assert fields["issuetype"]["name"]
    assert "severity-critical" in fields["labels"]
    assert "priority" not in fields


def test_attachment_lost_response_is_recovered_without_duplicate(tmp_path, monkeypatch) -> None:
    path = tmp_path / "evidence.log"
    path.write_bytes(b"full evidence")
    posted = []
    uploaded = False

    def fake_request(method, _url, **kwargs):
        nonlocal uploaded
        if method == "POST":
            posted.append(kwargs["files"]["file"][1].read())
            uploaded = True
            return None
        attachments = []
        if uploaded:
            attachments.append({"filename": path.name, "size": path.stat().st_size})
        return Response(
            200,
            data={"fields": {"attachment": attachments}},
            text="attachments",
        )

    monkeypatch.setattr(jira_client, "_jira_http_request", fake_request)

    attached = _attach_files(
        "https://jira.example.test",
        "QA-42",
        [str(path)],
        headers_base={},
        auth=None,
        use_bearer=True,
    )

    assert attached is True
    assert posted == [b"full evidence"]
