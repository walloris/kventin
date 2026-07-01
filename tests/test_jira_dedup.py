from agent.defects.jira_client import (
    build_defect_signature,
    is_local_duplicate,
    register_local_defect,
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
