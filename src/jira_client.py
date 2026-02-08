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

# Лейбл всех дефектов, заведённых агентом
JIRA_DEFECT_LABEL = "kventin"


def _jira_request(
    method: str,
    jira_url: str,
    path: str,
    *,
    headers: dict,
    auth: Optional[tuple],
    use_bearer: bool,
    **kwargs: object,
) -> Optional[dict]:
    """Выполнить запрос к Jira API. Возвращает JSON или None."""
    url = f"{jira_url}/rest/api/2/{path.lstrip('/')}"
    kwargs.setdefault("verify", False)
    kwargs.setdefault("timeout", 30)
    if use_bearer:
        kwargs["auth"] = None
    else:
        kwargs["auth"] = auth
    kwargs["headers"] = {**headers, **kwargs.get("headers", {})}
    try:
        r = requests.request(method, url, **kwargs)
        if r.status_code in (200, 201):
            return r.json() if r.text else {}
        return None
    except Exception as e:
        print(f"[Jira] Ошибка запроса: {e}")
        return None


def search_duplicates(
    summary_part: str,
    *,
    jira_url: Optional[str] = None,
    username: Optional[str] = None,
    email: Optional[str] = None,
    api_token: Optional[str] = None,
    project_key: Optional[str] = None,
) -> Optional[str]:
    """
    Поиск дубля по summary: открытые задачи с лейблом kventin и похожим summary.
    Возвращает ключ найденной задачи (PROJ-123) или None.
    """
    jira_url = (jira_url or os.getenv("JIRA_URL", "")).rstrip("/")
    login = username or os.getenv("JIRA_USERNAME", "") or email or os.getenv("JIRA_EMAIL", "")
    api_token = api_token or os.getenv("JIRA_API_TOKEN", "")
    project_key = project_key or os.getenv("JIRA_PROJECT_KEY", "")

    if not jira_url or not api_token or not project_key:
        return None
    use_bearer = len(api_token) > 20
    if not use_bearer and not login:
        return None

    headers = {"Content-Type": "application/json", "X-Atlassian-Token": "no-check"}
    if use_bearer:
        headers["Authorization"] = f"Bearer {api_token}"
        auth = None
    else:
        auth = (login, api_token)

    safe = (summary_part or "").replace('"', "").replace("\\", "")[:50].strip()
    if not safe:
        return None
    jql = (
        f'project = {project_key} AND labels = {JIRA_DEFECT_LABEL} '
        f'AND status not in (Closed, Done) AND summary ~ "{safe}"'
    )
    res = _jira_request(
        "GET",
        jira_url,
        "search",
        params={"jql": jql, "fields": "key", "maxResults": 1},
        headers=headers,
        auth=auth,
        use_bearer=use_bearer,
    )
    if not res or "issues" not in res or not res["issues"]:
        return None
    return res["issues"][0].get("key")


def is_ignorable_issue(summary: str, description: str) -> bool:
    """
    Решение: не создавать тикет, если это типичный флак/тестовая среда.
    Игнорируем: 404 в консоли, ошибки консоли, сетевые ошибки к сторонним сервисам и т.д.
    Ошибки 5xx после действий агента не считаем флаком — тикет всегда создаём.
    """
    text = (summary + " " + description).lower()
    # Ошибки сервера (5xx) после действий агента — не флак, всегда заводим дефект
    if any(
        x in text
        for x in (
            "500", "502", "503", "5xx",
            "ошибка сервера", "server error", "internal server error",
            "http 5xx", "http 500",
        )
    ):
        return False
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

    dup = search_duplicates(
        summary,
        jira_url=jira_url,
        username=login,
        api_token=api_token,
        project_key=project_key,
    )
    if dup:
        print(f"[Jira] Дубль: не создаём, найден {dup}")
        return dup

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
            "labels": [JIRA_DEFECT_LABEL],
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
