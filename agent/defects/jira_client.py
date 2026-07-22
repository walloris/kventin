"""
Клиент Jira REST API для создания дефектов.
Создаёт только реальные баги; флаки и проблемы тестовой среды не заводим.
Поддержка вложений (скриншоты, логи). Bearer или Basic, X-Atlassian-Token.
Многоуровневая дедупликация: локальная (память сессии) → Jira (JQL) → LLM (семантика).
"""
import copy
import os
import random
import re
import logging
import threading
import time
from typing import Optional, List, Union, Set

import requests

from agent.core.resilience import RetryPolicy, parse_retry_after

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

from config import (
    DEFECT_IGNORE_PATTERNS,
    JIRA_ASSIGNEE,
    JIRA_ISSUE_TYPE,
    JIRA_PRIORITY_CRITICAL,
    JIRA_PRIORITY_MAJOR,
    JIRA_PRIORITY_MINOR,
    JIRA_RETEST_RESOLUTION_FIXED,
    JIRA_RETEST_STATUS_IN_PROGRESS,
    JIRA_RETEST_STATUS_QA,
    JIRA_RETEST_STATUS_RESOLVED,
    JIRA_RETRY_BASE_DELAY,
    JIRA_RETRY_COUNT,
    JIRA_RETRY_MAX_DELAY,
    JIRA_VERIFY_SSL,
)

LOG = logging.getLogger("Jira")

# Лейбл всех дефектов, заведённых агентом
JIRA_DEFECT_LABEL = "kventin"
JIRA_RETRYABLE_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


def _jira_retry_policy() -> RetryPolicy:
    return RetryPolicy(
        max_attempts=max(1, int(JIRA_RETRY_COUNT)),
        base_delay=max(0.0, float(JIRA_RETRY_BASE_DELAY)),
        max_delay=max(0.0, float(JIRA_RETRY_MAX_DELAY)),
        retryable_statuses=JIRA_RETRYABLE_HTTP_STATUSES,
    )


def _jira_http_request(
    method: str,
    url: str,
    *,
    retry_mutating: bool = False,
    max_attempts: Optional[int] = None,
    **kwargs: object,
) -> Optional[requests.Response]:
    """Execute a bounded Jira request with conservative mutation retries."""
    policy = _jira_retry_policy()
    method_upper = method.upper()
    can_retry = method_upper in {"GET", "HEAD", "OPTIONS"} or retry_mutating
    attempts = policy.attempts() if can_retry else 1
    if max_attempts is not None:
        attempts = min(attempts, max(1, int(max_attempts)))
    for attempt in range(attempts):
        try:
            response = requests.request(method_upper, url, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if attempt >= attempts - 1:
                LOG.warning("Jira transport error %s %s: %s", method_upper, url, exc)
                return None
            delay = policy.delay_for(attempt, random_fn=random.uniform)
            LOG.warning(
                "Jira transport error, retry %d/%d in %.1fs: %s",
                attempt + 1,
                attempts,
                delay,
                exc,
            )
            time.sleep(delay)
            continue
        except requests.exceptions.RequestException as exc:
            LOG.warning("Jira request failed %s %s: %s", method_upper, url, exc)
            return None

        if response.status_code not in policy.retryable_statuses or attempt >= attempts - 1:
            return response
        delay = policy.delay_for(
            attempt,
            retry_after=parse_retry_after(response.headers),
            random_fn=random.uniform,
        )
        LOG.warning(
            "Jira HTTP %s, retry %d/%d in %.1fs: %s",
            response.status_code,
            attempt + 1,
            attempts,
            delay,
            (response.text or "")[:300],
        )
        time.sleep(delay)
    return None

# =============================================
# Локальная дедупликация (в памяти процесса)
# =============================================
# Многоуровневая дедупликация:
#   Уровень A — структурная сигнатура (signature_key):
#       (kind, rule, url_pattern, locator_or_message_signature)
#       Точное совпадение ⇒ дубль. Это самый надёжный путь — не зависит от
#       текста summary, который у LLM «плавает».
#   Уровень B — нормализованный summary (как раньше): для случаев без сигнатуры.
_session_defect_keys: Set[str] = set()        # уровень B: нормализованный summary
_session_signatures: Set[str] = set()         # уровень A: компактная сигнатура
_pending_defect_keys: Set[str] = set()
_pending_signatures: Set[str] = set()
_dedup_lock = threading.RLock()


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


def build_defect_signature(
    *,
    kind: str = "",
    rule: str = "",
    url_pattern: str = "",
    locator: str = "",
    error_signature: str = "",
) -> str:
    """
    Скомпоновать стабильную сигнатуру дефекта.

    Идея: один и тот же реальный баг должен давать ОДНУ сигнатуру вне зависимости
    от того, как LLM сформулировал summary. Поля, которые мы кладём:
      - kind:           page_load / action_failure / pageerror / network / a11y / ...
      - rule:           конкретное правило-источник (intercept / 5xx / 4xx_main / ...)
      - url_pattern:    URL без query, с :id/:uuid вместо динамики
      - locator:        канонический локатор, если применимо
      - error_signature: «топовый кадр стека» / 'STATUS METHOD path' / типа того

    Для сравнения нормализуем каждое поле и склеиваем через '|'. Если поля пусты —
    сигнатура считается слабой и НЕ кладётся в _session_signatures (чтобы не
    блокировать совершенно разные дефекты с одинаково пустыми полями).
    """
    def _n(s: str) -> str:
        s = (s or "").strip().lower()
        s = re.sub(r"\s+", " ", s)
        return s[:200]
    parts = [_n(kind), _n(rule), _n(url_pattern), _n(locator), _n(error_signature)]
    sig = "|".join(parts)
    # Минимальная «интересность»: должно быть хотя бы 2 непустых поля.
    if sum(1 for p in parts if p) < 2:
        return ""
    return sig


def is_local_duplicate(
    summary: str,
    description: str = "",
    *,
    signature: str = "",
) -> bool:
    """
    Проверить дедупликацию внутри текущей сессии.

    Сначала — структурная сигнатура (если задана). Совпадение ⇒ дубль.
    Затем — нормализованный summary (точное совпадение или Jaccard > 0.6).
    """
    with _dedup_lock:
        if signature and signature in (_session_signatures | _pending_signatures):
            LOG.info("Локальный дубль (сигнатура): %s", summary[:80])
            return True

        key = _normalize_defect_key(summary)
        if not key:
            return False
        all_keys = _session_defect_keys | _pending_defect_keys
        if key in all_keys:
            LOG.info("Локальный дубль (точный): %s", summary[:60])
            return True
        for existing in all_keys:
            sim = _similarity(key, existing)
            if sim > 0.6:
                LOG.info("Локальный дубль (sim=%.2f): '%s' ~ '%s'", sim, key[:40], existing[:40])
                return True
    return False


def register_local_defect(summary: str, *, signature: str = "") -> None:
    """Запомнить дефект в памяти сессии для дедупликации (summary + сигнатура)."""
    key = _normalize_defect_key(summary)
    with _dedup_lock:
        if key:
            _pending_defect_keys.discard(key)
            _session_defect_keys.add(key)
        if signature:
            _pending_signatures.discard(signature)
            _session_signatures.add(signature)


def reserve_local_defect(summary: str, *, signature: str = "") -> bool:
    """Atomically reserve a defect while Jira delivery is in flight."""
    with _dedup_lock:
        if is_local_duplicate(summary, signature=signature):
            return False
        key = _normalize_defect_key(summary)
        if key:
            _pending_defect_keys.add(key)
        if signature:
            _pending_signatures.add(signature)
        return True


def release_local_defect(summary: str, *, signature: str = "") -> None:
    """Release a failed delivery so a later observation can retry it."""
    key = _normalize_defect_key(summary)
    with _dedup_lock:
        if key:
            _pending_defect_keys.discard(key)
        if signature:
            _pending_signatures.discard(signature)


def reset_session_defects() -> None:
    """Сбросить локальный кеш (при перезапуске агента)."""
    with _dedup_lock:
        _session_defect_keys.clear()
        _session_signatures.clear()
        _pending_defect_keys.clear()
        _pending_signatures.clear()


def _jira_request(
    method: str,
    jira_url: str,
    path: str,
    *,
    headers: dict,
    auth: Optional[tuple],
    use_bearer: bool,
    max_attempts: Optional[int] = None,
    **kwargs: object,
) -> Optional[dict]:
    """Выполнить запрос к Jira API. Возвращает JSON или None."""
    url = f"{jira_url}/rest/api/2/{path.lstrip('/')}"
    kwargs.setdefault("verify", JIRA_VERIFY_SSL)
    kwargs.setdefault("timeout", 30)
    if use_bearer:
        kwargs["auth"] = None
    else:
        kwargs["auth"] = auth
    kwargs["headers"] = {**headers, **kwargs.get("headers", {})}
    try:
        r = _jira_http_request(method, url, max_attempts=max_attempts, **kwargs)
        if r is None:
            return None
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
    broad: bool = True,
    request_attempts: Optional[int] = None,
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
            max_attempts=request_attempts,
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
    if not broad:
        return None

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
            max_attempts=request_attempts,
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

    ВАЖНО: применяется ТОЛЬКО `DEFECT_IGNORE_PATTERNS`. Список
    `IGNORE_CONSOLE_PATTERNS` — это фильтр для записей console.log
    (содержит "404", "localhost", "favicon" и т.п.). Применять его к описанию
    дефекта нельзя: URL стенда часто содержит "localhost", а в любом
    нормальном баге может встретиться "консоли" / "console" — и тогда
    тикет молча отбрасывается. Эта ловушка раньше съедала все дефекты.

    5xx ошибки сервера и Playwright-фейлы клика (intercept/timeout) проходят
    мимо фильтра — это всегда баги.
    """
    text = (summary + " " + description).lower()
    # Ошибки сервера (5xx) после действий агента — не флак, всегда заводим дефект.
    if any(
        x in text
        for x in (
            "ошибка сервера", "server error", "internal server error",
            "http 5xx", "http 500", "http 502", "http 503",
        )
    ):
        return False
    # Фейлы клика и таймауты Playwright — реальные UI-баги, не флак.
    if any(
        x in text
        for x in (
            "intercepts pointer events",
            "не становится кликабельным",
            "перекрыта другим элементом",
        )
    ):
        return False
    for pattern in DEFECT_IGNORE_PATTERNS:
        if pattern.lower() in text:
            LOG.info("is_ignorable_issue: совпал паттерн '%s' — пропуск '%s'", pattern, summary[:80])
            return True
    return False


def _assign_issue(
    jira_url: str,
    issue_key: str,
    assignee_value: str,
    *,
    headers: dict,
    auth: Optional[tuple],
    use_bearer: bool,
) -> bool:
    """
    Назначить задачу на пользователя отдельным PUT-запросом.
    Пробует несколько форматов: name → accountId → emailAddress.
    """
    url = f"{jira_url}/rest/api/2/issue/{issue_key}/assignee"
    h = {k: v for k, v in headers.items()}

    # Определяем все варианты payload в порядке приоритета
    attempts = []
    if len(assignee_value) > 30 and "-" in assignee_value:
        # Похоже на accountId
        attempts.append(("accountId", {"accountId": assignee_value}))
        attempts.append(("name", {"name": assignee_value}))
    elif "@" in assignee_value:
        # Похоже на email
        attempts.append(("name", {"name": assignee_value}))
        attempts.append(("emailAddress", {"emailAddress": assignee_value}))
    else:
        # Username
        attempts.append(("name", {"name": assignee_value}))
        attempts.append(("accountId", {"accountId": assignee_value}))

    for label, body in attempts:
        try:
            r = _jira_http_request(
                "PUT",
                url,
                retry_mutating=True,
                json=body,
                headers=h,
                auth=auth if not use_bearer else None,
                verify=JIRA_VERIFY_SSL,
                timeout=15,
            )
            if r is None:
                continue
            if r.status_code in (200, 204):
                LOG.info("Assignee %s: %s=%s", issue_key, label, assignee_value)
                return True
            LOG.debug("Assignee попытка %s: %s — %s", label, r.status_code, r.text[:150])
        except Exception as e:
            LOG.debug("Assignee попытка %s: %s", label, e)

    LOG.warning("Не удалось назначить %s на %s. Проверьте JIRA_ASSIGNEE.", issue_key, assignee_value)
    return False


def _jira_attachment_exists(
    jira_url: str,
    issue_key: str,
    filename: str,
    file_size: int,
    *,
    headers: dict,
    auth: Optional[tuple],
    use_bearer: bool,
) -> bool:
    response = _jira_http_request(
        "GET",
        f"{jira_url}/rest/api/2/issue/{issue_key}",
        params={"fields": "attachment"},
        headers=headers,
        auth=auth if not use_bearer else None,
        verify=JIRA_VERIFY_SSL,
        timeout=30,
    )
    if response is None or response.status_code != 200:
        return False
    try:
        attachments = ((response.json() or {}).get("fields") or {}).get("attachment") or []
    except Exception:
        return False
    return any(
        str(item.get("filename") or "") == filename
        and int(item.get("size") or -1) == file_size
        for item in attachments
        if isinstance(item, dict)
    )


def _attach_files(
    jira_url: str,
    issue_key: str,
    file_paths: List[str],
    *,
    headers_base: dict,
    auth: Optional[tuple],
    use_bearer: bool,
) -> bool:
    """Приложить файлы к созданной задаче."""
    url = f"{jira_url}/rest/api/2/issue/{issue_key}/attachments"
    headers = {k: v for k, v in headers_base.items() if k.lower() != "content-type"}
    all_attached = True
    policy = _jira_retry_policy()
    for path in file_paths:
        if not path or not os.path.isfile(path):
            all_attached = False
            continue
        filename = os.path.basename(path)
        file_size = os.path.getsize(path)
        attached = False
        if _jira_attachment_exists(
            jira_url,
            issue_key,
            filename,
            file_size,
            headers=headers,
            auth=auth,
            use_bearer=use_bearer,
        ):
            LOG.info("Attachment already exists: %s on %s", filename, issue_key)
            continue
        for attempt in range(policy.attempts()):
            try:
                with open(path, "rb") as file_handle:
                    response = _jira_http_request(
                        "POST",
                        url,
                        retry_mutating=False,
                        files={"file": (filename, file_handle)},
                        headers=headers,
                        auth=auth if not use_bearer else None,
                        verify=JIRA_VERIFY_SSL,
                        timeout=60,
                    )
            except Exception as exc:
                LOG.warning("Attachment %s failed: %s", filename, exc)
                response = None

            if response is not None and response.status_code in (200, 201):
                attached = True
                break
            if _jira_attachment_exists(
                jira_url,
                issue_key,
                filename,
                file_size,
                headers=headers,
                auth=auth,
                use_bearer=use_bearer,
            ):
                LOG.info("Attachment response was lost; found %s on %s", filename, issue_key)
                attached = True
                break
            if (
                response is not None
                and response.status_code not in policy.retryable_statuses
            ) or attempt >= policy.attempts() - 1:
                break
            delay = policy.delay_for(
                attempt,
                retry_after=parse_retry_after(response.headers) if response is not None else None,
                random_fn=random.uniform,
            )
            time.sleep(delay)

        if attached:
            print(f"[Jira] Вложение: {issue_key} <- {filename}")
        else:
            all_attached = False
            status = response.status_code if response is not None else "нет ответа"
            print(f"[Jira] Ошибка вложения {status}: {filename}")
    return all_attached


def _jira_priority_name_for_severity(severity: Optional[str]) -> Optional[str]:
    """
    Вернуть имя приоритета из config или None, если priority не задан/не маппится.
    None — поле priority в payload не кладём (переносимо между Jira-инсталляциями).
    """
    s = (severity or "").lower().strip()
    if s not in ("critical", "major", "minor"):
        return None
    m = {
        "critical": JIRA_PRIORITY_CRITICAL,
        "major": JIRA_PRIORITY_MAJOR,
        "minor": JIRA_PRIORITY_MINOR,
    }
    name = (m.get(s) or "").strip()
    return name or None


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
    severity: Optional[str] = None,
    skip_local_duplicate_check: bool = False,
) -> Optional[str]:
    """
    Создать дефект в Jira с описанием и вложениями (фактура).
    Возвращает ключ задачи (PROJ-123) или None.
    attachment_paths: список путей к файлам (скриншот, console.log, network.log и т.д.).
    severity: critical | major | minor — если заданы JIRA_PRIORITY_* в config, в поле
    priority кладётся соответствующее имя; иначе priority не передаётся. При 400
    из-за неверного имени — один повтор без поля priority.
    """
    jira_url = (jira_url or os.getenv("JIRA_URL", "")).rstrip("/")
    login = username or os.getenv("JIRA_USERNAME", "") or email or os.getenv("JIRA_EMAIL", "")
    api_token = api_token or os.getenv("JIRA_API_TOKEN", "")
    project_key = project_key or os.getenv("JIRA_PROJECT_KEY", "")

    if not jira_url or not api_token or not project_key:
        missing = []
        if not jira_url:
            missing.append("JIRA_URL")
        if not api_token:
            missing.append("JIRA_API_TOKEN")
        if not project_key:
            missing.append("JIRA_PROJECT_KEY")
        msg = f"[Jira] Не заданы {', '.join(missing)} — пропуск создания тикета. summary={summary[:80]}"
        print(msg)
        LOG.warning(msg)
        return None
    # Bearer: только токен. Basic: нужен ещё логин (username/email)
    use_bearer = len(api_token) > 20
    if not use_bearer and not login:
        msg = f"[Jira] Для короткого токена нужен JIRA_USERNAME или JIRA_EMAIL — пропуск. summary={summary[:80]}"
        print(msg)
        LOG.warning(msg)
        return None

    if is_ignorable_issue(summary, description):
        print(f"[Jira] Пропуск: похоже на флак/тестовую среду — {summary[:80]}")
        return None

    # Уровень 1: локальная дедупликация (в памяти сессии)
    if not skip_local_duplicate_check and is_local_duplicate(summary, description):
        print(f"[Jira] Пропуск (локальный дубль): {summary[:80]}")
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
        print(f"[Jira] Дубль в Jira, не создаём — найден {dup}")
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
    assignee_value = JIRA_ASSIGNEE if JIRA_ASSIGNEE else login

    priority_name = _jira_priority_name_for_severity(severity)
    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary[:255],
            "description": description,
            "issuetype": {"name": JIRA_ISSUE_TYPE},
            "labels": [JIRA_DEFECT_LABEL],
        }
    }
    if priority_name:
        payload["fields"]["priority"] = {"name": priority_name}
        LOG.debug("create issue: priority.name=%r", priority_name)
    else:
        LOG.debug("create issue: priority не передаётся (JIRA_PRIORITY_* пусто или нет severity)")

    def _post_create(payload_to_send: dict) -> tuple:
        policy = _jira_retry_policy()
        last_response = None
        for attempt in range(policy.attempts()):
            last_response = _jira_http_request(
                "POST",
                url,
                retry_mutating=False,
                json=payload_to_send,
                headers=headers,
                auth=auth,
                verify=JIRA_VERIFY_SSL,
                timeout=30,
            )
            if last_response is not None and last_response.status_code not in policy.retryable_statuses:
                return last_response, None

            # Jira may have accepted the create before a proxy dropped the
            # response. Search before every retry to keep create idempotent.
            duplicate_key = search_duplicates(
                summary,
                jira_url=jira_url,
                username=login,
                api_token=api_token,
                project_key=project_key,
                broad=False,
                request_attempts=1,
            )
            if duplicate_key:
                return None, duplicate_key
            if attempt >= policy.attempts() - 1:
                break
            retry_after = (
                parse_retry_after(last_response.headers)
                if last_response is not None
                else None
            )
            delay = policy.delay_for(
                attempt,
                retry_after=retry_after,
                random_fn=random.uniform,
            )
            LOG.warning(
                "Jira create retry %d/%d in %.1fs",
                attempt + 1,
                policy.attempts(),
                delay,
            )
            time.sleep(delay)
        return last_response, None

    try:
        r, recovered_key = _post_create(payload)
        if recovered_key:
            LOG.info("Jira create response was lost; found issue by dedup: %s", recovered_key)
            register_local_defect(summary)
            return recovered_key
        if r is None:
            LOG.error("Jira create failed after retries: no response")
            return None
        if r.status_code == 400 and "priority" in (payload.get("fields") or {}):
            err_json: dict = {}
            try:
                err_json = r.json() or {}
            except Exception:
                pass
            if (err_json.get("errors") or {}).get("priority"):
                print(
                    f"[Jira] Неверный priority «{priority_name}» — "
                    f"укажи JIRA_PRIORITY_* в .env (как в проекте). Повтор без priority…"
                )
                LOG.warning("Jira 400: invalid priority, retrying without priority field")
                payload2 = copy.deepcopy(payload)
                payload2["fields"].pop("priority", None)
                r, recovered_key = _post_create(payload2)
                if recovered_key:
                    register_local_defect(summary)
                    return recovered_key
                if r is None:
                    return None
        r.raise_for_status()
        key = r.json().get("key")
        if not key:
            LOG.error("Jira create returned success without an issue key: %s", (r.text or "")[:300])
            return None
        LOG.info("Создан дефект: %s", key)
        register_local_defect(summary)

        # Назначить ПОСЛЕ создания отдельным запросом (надёжнее чем в payload)
        if key and assignee_value:
            _assign_issue(jira_url, key, assignee_value, headers=headers, auth=auth, use_bearer=use_bearer)

        if key and attachment_paths:
            paths = [os.fspath(p) for p in attachment_paths]
            _attach_files(
                jira_url, key, paths,
                headers_base=headers,
                auth=auth,
                use_bearer=use_bearer,
            )
        return key
    except requests.exceptions.HTTPError as e:
        print(f"[Jira] Ошибка API {e.response.status_code}: {e.response.text[:200]}")
        LOG.error("Ошибка API: %s — %s", e.response.status_code, e.response.text[:300])
        return None
    except Exception as e:
        print(f"[Jira] Ошибка при создании тикета: {e}")
        LOG.error("Ошибка: %s", e)
        return None


# =============================================
# Ретест: поиск, changelog, переходы, комментарии
# =============================================


def _jira_connection_from_env() -> Optional[dict]:
    """
    Параметры REST для операций с задачами (поиск, переходы, комментарии).
    Возвращает dict или None, если не хватает JIRA_URL / токена / проекта.
    """
    jira_url = (os.getenv("JIRA_URL", "") or "").rstrip("/")
    login = (os.getenv("JIRA_USERNAME", "") or os.getenv("JIRA_EMAIL", "") or "").strip()
    api_token = (os.getenv("JIRA_API_TOKEN", "") or "").strip()
    project_key = (os.getenv("JIRA_PROJECT_KEY", "") or "").strip()
    use_bearer = len(api_token) > 20
    if not jira_url or not api_token or not project_key:
        return None
    if not use_bearer and not login:
        return None
    headers = {"Content-Type": "application/json", "X-Atlassian-Token": "no-check"}
    if use_bearer:
        headers["Authorization"] = f"Bearer {api_token}"
        auth = None
    else:
        auth = (login, api_token)
    return {
        "jira_url": jira_url,
        "login": login,
        "project_key": project_key,
        "use_bearer": use_bearer,
        "headers": headers,
        "auth": auth,
    }


def is_jira_rest_configured() -> bool:
    """True, если заданы параметры для Jira REST (поиск, переходы, комментарии)."""
    return _jira_connection_from_env() is not None


def _jira_rest(
    method: str,
    path: str,
    *,
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
) -> tuple[int, Optional[dict], str]:
    """Низкоуровневый REST. Возвращает (status_code, json|None, raw_text_slice)."""
    conn = _jira_connection_from_env()
    if not conn:
        return 0, None, "Jira: не заданы JIRA_URL / JIRA_API_TOKEN / JIRA_PROJECT_KEY"
    url = f"{conn['jira_url']}/rest/api/2/{path.lstrip('/')}"
    try:
        r = _jira_http_request(
            method,
            url,
            retry_mutating=method.upper() in {"PUT", "DELETE"},
            params=params,
            json=json_body,
            headers=conn["headers"],
            auth=None if conn["use_bearer"] else conn["auth"],
            verify=JIRA_VERIFY_SSL,
            timeout=45,
        )
        if r is None:
            return 0, None, "Jira transport failed after retries"
        text = (r.text or "")[:800]
        if r.status_code in (200, 201):
            try:
                return r.status_code, r.json() if r.text else {}, text
            except Exception:
                return r.status_code, {}, text
        try:
            return r.status_code, r.json() if r.text else None, text
        except Exception:
            return r.status_code, None, text
    except Exception as e:
        LOG.exception("_jira_rest %s %s", method, path)
        return 0, None, str(e)[:400]


def search_kventin_issues_by_status(
    status_name: str,
    *,
    max_results: int = 50,
) -> List[dict]:
    """
    Найти открытые задачи проекта с лейблом kventin в указанном статусе.
    Возвращает элементы issues из /search (каждый с key, fields).
    """
    conn = _jira_connection_from_env()
    if not conn:
        LOG.warning("search_kventin_issues_by_status: нет подключения к Jira")
        return []
    status_escaped = (status_name or "").replace('"', '\\"')
    jql = (
        f'project = {conn["project_key"]} AND labels = {JIRA_DEFECT_LABEL} '
        f'AND status = "{status_escaped}"'
    )
    code, data, _ = _jira_rest(
        "GET",
        "search",
        params={"jql": jql, "fields": "summary,status", "maxResults": max_results},
    )
    if code != 200 or not data:
        LOG.warning("search_kventin: JQL search failed code=%s", code)
        return []
    return list(data.get("issues") or [])


def search_issues_by_status(
    status_name: str,
    *,
    require_label: Optional[str] = None,
    exclude_label: Optional[str] = None,
    issue_type: Optional[str] = None,
    max_results: int = 50,
) -> List[dict]:
    """
    Найти задачи проекта в указанном статусе (универсальный поиск для демона).

    Parameters
    ----------
    status_name:
        Имя статуса Jira.
    require_label:
        Если задан — добавляет ``AND labels = <label>``.
    exclude_label:
        Если задан — добавляет ``AND (labels != <label> OR labels is EMPTY)``.
    issue_type:
        Если задан — добавляет ``AND issuetype = "<type>"``.
    """
    conn = _jira_connection_from_env()
    if not conn:
        LOG.warning("search_issues_by_status: нет подключения к Jira")
        return []
    status_escaped = (status_name or "").replace('"', '\\"')
    jql = f'project = {conn["project_key"]} AND status = "{status_escaped}"'
    if require_label:
        jql += f" AND labels = {require_label}"
    if exclude_label:
        jql += f" AND (labels != {exclude_label} OR labels is EMPTY)"
    if issue_type:
        it = issue_type.replace('"', '\\"')
        jql += f' AND issuetype = "{it}"'
    code, data, _ = _jira_rest(
        "GET",
        "search",
        params={"jql": jql, "fields": "summary,status,issuetype", "maxResults": max_results},
    )
    if code != 200 or not data:
        LOG.warning("search_issues_by_status: JQL search failed code=%s", code)
        return []
    return list(data.get("issues") or [])


def attach_file_to_issue(issue_key: str, file_path: str) -> bool:
    """Приложить один файл к задаче, используя REST-параметры из окружения."""
    conn = _jira_connection_from_env()
    if not conn:
        LOG.warning("attach_file_to_issue: нет подключения к Jira")
        return False
    if not file_path or not os.path.isfile(file_path):
        LOG.warning("attach_file_to_issue: файл не найден: %s", file_path)
        return False
    return _attach_files(
        conn["jira_url"],
        issue_key,
        [file_path],
        headers_base=conn["headers"],
        auth=conn["auth"],
        use_bearer=conn["use_bearer"],
    )


def get_issue_with_changelog(issue_key: str) -> tuple[int, Optional[dict], str]:
    """GET issue with fields needed by retest and task workflows."""
    return _jira_rest(
        "GET",
        f"issue/{issue_key}",
        params={
            "expand": "changelog",
            "fields": "description,summary,status,assignee,reporter,comment,attachment,updated",
        },
    )


def find_author_who_moved_to_status(
    changelog: Optional[dict],
    target_status_name: str,
) -> Optional[dict]:
    """
    Последний (по времени changelog) автор, который перевёл задачу В статус target_status_name.

    Jira отдаёт histories в порядке от старых к новым — берём последнее совпадение toString.
    """
    if not changelog or not target_status_name:
        return None
    histories = changelog.get("histories") or []
    target_lower = target_status_name.strip().lower()
    last_author: Optional[dict] = None
    for h in histories:
        author = h.get("author") or h.get("updateAuthor")
        for item in h.get("items") or []:
            if (item.get("field") or "").lower() != "status":
                continue
            to_s = (item.get("toString") or item.get("to") or "").strip()
            if to_s.lower() == target_lower:
                if isinstance(author, dict):
                    last_author = author
    return last_author


def author_to_assignee_value(author: Optional[dict]) -> str:
    """Из объекта автора Jira сделать строку для _assign_issue (accountId / name / email)."""
    if not author:
        return ""
    if author.get("accountId"):
        return str(author["accountId"])
    if author.get("name"):
        return str(author["name"])
    if author.get("key"):
        return str(author["key"])
    if author.get("emailAddress"):
        return str(author["emailAddress"])
    return ""


def list_issue_transitions(issue_key: str) -> List[dict]:
    code, data, _ = _jira_rest("GET", f"issue/{issue_key}/transitions")
    if code != 200 or not data:
        return []
    return list(data.get("transitions") or [])


def find_transition_id_to_status(
    transitions: List[dict],
    target_status_name: str,
) -> Optional[str]:
    """Подобрать id перехода, у которого to.name совпадает с target_status_name (без учёта регистра)."""
    want = (target_status_name or "").strip().lower()
    if not want:
        return None
    best: Optional[str] = None
    for t in transitions:
        to_name = ((t.get("to") or {}).get("name") or "").strip().lower()
        if to_name == want:
            tid = t.get("id")
            if tid:
                best = str(tid)
    return best


def transition_issue(
    issue_key: str,
    transition_id: str,
    *,
    fields: Optional[dict] = None,
) -> tuple[bool, str]:
    """
    Выполнить переход workflow. fields — опционально (например resolution при закрытии).
    """
    body: dict = {"transition": {"id": str(transition_id)}}
    if fields:
        body["fields"] = fields
    code, data, text = _jira_rest("POST", f"issue/{issue_key}/transitions", json_body=body)
    if code in (200, 204):
        return True, ""
    err = text
    if isinstance(data, dict):
        em = data.get("errorMessages") or []
        if em:
            err = "; ".join(str(x) for x in em)
        errs = data.get("errors") or {}
        if errs:
            err = err + " " + str(errs)
    LOG.warning("transition_issue %s failed: %s %s", issue_key, code, err[:300])
    return False, err or f"HTTP {code}"


def add_issue_comment(issue_key: str, body: str) -> bool:
    """Добавить комментарий (wiki-текст)."""
    code, _, text = _jira_rest(
        "POST",
        f"issue/{issue_key}/comment",
        json_body={"body": body[:100000]},
    )
    if code in (200, 201):
        return True
    LOG.warning("add_issue_comment %s: %s — %s", issue_key, code, text[:200])
    return False


def reopen_or_move_to_in_progress(
    issue_key: str,
    *,
    assignee_value: str,
) -> bool:
    """
    Перевести задачу в In Progress и назначить assignee_value (если непусто).
    """
    trans = list_issue_transitions(issue_key)
    tid = find_transition_id_to_status(trans, JIRA_RETEST_STATUS_IN_PROGRESS)
    if not tid:
        # иногда переход называется "Reopen" но ведёт в In Progress — перебираем
        for t in trans:
            tname = (t.get("name") or "").lower()
            to_name = ((t.get("to") or {}).get("name") or "").lower()
            if "progress" in to_name or "reopen" in tname:
                tid = str(t.get("id") or "")
                if tid:
                    break
    if not tid:
        LOG.error("Нет перехода в статус «%s» для %s", JIRA_RETEST_STATUS_IN_PROGRESS, issue_key)
        return False
    ok, err = transition_issue(issue_key, tid)
    if not ok:
        print(f"[Jira] Не удалось перевести {issue_key} в In Progress: {err}")
        return False
    conn = _jira_connection_from_env()
    if assignee_value and conn:
        _assign_issue(
            conn["jira_url"],
            issue_key,
            assignee_value,
            headers=conn["headers"],
            auth=conn["auth"],
            use_bearer=conn["use_bearer"],
        )
    return True


def resolve_issue_fixed(issue_key: str) -> bool:
    """Перевести в Resolved с resolution = JIRA_RETEST_RESOLUTION_FIXED."""
    trans = list_issue_transitions(issue_key)
    tid = find_transition_id_to_status(trans, JIRA_RETEST_STATUS_RESOLVED)
    if not tid:
        # fallback: ищем переход с «resolve» в имени
        for t in trans:
            n = (t.get("name") or "").lower()
            to_n = ((t.get("to") or {}).get("name") or "").lower()
            if "resolv" in n or to_n == (JIRA_RETEST_STATUS_RESOLVED or "").lower():
                tid = str(t.get("id") or "")
                if tid:
                    break
    if not tid:
        LOG.error("Нет перехода в «%s» для %s", JIRA_RETEST_STATUS_RESOLVED, issue_key)
        return False
    fields = {}
    if JIRA_RETEST_RESOLUTION_FIXED:
        fields["resolution"] = {"name": JIRA_RETEST_RESOLUTION_FIXED}
    ok, err = transition_issue(issue_key, tid, fields=fields if fields else None)
    if ok:
        return True
    # повтор без resolution (если в workflow резолюция не требуется или другое имя)
    if fields:
        ok2, err2 = transition_issue(issue_key, tid, fields=None)
        if ok2:
            LOG.warning("resolve %s: без поля resolution (было: %s)", issue_key, err[:120])
            return True
        err = err2
    print(f"[Jira] Не удалось закрыть {issue_key} как Fixed: {err}")
    return False


def start_qa_transition(issue_key: str) -> bool:
    """Перевести из текущего статуса в QA (статус назначения JIRA_RETEST_STATUS_QA)."""
    trans = list_issue_transitions(issue_key)
    tid = find_transition_id_to_status(trans, JIRA_RETEST_STATUS_QA)
    if not tid:
        LOG.error(
            "Нет перехода в статус «%s» для %s. Доступно: %s",
            JIRA_RETEST_STATUS_QA,
            issue_key,
            [((t.get("to") or {}).get("name"), t.get("name")) for t in trans[:8]],
        )
        return False
    ok, err = transition_issue(issue_key, tid)
    if not ok:
        print(f"[Jira] Не удалось перевести {issue_key} в QA: {err}")
        return False
    return True


def extract_description_text(fields: Optional[dict]) -> str:
    """Достать текст описания из fields (строка wiki или простой текст)."""
    if not fields:
        return ""
    d = fields.get("description")
    if isinstance(d, str):
        return d
    if isinstance(d, dict):
        # ADF (Cloud): очень упрощённо — ищем text в content
        try:
            parts: List[str] = []

            def walk(node: object) -> None:
                if isinstance(node, dict):
                    if node.get("text"):
                        parts.append(str(node["text"]))
                    for c in node.get("content") or []:
                        walk(c)
                elif isinstance(node, list):
                    for c in node:
                        walk(c)

            walk(d.get("content"))
            return "\n".join(parts)
        except Exception:
            return str(d)[:20000]
    return ""


def extract_issue_comment_text(fields: Optional[dict]) -> str:
    """Flatten Jira wiki/ADF comments into searchable plain text."""
    comments = (((fields or {}).get("comment") or {}).get("comments") or [])

    def flatten(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return " ".join(flatten(item) for item in value.values())
        if isinstance(value, list):
            return " ".join(flatten(item) for item in value)
        return ""

    return "\n".join(
        flatten(comment.get("body"))
        for comment in comments
        if isinstance(comment, dict)
    )
