from scripts.add_sbrppl_third_party_label import (
    LABEL,
    build_jql,
    issue_has_label,
    normalize_label,
)


def test_build_jql_selects_all_sbrppl_stories() -> None:
    assert build_jql() == "project = SBRPPL AND issuetype = Story ORDER BY key ASC"


def test_normalize_label_accepts_jira_and_ui_notation() -> None:
    assert normalize_label(LABEL) == normalize_label(f"#{LABEL}")
    assert normalize_label(f"  #{LABEL.upper()}  ") == normalize_label(LABEL)


def test_issue_has_label_handles_both_notations() -> None:
    assert issue_has_label({"fields": {"labels": ["other", LABEL]}})
    assert issue_has_label({"fields": {"labels": [f"#{LABEL}"]}})
    assert not issue_has_label({"fields": {"labels": ["other"]}})
    assert not issue_has_label({"fields": {"labels": None}})
