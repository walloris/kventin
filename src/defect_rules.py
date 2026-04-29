"""
Централизованные правила: что считаем багом, а что — шумом.

Все эвристики «является ли это реальным дефектом» вынесены сюда, чтобы:
- не дублировать логику в нескольких местах agent.py;
- править поведение в одном месте;
- снизить шум в Jira (404 на favicon-ах, расширения, аналитика, и т.п.).

Возвращаемые значения у функций rule_* — Optional[Dict] вида:
    {
        "title": "Краткая суть дефекта",
        "details": "Детали (URL, статус, текст ошибки)",
        "severity": "critical" | "major" | "minor",
    }
или None, если по правилу баг не подтверждается.
"""
from __future__ import annotations
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


# --- Фильтры шума: к каким источникам мы НЕ заводим баги ---

_NOISE_HOST_SUFFIXES = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "facebook.com",
    "hotjar.com",
    "yandex.ru/metrika",
    "mc.yandex.ru",
    "sentry.io",
    "newrelic.com",
    "datadog",
    "amplitude.com",
    "segment.io",
    "intercom.io",
    "intercomcdn.com",
    "bugsnag.com",
    "logrocket.com",
    "fullstory.com",
)

_NOISE_PATH_PATTERNS = (
    "/favicon",
    "/apple-touch-icon",
    "/robots.txt",
    "/sitemap",
    "chrome-extension://",
    "moz-extension://",
    "/sw.js",
    "/service-worker",
    "/manifest.json",
)

_NOISE_CONTENT_TYPES = (
    "image/",
    "font/",
    "audio/",
    "video/",
)

_NOISE_TEXT_FRAGMENTS = (
    "extension context invalidated",
    "ResizeObserver loop limit exceeded",
    "ResizeObserver loop completed with undelivered notifications",
    "Non-Error promise rejection captured",
    "<unavailable>",
    "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT",
)


def is_noise_url(url: str) -> bool:
    """URL не интересен для дефектов (аналитика, фавиконы, расширения и т.п.)."""
    if not url:
        return True
    u = url.strip()
    if any(p in u for p in _NOISE_PATH_PATTERNS):
        return True
    try:
        host = (urlparse(u).hostname or "").lower()
    except Exception:
        host = ""
    if any(host.endswith(s) for s in _NOISE_HOST_SUFFIXES):
        return True
    return False


def is_noise_console_text(text: str) -> bool:
    """Сообщение консоли — типичный шум, не баг."""
    if not text:
        return True
    t = text.strip()
    if not t:
        return True
    return any(frag in t for frag in _NOISE_TEXT_FRAGMENTS)


# --- Правила «это баг» ---

def rule_5xx(network_failures: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Любой 5xx ответ от сервера в окне действия — это критичный баг."""
    if not network_failures:
        return None
    for n in network_failures[-30:]:
        status = n.get("status") or 0
        url = n.get("url") or ""
        if status >= 500 and not is_noise_url(url):
            return {
                "title": f"HTTP {status} от сервера: {(urlparse(url).path or '/')[:80]}",
                "details": f"{n.get('method', 'GET')} {url} → {status}",
                "severity": "critical",
            }
    return None


def rule_4xx_on_main(network_failures: List[Dict[str, Any]], current_url: str) -> Optional[Dict[str, Any]]:
    """4xx (кроме 401/403) на основном документе или ключевом API — это баг."""
    if not network_failures:
        return None
    cur_host = ""
    try:
        cur_host = (urlparse(current_url).hostname or "").lower()
    except Exception:
        pass
    for n in network_failures[-30:]:
        status = n.get("status") or 0
        url = n.get("url") or ""
        method = (n.get("method") or "GET").upper()
        # 401/403 часто легитимные (нужна авторизация) — не считаем багом по умолчанию
        if not (400 <= status < 500) or status in (401, 403):
            continue
        if is_noise_url(url):
            continue
        try:
            n_host = (urlparse(url).hostname or "").lower()
        except Exception:
            n_host = ""
        # Своего хоста или явного API на чужом — это интересно.
        is_same_host = (n_host == cur_host) if cur_host and n_host else False
        is_api_path = any(seg in url for seg in ("/api/", "/v1/", "/v2/", "/graphql"))
        if not (is_same_host or is_api_path):
            continue
        return {
            "title": f"HTTP {status} на {method} {(urlparse(url).path or '/')[:80]}",
            "details": f"{method} {url} → {status}",
            "severity": "major",
        }
    return None


# Серьёзные JS-сигналы, которые приходят и через console.error, и через pageerror.
# Если такие фрагменты встречаются в тексте — это однозначно баг, а не предупреждение.
_SEVERE_JS_PATTERNS = (
    "Uncaught",
    "TypeError",
    "ReferenceError",
    "SyntaxError",
    "RangeError",
    "is not a function",
    "is not defined",
    "Cannot read properties",
    "Cannot read property",
    "Cannot set properties",
    "Cannot set property",
    "Failed to fetch",
    "NetworkError when attempting",
)


def _looks_like_severe_js_error(text: str) -> bool:
    if not text:
        return False
    return any(p in text for p in _SEVERE_JS_PATTERNS)


def rule_pageerror(console_log: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Необработанное JS-исключение (pageerror) или серьёзный console.error — это баг.

    Берём:
      - всё что прилетает с типом `pageerror` (это всегда необработанное исключение);
      - сообщения с типом `error`, в которых видны типичные JS-исключения
        (TypeError/ReferenceError/Uncaught и т.п.). Обычные `console.error("...")`
        с произвольным текстом без признаков исключения мы не считаем багом —
        часто это просто диагностика приложения.
    """
    if not console_log:
        return None
    for c in reversed(console_log[-80:]):
        ctype = (c.get("type") or "").lower()
        text = (c.get("text") or "").strip()
        if not text or is_noise_console_text(text):
            continue
        if ctype == "pageerror":
            return {
                "title": f"JS pageerror: {text[:120]}",
                "details": text[:600],
                "severity": "major",
            }
        if ctype == "error" and _looks_like_severe_js_error(text):
            return {
                "title": f"JS error в консоли: {text[:120]}",
                "details": text[:600],
                "severity": "major",
            }
    return None


# Текст из click_error/Playwright, по которому однозначно видно UI-проблему.
_INTERCEPT_FRAGMENTS = (
    "intercepts pointer events",
    "intercept pointer events",
)
_TIMEOUT_FRAGMENTS = (
    "Timeout 10000ms exceeded",
    "Timeout 30000ms exceeded",
    "exceeded while waiting",
    "waiting for element to be visible, enabled and stable",
)


def _extract_intercept_class(result_text: str) -> str:
    """Выдернуть класс перекрывающего элемента из текста ошибки Playwright."""
    m = re.search(r'<div\s+class="([^"]+)"[^>]*>\s*[^<]*</div>\s*from\s*<', result_text or "")
    if m:
        return m.group(1)
    m = re.search(r'<(\w+)\s+class="([^"]+)"[^>]*>[^<]*</\1>\s*(?:from|subtree intercepts)', result_text or "")
    if m:
        return m.group(2)
    return ""


def rule_action_failure(
    action: Optional[Dict[str, Any]],
    result: str,
    page_url: str = "",
) -> Optional[Dict[str, Any]]:
    """Действие в браузере не удалось из-за реальной UI-проблемы.

    Сценарии:
      1) "subtree intercepts pointer events" — элемент видим, но невидимый/прозрачный
         div сверху перехватывает клики. Реальный пользователь столкнётся с тем же.
         => critical UX баг.
      2) "Timeout … exceeded while waiting" / "waiting for element to be visible…"
         — элемент не становится usable за 10–30 секунд. => major.

    `not_found:` / `detached` намеренно НЕ заводим — это, как правило, устаревший
    ref после ре-рендера, а не баг продукта.
    """
    if not result or not isinstance(result, str):
        return None
    res = result.strip()
    if not res:
        return None
    if not (res.startswith("click_error") or res.startswith("type_error") or res.startswith("hover_error")):
        return None

    act = (action or {}).get("action", "?")
    sel = (action or {}).get("selector", "") or (action or {}).get("locator", "")
    sel_brief = (sel or "")[:120]

    if any(frag in res for frag in _INTERCEPT_FRAGMENTS):
        cls = _extract_intercept_class(res)
        cls_part = f" Перекрывающий элемент: <div class=\"{cls}\">." if cls else ""
        title = f"Кнопка/элемент недоступна для клика: перекрыта другим элементом ({sel_brief or act})"
        details = (
            f"Действие: {act} → '{sel_brief}'\n"
            f"URL: {page_url}\n"
            f"Реакция Playwright: пользовательский клик невозможен — другой элемент "
            f"перехватывает события указателя.{cls_part}\n\n"
            f"Полный лог ошибки:\n{res[:1500]}"
        )
        return {"title": title[:200], "details": details, "severity": "critical"}

    if any(frag in res for frag in _TIMEOUT_FRAGMENTS):
        title = f"Элемент не становится кликабельным за 10с: {sel_brief or act}"
        details = (
            f"Действие: {act} → '{sel_brief}'\n"
            f"URL: {page_url}\n"
            f"Реакция Playwright: ожидание visible/enabled/stable истекло. "
            f"Похоже на бесконечный спиннер или зависший UI.\n\n"
            f"Полный лог ошибки:\n{res[:1500]}"
        )
        return {"title": title[:200], "details": details, "severity": "major"}

    return None


def rule_page_load_errors(
    page_url: str,
    console_entries: List[Dict[str, Any]],
    network_entries: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Дефект «при открытии страницы» — без привязки к действию агента.

    Что считаем багом загрузки:
      • `pageerror` или серьёзный `console.error` в окне после goto
        (TypeError/ReferenceError/Uncaught/…)
      • 5xx на любом запросе
      • 4xx на ключевых запросах: HTML-документ или API того же хоста
        (401/403 не считаем — часто легитимная авторизация).

    Возвращает {title, details, severity, kind, errors_summary}, где
    `kind = "page_load"` помогает на стороне create_defect отделить класс дефекта.
    """
    page_url = page_url or ""
    page_host = ""
    try:
        page_host = (urlparse(page_url).hostname or "").lower()
    except Exception:
        pass

    # 1) JS-ошибки
    js_hits: List[Dict[str, Any]] = []
    for c in console_entries[-200:]:
        ctype = (c.get("type") or "").lower()
        text = (c.get("text") or "").strip()
        if not text or is_noise_console_text(text):
            continue
        if ctype == "pageerror" or (ctype == "error" and _looks_like_severe_js_error(text)):
            js_hits.append(c)

    # 2) Сетевые
    net_5xx: List[Dict[str, Any]] = []
    net_4xx: List[Dict[str, Any]] = []
    for n in network_entries[-200:]:
        url = n.get("url") or ""
        status = n.get("status") or 0
        if not isinstance(status, int) or status < 400 or status == 0:
            continue
        if is_noise_url(url):
            continue
        try:
            n_host = (urlparse(url).hostname or "").lower()
        except Exception:
            n_host = ""
        is_main_doc = url.split("?")[0].rstrip("/") == page_url.split("?")[0].rstrip("/")
        is_same_host = bool(page_host and n_host == page_host)
        is_api = any(seg in url for seg in ("/api/", "/v1/", "/v2/", "/graphql"))
        if status >= 500:
            net_5xx.append(n)
            continue
        if 400 <= status < 500 and status not in (401, 403):
            if is_main_doc or (is_same_host and is_api):
                net_4xx.append(n)

    if not js_hits and not net_5xx and not net_4xx:
        return None

    # Severity: 5xx → critical; pageerror/severe → major; 4xx → major
    severity = "major"
    if net_5xx:
        severity = "critical"
    elif any((c.get("type") or "").lower() == "pageerror" for c in js_hits):
        severity = "major"

    # Заголовок и детали
    if net_5xx:
        worst = net_5xx[-1]
        title = f"Страница не работает корректно при открытии: {worst.get('status')} {worst.get('method', 'GET')} {(urlparse(worst.get('url') or '').path or '/')[:80]}"
    elif js_hits:
        first = js_hits[0]
        title = f"Ошибки в консоли при открытии страницы: {(first.get('text') or '')[:120]}"
    else:
        worst = net_4xx[-1]
        title = f"Ошибка ресурса при открытии страницы: {worst.get('status')} {(urlparse(worst.get('url') or '').path or '/')[:80]}"

    details_parts: List[str] = [f"URL: {page_url}"]
    if js_hits:
        details_parts.append(f"JS-ошибок в консоли: {len(js_hits)} (см. вложение console.log и блок «Ошибки консоли»).")
    if net_5xx:
        details_parts.append(f"5xx-ответов: {len(net_5xx)}.")
    if net_4xx:
        details_parts.append(f"4xx на ключевых запросах: {len(net_4xx)}.")
    details = "\n".join(details_parts)

    return {
        "title": title[:200],
        "details": details,
        "severity": severity,
        "kind": "page_load",
        "errors_summary": {
            "js_count": len(js_hits),
            "net_5xx": len(net_5xx),
            "net_4xx": len(net_4xx),
        },
    }


def rule_blank_page(page_text: str) -> Optional[Dict[str, Any]]:
    """Подозрение на «белый экран»: содержимое body — пусто или совсем мало."""
    if not isinstance(page_text, str):
        return None
    body = page_text.strip()
    if len(body) < 30:
        return {
            "title": "Похоже, страница пустая (белый экран)",
            "details": f"body содержит {len(body)} символов: '{body[:120]}'",
            "severity": "critical",
        }
    return None


# --- Решающая функция: пропустить или нет ---

def should_create_defect(
    *,
    bug_text: str,
    console_log: Optional[List[Dict[str, Any]]] = None,
    network_failures: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """
    Финальный фильтр перед отправкой в Jira.

    Возвращает False, если уверены что это не баг (одно лишь сообщение про шум).
    Возвращает True, если есть хоть одно реально подозрительное событие.
    """
    text = (bug_text or "").strip()
    if not text:
        return False
    # Если в bug_text по сути одна строка про известный шум — не заводим
    if is_noise_console_text(text):
        return False
    # Если есть 5xx или непустой pageerror — это баг
    if rule_5xx(network_failures or []):
        return True
    if rule_pageerror(console_log or []):
        return True
    # Иначе доверяем оракулу (LLM): он уже предположил, что это дефект
    return True


__all__ = [
    "is_noise_url",
    "is_noise_console_text",
    "rule_5xx",
    "rule_4xx_on_main",
    "rule_pageerror",
    "rule_action_failure",
    "rule_page_load_errors",
    "rule_blank_page",
    "should_create_defect",
]
