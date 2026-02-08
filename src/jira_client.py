"""
Клиент Jira REST API для создания дефектов.
Создаёт только реальные баги; флаки и проблемы тестовой среды не заводим.
Поддержка как в твоём проекте: Bearer-токен (если длинный) или Basic (username, token), X-Atlassian-Token, verify=False.
"""
import os
from typing import Optional

import requests

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

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
    username: Optional[str] = None,
    email: Optional[str] = None,
    api_token: Optional[str] = None,
    project_key: Optional[str] = None,
) -> Optional[str]:
    """
    Создать дефект в Jira. Возвращает ключ задачи (например PROJ-123) или None.
    Логин: JIRA_USERNAME или JIRA_EMAIL (в зависимости от типа Jira).
    """
    jira_url = (jira_url or os.getenv("JIRA_URL", "")).rstrip("/")
    login = username or os.getenv("JIRA_USERNAME", "") or email or os.getenv("JIRA_EMAIL", "")
    api_token = api_token or os.getenv("JIRA_API_TOKEN", "")
    project_key = project_key or os.getenv("JIRA_PROJECT_KEY", "")

    if not jira_url or not api_token or not project_key:
        print("[Jira] Не заданы JIRA_URL, JIRA_API_TOKEN или JIRA_PROJECT_KEY — пропуск создания тикета.")
        return None
    # Bearer: только токен. Basic: нужен ещё логин (username/email)
    use_bearer = len(api_token) > 20
    if not use_bearer and not login:
        print("[Jira] Для короткого токена нужен JIRA_USERNAME или JIRA_EMAIL — пропуск.")
        return None

    if is_ignorable_issue(summary, description):
        print("[Jira] Пропуск: похоже на флак/тестовую среду:", summary[:80])
        return None

    url = f"{jira_url}/rest/api/2/issue"
    headers = {"Content-Type": "application/json", "X-Atlassian-Token": "no-check"}
    if use_bearer:
        headers["Authorization"] = f"Bearer {api_token}"
        auth = None
    else:
        auth = (login, api_token)

    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary[:255],
            "description": description,
            "issuetype": {"name": "Bug"},
        }
    }

    try:
        r = requests.post(url, json=payload, headers=headers, auth=auth, verify=False, timeout=30)
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
