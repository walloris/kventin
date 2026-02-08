"""
Клиент Jira REST API для создания дефектов.
Создаёт только реальные баги; флаки и проблемы тестовой среды не заводим.
Поддержка вложений (скриншоты, логи). Bearer или Basic, X-Atlassian-Token, verify=False.
"""
import os
from typing import Optional, List, Union

import requests

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

from config import IGNORE_CONSOLE_PATTERNS, IGNORE_NETWORK_STATUSES, DEFECT_IGNORE_PATTERNS, JIRA_ISSUE_TYPE


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


def _attach_files(
    jira_url: str,
    issue_key: str,
    file_paths: List[str],
    *,
    headers_base: dict,
    auth: Optional[tuple],
    use_bearer: bool,
    api_token: str,
) -> None:
    """Приложить файлы к созданной задаче."""
    url = f"{jira_url}/rest/api/2/issue/{issue_key}/attachments"
    headers = {k: v for k, v in headers_base.items() if k.lower() != "content-type"}
    for path in file_paths:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as f:
                files = {"file": (os.path.basename(path), f)}
                r = requests.post(
                    url,
                    files=files,
                    headers=headers,
                    auth=auth if not use_bearer else None,
                    verify=False,
                    timeout=60,
                )
            if r.status_code in (200, 201):
                print(f"[Jira] Вложение: {issue_key} <- {os.path.basename(path)}")
            else:
                print(f"[Jira] Ошибка вложения {r.status_code}: {os.path.basename(path)}")
        except Exception as e:
            print(f"[Jira] Ошибка вложения {path}: {e}")


def create_jira_issue(
    summary: str,
    description: str,
    *,
    jira_url: Optional[str] = None,
    username: Optional[str] = None,
    email: Optional[str] = None,
    api_token: Optional[str] = None,
    project_key: Optional[str] = None,
    attachment_paths: Optional[List[Union[str, os.PathLike]]] = None,
) -> Optional[str]:
    """
    Создать дефект в Jira с описанием и вложениями (фактура).
    Возвращает ключ задачи (PROJ-123) или None.
    attachment_paths: список путей к файлам (скриншот, console.log, network.log и т.д.).
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
            "issuetype": {"name": JIRA_ISSUE_TYPE},
        }
    }

    try:
        r = requests.post(url, json=payload, headers=headers, auth=auth, verify=False, timeout=30)
        r.raise_for_status()
        key = r.json().get("key")
        print(f"[Jira] Создан дефект: {key}")

        if key and attachment_paths:
            paths = [os.fspath(p) for p in attachment_paths]
            _attach_files(
                jira_url, key, paths,
                headers_base=headers,
                auth=auth,
                use_bearer=use_bearer,
                api_token=api_token,
            )
        return key
    except requests.exceptions.HTTPError as e:
        print(f"[Jira] Ошибка API: {e.response.status_code} — {e.response.text[:200]}")
        return None
    except Exception as e:
        print(f"[Jira] Ошибка: {e}")
        return None
