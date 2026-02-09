"""
Клиент Jira REST API для создания дефектов.
Создаёт только реальные баги; флаки и проблемы тестовой среды не заводим.
Поддержка вложений (скриншоты, логи). Bearer или Basic, X-Atlassian-Token, verify=False.
Многоуровневая дедупликация: локальная (память сессии) → Jira (JQL) → GigaChat (семантика).
"""
import os
import re
import logging
from typing import Optional, List, Union, Set

import requests

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

from config import IGNORE_CONSOLE_PATTERNS, IGNORE_NETWORK_STATUSES, DEFECT_IGNORE_PATTERNS, JIRA_ISSUE_TYPE, JIRA_ASSIGNEE

LOG = logging.getLogger("Jira")

# Лейбл всех дефектов, заведённых агентом
JIRA_DEFECT_LABEL = "kventin"

# =============================================
# Локальная дедупликация (в памяти процесса)
# =============================================
_session_defect_keys: Set[str] = set()  # нормализованные ключи дефектов за сессию


def _normalize_defect_key(text: str) -> str:
    """Нормализовать текст бага для сравнения: без пунктуации, lowercase, без стоп-слов."""
    if not text:
        return ""
    t = text.lower().strip()
    # Убрать [Kventin] префикс
    t = re.sub(r'\[kventin\]\s*', '', t)
    # Убрать URL-ы
    t = re.sub(r'https?://\S+', '', t)
    # Убрать пунктуацию
    t = re.sub(r'[^\w\sа-яёА-ЯЁ]', ' ', t)
    # Схлопнуть пробелы
    t = re.sub(r'\s+', ' ', t).strip()
    # Обрезать до 120 символов
    return t[:120]


def _similarity(a: str, b: str) -> float:
    """Простая метрика схожести: Jaccard по словам (bigrams для коротких текстов)."""
    if not a or not b:
        return 0.0
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union) if union else 0.0


def is_local_duplicate(summary: str, description: str = "") -> bool:
    """
    Проверить дедупликацию внутри текущей сессии.
    Если summary/description похожи на уже созданный дефект — дубль.
    """
    key = _normalize_defect_key(summary)
    if not key:
        return False
    # Точное совпадение ключа
    if key in _session_defect_keys:
        LOG.info("Локальный дубль (точный): %s", summary[:60])
        return True
    # Нечёткое: Jaccard > 0.6
    for existing in _session_defect_keys:
        sim = _similarity(key, existing)
        if sim > 0.6:
            LOG.info("Локальный дубль (sim=%.2f): '%s' ~ '%s'", sim, key[:40], existing[:40])
            return True
    return False


def register_local_defect(summary: str):
    """Запомнить дефект в памяти сессии для дедупликации."""
    key = _normalize_defect_key(summary)
    if key:
        _session_defect_keys.add(key)


def reset_session_defects():
    """Сбросить локальный кеш (при перезапуске агента)."""
    _session_defect_keys.clear()


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


def _extract_search_keywords(text: str, max_words: int = 6) -> str:
    """Извлечь ключевые слова из summary для JQL-поиска (убрать стоп-слова, оставить суть)."""
    stop_words = {
        "на", "в", "и", "с", "не", "по", "к", "от", "за", "из", "для", "при", "что", "это",
        "the", "is", "at", "on", "in", "to", "for", "a", "an", "of", "with",
        "kventin", "ошибка", "error", "проблема", "баг", "bug", "http", "после", "страниц",
    }
    text = re.sub(r'\[kventin\]\s*', '', text.lower())
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'[^\w\sа-яёА-ЯЁ]', ' ', text)
    words = [w for w in text.split() if len(w) > 2 and w not in stop_words]
    # Берём уникальные слова, не больше max_words
    seen = []
    for w in words:
        if w not in seen:
            seen.append(w)
        if len(seen) >= max_words:
            break
    return " ".join(seen)


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
    Поиск дубля в Jira: открытые задачи с лейблом kventin и похожим summary.
    Двухуровневый: сначала точный поиск, потом по ключевым словам.
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

    # --- Поиск 1: по подстроке summary (точный) ---
    safe = (summary_part or "").replace('"', "").replace("\\", "")[:80].strip()
    if safe:
        jql = (
            f'project = {project_key} AND labels = {JIRA_DEFECT_LABEL} '
            f'AND status not in (Closed, Done, Resolved) AND summary ~ "{safe[:50]}"'
        )
        res = _jira_request(
            "GET", jira_url, "search",
            params={"jql": jql, "fields": "key,summary", "maxResults": 5},
            headers=headers, auth=auth, use_bearer=use_bearer,
        )
        if res and res.get("issues"):
            # Проверяем similarity с каждым результатом
            norm_input = _normalize_defect_key(summary_part)
            for issue in res["issues"]:
                existing_summary = issue.get("fields", {}).get("summary", "")
                norm_existing = _normalize_defect_key(existing_summary)
                sim = _similarity(norm_input, norm_existing)
                if sim > 0.5:
                    key = issue.get("key", "?")
                    LOG.info("Jira дубль (sim=%.2f): %s — '%s'", sim, key, existing_summary[:60])
                    return key

    # --- Поиск 2: по ключевым словам (широкий) ---
    keywords = _extract_search_keywords(summary_part)
    if keywords and len(keywords.split()) >= 2:
        kw_safe = keywords.replace('"', '').replace('\\', '')[:60]
        jql2 = (
            f'project = {project_key} AND labels = {JIRA_DEFECT_LABEL} '
            f'AND status not in (Closed, Done, Resolved) AND text ~ "{kw_safe}"'
        )
        res2 = _jira_request(
            "GET", jira_url, "search",
            params={"jql": jql2, "fields": "key,summary", "maxResults": 5},
            headers=headers, auth=auth, use_bearer=use_bearer,
        )
        if res2 and res2.get("issues"):
            norm_input = _normalize_defect_key(summary_part)
            for issue in res2["issues"]:
                existing_summary = issue.get("fields", {}).get("summary", "")
                norm_existing = _normalize_defect_key(existing_summary)
                sim = _similarity(norm_input, norm_existing)
                if sim > 0.5:
                    key = issue.get("key", "?")
                    LOG.info("Jira дубль по keywords (sim=%.2f): %s — '%s'", sim, key, existing_summary[:60])
                    return key

    return None


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
        LOG.info("Пропуск: похоже на флак/тестовую среду: %s", summary[:80])
        return None

    # Уровень 1: локальная дедупликация (в памяти сессии)
    if is_local_duplicate(summary, description):
        LOG.info("Пропуск (локальный дубль): %s", summary[:80])
        return None

    # Уровень 2: дедупликация через Jira (JQL поиск)
    dup = search_duplicates(
        summary,
        jira_url=jira_url,
        username=login,
        api_token=api_token,
        project_key=project_key,
    )
    if dup:
        LOG.info("Дубль в Jira: не создаём, найден %s", dup)
        register_local_defect(summary)  # запомнить чтобы не искать повторно
        return dup

    url = f"{jira_url}/rest/api/2/issue"
    headers = {"Content-Type": "application/json", "X-Atlassian-Token": "no-check"}
    if use_bearer:
        headers["Authorization"] = f"Bearer {api_token}"
        auth = None
    else:
        auth = (login, api_token)

    # Assignee: если задан JIRA_ASSIGNEE — используем его, иначе — текущего пользователя (login)
    assignee_value = None
    if JIRA_ASSIGNEE:
        assignee_value = JIRA_ASSIGNEE
    elif login:
        assignee_value = login
    
    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary[:255],
            "description": description,
            "issuetype": {"name": JIRA_ISSUE_TYPE},
            "labels": [JIRA_DEFECT_LABEL],
        }
    }
    
    # Добавляем assignee, если указан
    if assignee_value:
        # Для Jira Server: {"name": "username"}
        # Для Jira Cloud: {"accountId": "..."} или {"name": "email"} (зависит от настроек)
        # Пробуем универсальный вариант: если assignee_value похож на accountId (UUID) — используем accountId, иначе name
        if len(assignee_value) > 30 and "-" in assignee_value:
            # Похоже на accountId (UUID формат: 557058:xxx-xxx-xxx)
            payload["fields"]["assignee"] = {"accountId": assignee_value}
            LOG.info("Assignee: accountId=%s", assignee_value)
        else:
            # Username или email
            payload["fields"]["assignee"] = {"name": assignee_value}
            LOG.info("Assignee: name=%s", assignee_value)

    try:
        r = requests.post(url, json=payload, headers=headers, auth=auth, verify=False, timeout=30)
        r.raise_for_status()
        key = r.json().get("key")
        assignee_info = f" (assignee: {assignee_value})" if assignee_value else ""
        LOG.info("Создан дефект: %s%s", key, assignee_info)
        register_local_defect(summary)  # запомнить для дедупликации

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
