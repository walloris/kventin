from urllib.request import urlopen

from examples.registration_demo.server import (
    BUGGY_RELEASE,
    FIXED_RELEASE,
    DemoState,
    RegistrationDemoServer,
)


VALID_REGISTRATION = {
    "fullName": "Иван Тестов",
    "email": "test@example.com",
    "password": "TestPass123!",
    "terms": True,
}


def test_known_registration_bug_is_removed_by_fixed_release() -> None:
    state = DemoState()

    status, error = state.register(VALID_REGISTRATION)
    assert state.release == BUGGY_RELEASE
    assert status == 500
    assert error["code"] == "PROFILE_INITIALIZATION_FAILED"

    state.deploy_fix()
    status, created = state.register(VALID_REGISTRATION)
    assert state.release == FIXED_RELEASE
    assert status == 201
    assert created["email"] == "test@example.com"


def test_debug_jira_tracks_ready_qa_and_fixed_lifecycle() -> None:
    state = DemoState()
    key = state.create_issue({
        "summary": "Registration returns HTTP 500",
        "description": "KVENTIN_RETEST_JSON_V1",
        "labels": ["kventin"],
    })

    assert state.mark_ready_for_qa(key)
    assert state.transition(key, "31")
    assert state.transition(key, "51", {"resolution": {"name": "Fixed"}})
    assert state.add_comment(key, "Kventin retest passed")

    issue = state.snapshot()["issues"][0]
    assert issue["status"] == "Closed"
    assert issue["resolution"] == "Fixed"
    assert issue["comments"] == ["Kventin retest passed"]


def test_debug_jira_page_renders_issue_and_release() -> None:
    server = RegistrationDemoServer().start()
    try:
        key = server.state.create_issue({
            "summary": "Registration returns HTTP 500",
            "description": (
                "h3. Описание проблемы\nВалидная регистрация завершается HTTP 500.\n\n"
                "h3. Ожидаемый результат (ОР)\nПользователь зарегистрирован.\n\n"
                "h3. Фактический результат (ФР)\nPOST /api/register возвращает 500.\n\n"
                "h3. Шаги воспроизведения\n# Открыть форму\n# Заполнить поля\n# Нажать Зарегистрироваться"
            ),
            "issuetype": {"name": "Bug"},
            "priority": {"name": "Critical"},
            "labels": ["kventin", "severity-critical"],
        })
        server.state.assign(key, "kventin-agent")
        server.state.add_attachment(key, "network.har")
        server.state.add_comment(key, "h3. Kventin retest\n{quote}Исправление подтверждено{quote}")

        with urlopen(server.base_url + "/debug/issues", timeout=3) as response:
            page = response.read().decode("utf-8")

        assert response.status == 200
        assert "Debug Jira" in page
        assert key in page
        assert BUGGY_RELEASE in page
        assert "Описание дефекта" in page
        assert "Шаги воспроизведения" in page
        assert "Ожидаемый результат (ОР)" in page
        assert "Фактический результат (ФР)" in page
        assert "Critical" in page
        assert "kventin-agent" in page
        assert "severity-critical" in page
        assert "1 файла" in page
        assert "Исправление подтверждено" in page
        assert "{quote}" not in page
    finally:
        server.stop()
