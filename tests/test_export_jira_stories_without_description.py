from scripts.export_jira_stories_without_description import (
    build_jql,
    is_empty_description,
    issue_to_row,
    sanitize_sheet_title,
)


def test_build_jql_filters_story_without_description_for_last_year() -> None:
    jql = build_jql(project_key="HRM", issue_type="Story", days_back=365)

    assert jql == (
        "project = HRM "
        "AND issuetype = Story "
        "AND description IS EMPTY "
        "AND created >= -365d "
        "ORDER BY created DESC"
    )


def test_is_empty_description_handles_plain_text_and_adf() -> None:
    assert is_empty_description(None) is True
    assert is_empty_description("   ") is True
    assert is_empty_description({"type": "doc", "content": []}) is True
    assert (
        is_empty_description(
            {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Описание есть"}],
                    }
                ],
            }
        )
        is False
    )


def test_issue_to_row_extracts_expected_fields() -> None:
    issue = {
        "key": "HRM-123",
        "fields": {
            "summary": "Story summary",
            "status": {"name": "Open"},
            "assignee": None,
            "reporter": {"displayName": "Иван Иванов"},
            "created": "2026-01-15T10:20:30.000+0300",
            "updated": "2026-02-15T10:20:30.000+0300",
            "priority": {"name": "Major"},
            "labels": ["one", "two"],
        },
    }

    assert issue_to_row(issue, "https://jira.example.ru") == [
        "HRM-123",
        "Story summary",
        "Open",
        "Не назначен",
        "Иван Иванов",
        "2026-01-15T10:20:30.000+0300",
        "2026-02-15T10:20:30.000+0300",
        "Major",
        "one, two",
        "https://jira.example.ru/browse/HRM-123",
    ]


def test_sanitize_sheet_title_is_unique_and_excel_compatible() -> None:
    used_titles: set[str] = set()

    assert sanitize_sheet_title("SPACE/NAME:LONG", used_titles) == "SPACE_NAME_LONG"
    assert sanitize_sheet_title("SPACE/NAME:LONG", used_titles) == "SPACE_NAME_LONG_2"
