"""Add the #Пульс_3лица label to Story, Task, and Bug issues in SBRPPL.

Jira stores this label as ``Пульс_3лица``; the leading ``#`` is only the
human-readable notation used in the project.

Usage:
    python scripts/add_sbrppl_third_party_label.py
    python scripts/add_sbrppl_third_party_label.py --apply

The first command is a dry run. Pass ``--apply`` to update Jira.
Connection settings are loaded exactly like in
``export_jira_stories_without_description.py``: first from environment/.env,
then from the repository's legacy ``config.py``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Iterator, Sequence

script_dir = Path(__file__).resolve().parent
parent_dir = script_dir.parent
if str(parent_dir) not in sys.path:
    sys.path.insert(0, str(parent_dir))
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from export_jira_stories_without_description import (  # noqa: E402
    JiraSettings,
    jira_auth,
    jql_value,
    load_jira_settings,
)

PROJECT_KEY = "SBRPPL"
ISSUE_TYPES = ("Story", "Task", "Bug")
LABEL = "Пульс_3лица"
PAGE_SIZE = 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Добавляет лейбл #Пульс_3лица всем Story, Task и Bug проекта SBRPPL, "
            "у которых его ещё нет."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Внести изменения в Jira. Без флага выполняется только dry-run.",
    )
    parser.add_argument(
        "--max-issues",
        type=int,
        default=0,
        help="Ограничить число найденных задач; 0 означает без ограничения.",
    )
    parser.add_argument(
        "--auth-mode",
        choices=("token", "basic"),
        default=None,
        help="Авторизация: token=Bearer token, basic=login+token.",
    )
    return parser.parse_args()


def build_jql(
    project_key: str = PROJECT_KEY,
    issue_types: Sequence[str] = ISSUE_TYPES,
) -> str:
    """Build JQL for all selected issue types in the target project."""

    if not issue_types:
        raise ValueError("Список типов задач пустой.")
    issue_types_jql = ", ".join(jql_value(issue_type) for issue_type in issue_types)
    return (
        f"project = {jql_value(project_key)} "
        f"AND issuetype in ({issue_types_jql}) "
        "ORDER BY key ASC"
    )


def normalize_label(value: Any) -> str:
    """Normalize Jira/UI label notation for a case-insensitive comparison."""

    return str(value or "").strip().lstrip("#").casefold()


def issue_has_label(issue: dict[str, Any], label: str = LABEL) -> bool:
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        return False
    labels = fields.get("labels") or []
    if not isinstance(labels, list):
        return False
    expected = normalize_label(label)
    return any(normalize_label(existing) == expected for existing in labels)


def request_search_page(
    settings: JiraSettings,
    *,
    jql: str,
    start_at: int,
    max_results: int,
) -> dict[str, Any]:
    """Fetch one Jira search page."""

    try:
        import requests
        import urllib3
    except ImportError as exc:
        raise RuntimeError(
            "Не установлены requests/urllib3. Выполните: pip install -r requirements.txt"
        ) from exc

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    headers, auth = jira_auth(settings)
    response = requests.post(
        f"{settings.url}/rest/api/2/search",
        headers=headers,
        auth=auth,
        verify=settings.verify_ssl,
        timeout=60,
        json={
            "jql": jql,
            "startAt": start_at,
            "maxResults": max_results,
            "fields": ["summary", "labels"],
        },
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Jira search failed: HTTP {response.status_code}; "
            f"response: {response.text[:500]}"
        )
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Jira search returned an unexpected response.")
    return data


def iter_issues(
    settings: JiraSettings,
    *,
    max_issues: int = 0,
    page_size: int = PAGE_SIZE,
) -> Iterator[dict[str, Any]]:
    """Yield every selected SBRPPL issue, following Jira pagination."""

    if max_issues < 0:
        raise ValueError("--max-issues не может быть отрицательным.")

    start_at = 0
    emitted = 0
    jql = build_jql()
    while True:
        remaining = max_issues - emitted if max_issues else page_size
        current_page_size = min(page_size, remaining) if max_issues else page_size
        if current_page_size <= 0:
            return

        data = request_search_page(
            settings,
            jql=jql,
            start_at=start_at,
            max_results=current_page_size,
        )
        issues = data.get("issues") or []
        if not isinstance(issues, list) or not issues:
            return

        for issue in issues:
            if isinstance(issue, dict):
                yield issue
                emitted += 1
                if max_issues and emitted >= max_issues:
                    return

        start_at += len(issues)
        if start_at >= int(data.get("total") or 0):
            return


def add_label(settings: JiraSettings, issue_key: str, label: str = LABEL) -> None:
    """Atomically add one label without replacing existing issue labels."""

    try:
        import requests
        import urllib3
    except ImportError as exc:
        raise RuntimeError(
            "Не установлены requests/urllib3. Выполните: pip install -r requirements.txt"
        ) from exc

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    headers, auth = jira_auth(settings)
    response = requests.put(
        f"{settings.url}/rest/api/2/issue/{issue_key}",
        headers=headers,
        auth=auth,
        verify=settings.verify_ssl,
        timeout=60,
        json={"update": {"labels": [{"add": label}]}},
    )
    if response.status_code not in (200, 204):
        raise RuntimeError(
            f"{issue_key}: HTTP {response.status_code}; response: {response.text[:500]}"
        )


def issue_summary(issue: dict[str, Any]) -> str:
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        return ""
    return str(fields.get("summary") or "").strip()


def main() -> int:
    args = parse_args()
    try:
        settings = load_jira_settings(auth_mode_arg=args.auth_mode)
        issues = list(iter_issues(settings, max_issues=args.max_issues))
        missing = [issue for issue in issues if not issue_has_label(issue)]

        print(f"Найдено Story/Task/Bug в {PROJECT_KEY}: {len(issues)}")
        print(f"Уже с лейблом #{LABEL}: {len(issues) - len(missing)}")
        print(f"Нужно обновить: {len(missing)}")

        if not args.apply:
            for issue in missing:
                print(f"[dry-run] {issue.get('key', '?')}: {issue_summary(issue)}")
            print("Изменения не внесены. Для применения запустите с флагом --apply.")
            return 0

        updated = 0
        for issue in missing:
            issue_key = str(issue.get("key") or "").strip()
            if not issue_key:
                raise RuntimeError("Jira вернула задачу без key.")
            add_label(settings, issue_key)
            updated += 1
            print(f"[{updated}/{len(missing)}] Обновлена {issue_key}")
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    print(f"Готово. Обновлено задач: {updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
