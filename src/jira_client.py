"""
Клиент Jira REST API для создания дефектов.
Создаёт только реальные баги; флаки и проблемы тестовой среды не заводим.
"""
import os
from typing import Optional

import requests

from config import IGNORE_CONSOLE_PATTERNS, IGNORE_NETWORK_STATUSES, DEFECT_IGNORE_PATTERNS


def is_ignorable_issue(summary: str, description: str) -> bool:
    """
    Решение: не создавать тикет, если это типичный флак/тестовая среда.
    Игнорируем: 404 в консоли, ошибки консоли, сетевые ошибки к сторонним сервисам и т.д.
    """
    text = (summary + " " + description).lower()
    for pattern in DEFECT_IGNORE_PATTERNS:
        if pattern.lower() in text:
            return True
    for pattern in IGNORE_CONSOLE_PATTERNS:
        if pattern.lower() in text:
            return True
    return False


def create_jira_issue(
    summary: str,
    description: str,
    *,
    jira_url: Optional[str] = None,
    email: Optional[str] = None,
    api_token: Optional[str] = None,
    project_key: Optional[str] = None,
) -> Optional[str]:
    """
    Создать дефект в Jira. Возвращает ключ задачи (например PROJ-123) или None.
    """
    jira_url = (jira_url or os.getenv("JIRA_URL", "")).rstrip("/")
    email = email or os.getenv("JIRA_EMAIL", "")
    api_token = api_token or os.getenv("JIRA_API_TOKEN", "")
    project_key = project_key or os.getenv("JIRA_PROJECT_KEY", "")

    if not all([jira_url, email, api_token, project_key]):
        print("[Jira] Не заданы JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN или JIRA_PROJECT_KEY — пропуск создания тикета.")
        return None

    if is_ignorable_issue(summary, description):
        print("[Jira] Пропуск: похоже на флак/тестовую среду:", summary[:80])
        return None

    url = f"{jira_url}/rest/api/2/issue/"
    auth = (email, api_token)
    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary[:255],
            "description": description,
            "issuetype": {"name": "Bug"},
        }
    }

    try:
        r = requests.post(url, json=payload, auth=auth, timeout=30)
        r.raise_for_status()
        key = r.json().get("key")
        print(f"[Jira] Создан дефект: {key}")
        return key
    except requests.exceptions.HTTPError as e:
        print(f"[Jira] Ошибка API: {e.response.status_code} — {e.response.text[:200]}")
        return None
    except Exception as e:
        print(f"[Jira] Ошибка: {e}")
        return None
